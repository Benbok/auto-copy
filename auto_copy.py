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
from PIL import Image, ImageDraw
from pynput import keyboard, mouse

APP_NAME = "AutoCopy"
CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

DEFAULT_CONFIG = {
    "enabled": True,
    "sound": True,
    "notifications": True,
    # Минимальная длина протягивания мыши (px), чтобы считать это выделением
    "min_drag_px": 6,
    # Интервал между щелчками для двойного/тройного щелчка (мс)
    "multi_click_ms": 450,
    # Сколько символов показывать в уведомлении
    "preview_chars": 120,
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
def single_instance_or_exit():
    """Не даём запустить вторую копию программы."""
    handle = kernel32.CreateMutexW(None, False, f"Global\\{APP_NAME}_mutex")
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        user32.MessageBoxW(None, "AutoCopy уже запущен (смотрите в трее).", APP_NAME, 0x40)
        sys.exit(0)
    return handle


def foreground_process_name():
    hwnd = user32.GetForegroundWindow()
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    try:
        return psutil.Process(pid.value).name().lower()
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


def make_icon(active=True):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    color = (46, 134, 222) if active else (140, 140, 140)
    d.rounded_rectangle((18, 6, 54, 46), 6, outline=color, width=5)
    d.rounded_rectangle((8, 18, 44, 58), 6, fill=color)
    for y in (28, 37, 46):
        d.line((15, y, 37, y), fill="white", width=3)
    return img


# ------------------------------------------------------------------ app ---
class AutoCopy:
    def __init__(self):
        self.cfg = load_config()
        self.kb = keyboard.Controller()
        self.jobs = queue.Queue()
        self.press_pos = None
        self.last_click_time = 0.0
        self.last_click_pos = (0, 0)
        self.icon = pystray.Icon(APP_NAME, make_icon(self.cfg["enabled"]),
                                 "AutoCopy", menu=self.build_menu())

    # ----- tray menu
    def build_menu(self):
        def toggle(key):
            def action(icon, item):
                self.cfg[key] = not self.cfg[key]
                save_config(self.cfg)
                if key == "enabled":
                    icon.icon = make_icon(self.cfg["enabled"])
            return action

        def checked(key):
            return lambda item: self.cfg[key]

        return pystray.Menu(
            pystray.MenuItem("Автокопирование включено", toggle("enabled"),
                             checked=checked("enabled"), default=True),
            pystray.MenuItem("Звуковой сигнал", toggle("sound"), checked=checked("sound")),
            pystray.MenuItem("Всплывающие уведомления", toggle("notifications"),
                             checked=checked("notifications")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Запускать вместе с Windows",
                             lambda i, it: set_autostart(not autostart_enabled()),
                             checked=lambda it: autostart_enabled()),
            pystray.MenuItem("Открыть настройки (config.json)",
                             lambda i, it: os.startfile(CONFIG_PATH)),
            pystray.MenuItem("Перечитать настройки", lambda i, it: self.reload_config()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", self.quit),
        )

    def reload_config(self):
        self.cfg = load_config()
        self.icon.icon = make_icon(self.cfg["enabled"])

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

        if dragged or multi:
            self.jobs.put(now)

    # ----- копирование (отдельный рабочий поток)
    def worker(self):
        while True:
            self.jobs.get()
            time.sleep(0.12)  # даём приложению завершить выделение
            # Схлопываем серию событий (например, тройной щелчок) в одно копирование
            while not self.jobs.empty():
                self.jobs.get_nowait()
                time.sleep(0.12)
            try:
                self.try_copy()
            except Exception:
                pass

    def try_copy(self):
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
            self.announce(text)

    def announce(self, text):
        if self.cfg["sound"]:
            winsound.PlaySound("SystemAsterisk", winsound.SND_ALIAS | winsound.SND_ASYNC)
        if self.cfg["notifications"]:
            preview = " ".join(text.split())
            n = self.cfg["preview_chars"]
            if len(preview) > n:
                preview = preview[:n] + "…"
            self.icon.notify(preview, f"Текст скопирован ({len(text)} симв.)")

    # ----- lifecycle
    def quit(self, icon, item):
        self.listener.stop()
        icon.stop()

    def run(self):
        threading.Thread(target=self.worker, daemon=True).start()
        self.listener = mouse.Listener(on_click=self.on_click)
        self.listener.start()
        self.icon.run()


if __name__ == "__main__":
    try:  # корректные координаты мыши на мониторах с масштабированием
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        pass
    _mutex = single_instance_or_exit()
    AutoCopy().run()
