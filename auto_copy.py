"""
AutoCopy — копирование выделенного текста без Ctrl+C (Windows).

Приложение живёт в системном трее. Как только пользователь выделяет текст
мышью (протягиванием, двойным или тройным щелчком) в любом окне, программа
сама отправляет Ctrl+C, проверяет, что в буфер обмена действительно попал
текст, и подаёт звуковой сигнал + всплывающее уведомление.
"""

import ctypes
import ctypes.wintypes as wt
import json
import math
import os
import queue
import sys
import threading
import time
import winreg
import winsound

import psutil
import pyperclip
import pystray
from pynput import keyboard, mouse

from feedback import CopyPopup, enable_high_dpi, make_soft_sound
from selection import selection_end_rect
from program_settings import ProgramSettings, updated_exclusions
from app_icon import make_icon

APP_NAME = "AutoCopy"
CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

DEFAULT_CONFIG = {
    "enabled": True,
    "sound": True,
    "notifications": True,
    # Тема плашки уведомления: "dark", "light" или "system" (как в Windows)
    "theme": "dark",
    # Минимальная длина протягивания мыши (px), чтобы считать это выделением
    "min_drag_px": 6,
    # Интервал между щелчками для двойного/тройного щелчка (мс)
    "multi_click_ms": 450,
    # Какое выделение мышью копировать: "left_to_right" (слева направо, на нескольких
    # строках — сверху вниз), "right_to_left" (справа налево, снизу вверх) или "any" (всегда).
    # Двойной и тройной щелчок направления не имеют и копируются всегда.
    "copy_direction": "left_to_right",
    # Программы, в которых автокопирование отключено.
    # Терминалы — потому что Ctrl+C там прерывает выполнение команды,
    # проводник — чтобы не копировать файлы при перетаскивании.
    "excluded_processes": [
        "cmd.exe",
        "powershell.exe",
        "pwsh.exe",
        "windowsterminal.exe",
        "conhost.exe",
        "mintty.exe",
        "putty.exe",
        "explorer.exe",
    ],
}

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN = 0x10, 0x11, 0x12, 0x5B, 0x5C
VK_C = 0x43


# ---------------------------------------------------------------- config ---
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    save_config(cfg)
    return cfg


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# --------------------------------------------------------------- helpers ---
COPY_DIRECTIONS = ("left_to_right", "right_to_left", "any")


def drag_allowed(mode, press, release):
    """Нужно ли копировать выделение, протянутое от press до release."""
    if mode == "any":
        return True
    (px, py), (x, y) = press, release
    forward = y > py if abs(y - py) > 24 else x >= px
    if mode == "right_to_left":
        return not forward
    return forward  # "left_to_right" и любое неизвестное значение


def single_instance_or_exit():
    """Не даём запустить вторую копию программы."""
    handle = kernel32.CreateMutexW(None, False, f"Global\\{APP_NAME}_mutex")
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        user32.MessageBoxW(None, "AutoCopy уже запущен (смотрите в трее).", APP_NAME, 0x40)
        sys.exit(0)
    return handle


def foreground_process_id():
    hwnd = user32.GetForegroundWindow()
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def foreground_process_name():
    try:
        return psutil.Process(foreground_process_id()).name().lower()
    except (psutil.Error, ValueError):
        return ""


def modifiers_held():
    return any(user32.GetAsyncKeyState(vk) & 0x8000
               for vk in (VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN))


def clipboard_seq():
    return user32.GetClipboardSequenceNumber()


def autostart_command():
    if getattr(sys, "frozen", False):  # собранный .exe
        return f'"{sys.executable}"'
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return f'"{pythonw}" "{os.path.abspath(__file__)}"'


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(on):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except OSError:
                pass


# ------------------------------------------------------------------ app ---
class AutoCopy:
    def __init__(self):
        self.cfg = load_config()
        self.popup = CopyPopup(theme=self.cfg.get("theme", "dark"))
        self.copy_sound = make_soft_sound()
        self.sound_requested = threading.Event()
        self.kb = keyboard.Controller()
        self.jobs = queue.Queue()
        self.settings_requested = threading.Event()
        self.settings_window = None
        self.press_pos = None
        self.last_click_time = 0.0
        self.last_click_pos = (0, 0)
        self.icon = pystray.Icon(APP_NAME, make_icon(self.cfg["enabled"]),
                                 "AutoCopy", menu=self.build_menu())
        self.popup.root.after(100, self._poll_settings)

    # ----- tray menu
    def build_menu(self):
        def toggle(key):
            def action(icon, item):
                self.cfg[key] = not self.cfg[key]
                save_config(self.cfg)
                if key == "enabled":
                    icon.icon = make_icon(self.cfg["enabled"])
                if key == "notifications" and not self.cfg[key]:
                    self.popup.hide()
            return action

        def checked(key):
            return lambda item: self.cfg[key]

        def set_theme_action(theme_val):
            def action(icon, item):
                self.cfg["theme"] = theme_val
                save_config(self.cfg)
                self.popup.set_theme(theme_val)
            return action

        def is_theme(theme_val):
            return lambda item: self.cfg.get("theme", "dark") == theme_val

        def set_direction_action(mode):
            def action(icon, item):
                self.cfg["copy_direction"] = mode
                save_config(self.cfg)
            return action

        def is_direction(mode):
            return lambda item: self.cfg.get("copy_direction") == mode or (
                mode == "left_to_right" and self.cfg.get("copy_direction") not in COPY_DIRECTIONS)

        return pystray.Menu(
            pystray.MenuItem("Автокопирование включено", toggle("enabled"),
                             checked=checked("enabled"), default=True),
            pystray.MenuItem("Звуковой сигнал", toggle("sound"), checked=checked("sound")),
            pystray.MenuItem("Всплывающие уведомления", toggle("notifications"),
                             checked=checked("notifications")),
            pystray.MenuItem("Что копировать", pystray.Menu(
                pystray.MenuItem("Выделение слева направо", set_direction_action("left_to_right"),
                                 checked=is_direction("left_to_right"), radio=True),
                pystray.MenuItem("Выделение справа налево", set_direction_action("right_to_left"),
                                 checked=is_direction("right_to_left"), radio=True),
                pystray.MenuItem("Любое выделение", set_direction_action("any"),
                                 checked=is_direction("any"), radio=True),
            )),
            pystray.MenuItem("Тема уведомлений", pystray.Menu(
                pystray.MenuItem("Тёмная", set_theme_action("dark"),
                                 checked=is_theme("dark"), radio=True),
                pystray.MenuItem("Светлая", set_theme_action("light"),
                                 checked=is_theme("light"), radio=True),
                pystray.MenuItem("Как в системе", set_theme_action("system"),
                                 checked=is_theme("system"), radio=True),
            )),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Запускать вместе с Windows",
                             lambda i, it: set_autostart(not autostart_enabled()),
                             checked=lambda it: autostart_enabled()),
            pystray.MenuItem("Настройки программ…", lambda i, it: self.settings_requested.set()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", self.quit),
        )

    def reload_config(self):
        self.cfg = load_config()
        self.icon.icon = make_icon(self.cfg["enabled"])
        if not self.cfg["notifications"]:
            self.popup.hide()
        self.popup.set_theme(self.cfg.get("theme", "dark"))

    def set_programs_excluded(self, names, excluded):
        updated = dict(self.cfg, excluded_processes=updated_exclusions(
            self.cfg["excluded_processes"], names, excluded))
        save_config(updated)
        self.cfg = updated

    def _poll_settings(self):
        if self.settings_requested.is_set():
            self.settings_requested.clear()
            self.popup.hide()
            if self.settings_window is None or not self.settings_window.window.winfo_exists():
                self.settings_window = ProgramSettings(
                    self.popup.root, lambda: self.cfg["excluded_processes"], self.set_programs_excluded)
            self.settings_window.show()
        self.popup.root.after(100, self._poll_settings)

    # ----- mouse handling (вызывается в потоке pynput — не блокируем его)
    def on_click(self, x, y, button, pressed):
        if button != mouse.Button.left:
            return
        if pressed:
            self.press_pos = (x, y)
            return
        if not self.cfg["enabled"] or self.press_pos is None:
            return

        px, py = self.press_pos
        dragged = math.hypot(x - px, y - py) >= self.cfg["min_drag_px"]

        now = time.monotonic()
        lx, ly = self.last_click_pos
        multi = (now - self.last_click_time) * 1000 <= self.cfg["multi_click_ms"] \
            and math.hypot(x - lx, y - ly) < 5
        self.last_click_time, self.last_click_pos = now, (x, y)

        multiline = abs(y - py) > 24
        if dragged and not drag_allowed(self.cfg.get("copy_direction"), self.press_pos, (x, y)):
            return  # направление отключено в настройках (обычно так выделяют, чтобы заменить)

        if dragged or multi:
            # Clicks can select a whole word beyond the cursor; use below then.
            position = (max(px, x), max(py, y) if multiline else y)
            self.jobs.put((position, not dragged or multiline))

    # ----- копирование (отдельный рабочий поток)
    def worker(self):
        while True:
            position, prefer_below = self.jobs.get()
            time.sleep(0.12)  # даём приложению завершить выделение
            # Схлопываем серию событий (например, тройной щелчок) в одно копирование
            while not self.jobs.empty():
                position, prefer_below = self.jobs.get_nowait()
                time.sleep(0.12)
            try:
                self.try_copy(position, prefer_below)
            except Exception:
                pass

    def try_copy(self, position=None, prefer_below=False):
        if foreground_process_id() == os.getpid():
            return  # Do not inject Ctrl+C into AutoCopy's own settings window.
        if modifiers_held():
            return  # Shift/Ctrl/Alt зажаты — Ctrl+C превратился бы в другое сочетание
        if foreground_process_name() in {p.lower() for p in self.cfg["excluded_processes"]}:
            return

        before = clipboard_seq()
        # Отправляем именно виртуальную клавишу C, чтобы работало в любой раскладке
        c_key = keyboard.KeyCode.from_vk(VK_C)
        with self.kb.pressed(keyboard.Key.ctrl):
            self.kb.press(c_key)
            self.kb.release(c_key)

        deadline = time.monotonic() + 0.6
        while clipboard_seq() == before:
            if time.monotonic() > deadline:
                return  # ничего не выделено — буфер обмена не менялся
            time.sleep(0.02)
        time.sleep(0.03)

        text = ""
        for _ in range(5):  # буфер может быть ненадолго занят другим приложением
            try:
                text = pyperclip.paste()
                break
            except pyperclip.PyperclipException:
                time.sleep(0.05)
        if text and text.strip():
            rect = selection_end_rect() if self.cfg["notifications"] else None
            self.announce(text, position, prefer_below, rect)

    def announce(self, text, position=None, prefer_below=False, rect=None):
        if self.cfg["notifications"]:
            self.popup.show(position, prefer_below, rect)
        if self.cfg["sound"]:
            self.sound_requested.set()

    def sound_worker(self):
        while True:
            self.sound_requested.wait()
            self.sound_requested.clear()
            if not self.cfg["sound"]:
                continue
            try:
                # Keep the bell's tail from delaying subsequent copies.
                winsound.PlaySound(self.copy_sound, winsound.SND_MEMORY | winsound.SND_NODEFAULT)
            except RuntimeError:
                pass  # Copying still works when no audio device is available.

    # ----- lifecycle
    def quit(self, icon, item):
        self.listener.stop()
        self.popup.stop()
        icon.stop()

    def run(self):
        threading.Thread(target=self.worker, daemon=True).start()
        threading.Thread(target=self.sound_worker, daemon=True).start()
        self.listener = mouse.Listener(on_click=self.on_click)
        self.listener.start()
        threading.Thread(target=self.icon.run, daemon=True).start()
        try:
            self.popup.run()
        finally:
            self.listener.stop()
            self.icon.stop()


if __name__ == "__main__":
    enable_high_dpi()
    _mutex = single_instance_or_exit()
    AutoCopy().run()
