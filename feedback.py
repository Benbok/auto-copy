"""Quiet audio and a single reusable, non-activating copy popup (Windows)."""

import array
import ctypes
import ctypes.wintypes as wt
import io
import math
import threading
import time
import tkinter as tk
import wave

from PIL import Image, ImageDraw, ImageTk
import winreg

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

THEME_PALETTES = {
    "dark": {
        "bg": "#202024",
        "border": None,
        "text_fg": "#ffffff",
        "icon_bg": "#064e3b",
        "icon_fg": "#34d399",
    },
    "light": {
        "bg": "#ffffff",
        "border": "#e2e8f0",
        "text_fg": "#18181b",
        "icon_bg": "#dcfce7",
        "icon_fg": "#16a34a",
    },
}


def get_system_theme():
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return "light" if value == 1 else "dark"
    except OSError:
        return "dark"


def resolve_theme(theme_setting):
    if theme_setting in ("light", "dark"):
        return theme_setting
    if theme_setting == "system":
        return get_system_theme()
    return "dark"


def enable_high_dpi():
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


def get_dpi_scale():
    enable_high_dpi()
    try:
        hdc = user32.GetDC(0)
        dpi = gdi32.GetDeviceCaps(hdc, 88)
        user32.ReleaseDC(0, hdc)
        return max(1.0, dpi / 96.0)
    except Exception:
        return 1.0


def make_smooth_check_icon(size, bg_color="#064e3b", check_color="#34d399", scale=4):
    big = size * scale
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = scale
    draw.ellipse((pad, pad, big - pad, big - pad), fill=bg_color)
    pts = [
        (big * 0.28, big * 0.52),
        (big * 0.44, big * 0.68),
        (big * 0.72, big * 0.36),
    ]
    draw.line(pts, fill=check_color, width=max(1, round(2.2 * scale)), joint="round")
    return ImageTk.PhotoImage(img.resize((size, size), Image.Resampling.LANCZOS))


enable_high_dpi()

user32 = ctypes.windll.user32
user32.GetParent.argtypes = [wt.HWND]
user32.GetParent.restype = wt.HWND
user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
user32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, wt.LONG]
user32.SetWindowPos.argtypes = [
    wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, wt.UINT,
]
user32.SetWindowRgn.argtypes = [wt.HWND, wt.HANDLE, wt.BOOL]
gdi32 = ctypes.windll.gdi32
gdi32.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
gdi32.CreateRoundRectRgn.restype = wt.HANDLE
gdi32.DeleteObject.argtypes = [wt.HANDLE]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


user32.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
user32.MonitorFromPoint.restype = wt.HANDLE
user32.GetMonitorInfoW.argtypes = [wt.HANDLE, ctypes.POINTER(MONITORINFO)]


def monitor_work_area(position):
    monitor = user32.MonitorFromPoint(wt.POINT(*position), 2)
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(info)
    if user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        area = info.rcWork
    else:
        area = wt.RECT()
        user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(area), 0)
    return area.left, area.top, area.right, area.bottom


def popup_position(anchor, size, area, prefer_below=False):
    ax, ay = anchor
    width, height = size
    left, top, right, bottom = area
    x, y = ax + 28, ay - height // 2
    if prefer_below or x + width > right - 8:
        x, y = ax - width // 2, ay + 32
        if y + height > bottom - 8:
            y = ay - height - 32
    return (max(left + 8, min(x, right - width - 8)),
            max(top + 8, min(y, bottom - height - 8)))


RECT_GAP = 10


def rect_popup_position(rect, size, area):
    """Place the popup right of the selection end; below it if there is no room."""
    left, top, right, bottom = rect
    width, height = size
    a_left, a_top, a_right, a_bottom = area
    x, y = right + RECT_GAP, (top + bottom) // 2 - height // 2
    if x + width > a_right - 8:
        x, y = right - width, bottom + 6
        if y + height > a_bottom - 8:
            y = top - height - 6
    return (max(a_left + 8, min(x, a_right - width - 8)),
            max(a_top + 8, min(y, a_bottom - height - 8)))


def make_soft_sound():
    rate = 44100
    duration = 0.50
    count = int(rate * duration)
    values = []
    notes = [
        (0, 659.25, [(1, 1, 0.105), (2, 0.055, 0.055), (3, 0.012, 0.025)], 0.018),
        (0.055, 988.875, [(1, 0.12, 0.085)], 0.025),
    ]
    for i in range(count):
        t = i / rate
        value = 0.0
        for start, frequency, partials, attack in notes:
            age = t - start
            if age < 0:
                continue
            strike = 1 - math.exp(-age / attack)
            value += strike * sum(
                amplitude * math.sin(2 * math.pi * frequency * ratio * age)
                * math.exp(-age / decay)
                for ratio, amplitude, decay in partials
            )
        value *= min(1.0, (count - 1 - i) / (rate * 0.07))
        values.append(value)
    scale = 1900 / max(abs(value) for value in values)
    samples = array.array("h", (round(value * scale) for value in values))
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(samples.tobytes())
    return output.getvalue()


class CopyPopup:
    """Owns Tk on the main thread; other threads only update pending state."""

    duration_ms = 1600
    fade_in_ms = 120
    fade_out_ms = 220

    def __init__(self, theme="dark"):
        self._lock = threading.Lock()
        self._pending = None
        self._pending_theme = None
        self._hide_requested = False
        self._stopped = threading.Event()
        self._hide_timer = None
        self._animation_timer = None
        self._scale = get_dpi_scale()
        self._theme_setting = theme

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.0)

        self.panel = tk.Frame(self.root)
        self.panel.pack(fill="both", expand=True)

        self.badge = tk.Label(self.panel, bd=0, highlightthickness=0)
        self.badge.pack(side="left", padx=(round(14 * self._scale), round(10 * self._scale)),
                        pady=round(10 * self._scale))

        self.title = tk.Label(self.panel, text="Текст скопирован",
                              font=("Segoe UI", 10, "bold"), anchor="w")
        self.title.pack(side="left", padx=(0, round(18 * self._scale)),
                        pady=round(10 * self._scale))

        self._apply_theme(theme)

        self.root.update_idletasks()
        self._hwnd = user32.GetParent(self.root.winfo_id())
        # Tool window, click-through, never take focus from the source app.
        style = user32.GetWindowLongW(self._hwnd, -20)
        user32.SetWindowLongW(
            self._hwnd, -20, style | 0x00000080 | 0x00000020 | 0x08000000)
        self.root.after(40, self._poll)

    def set_theme(self, theme):
        with self._lock:
            self._pending_theme = theme

    def _apply_theme(self, theme_setting):
        self._theme_setting = theme_setting
        effective = resolve_theme(theme_setting)
        palette = THEME_PALETTES.get(effective, THEME_PALETTES["dark"])
        self._current_palette = palette

        border = palette.get("border")
        bg = palette["bg"]
        self.root.configure(bg=border if border else bg)
        self.panel.configure(bg=bg)
        if border:
            self.panel.pack_configure(padx=1, pady=1)
        else:
            self.panel.pack_configure(padx=0, pady=0)

        icon_size = round(22 * self._scale)
        self._icon_img = make_smooth_check_icon(
            icon_size, bg_color=palette["icon_bg"], check_color=palette["icon_fg"]
        )
        self.badge.configure(image=self._icon_img, bg=bg)
        self.title.configure(bg=bg, fg=palette["text_fg"])

    def show(self, position=None, prefer_below=False, rect=None):
        if position is None and rect is not None:
            position = (rect[2], (rect[1] + rect[3]) // 2)
        if position is None:
            point = wt.POINT()
            user32.GetCursorPos(ctypes.byref(point))
            position = (point.x, point.y)
        with self._lock:
            self._pending = (position, prefer_below, rect)

    def hide(self):
        with self._lock:
            self._pending = None
            self._hide_requested = True

    def stop(self):
        self._stopped.set()

    def _hide(self):
        if self._hide_timer is not None:
            self.root.after_cancel(self._hide_timer)
            self._hide_timer = None
        if self._animation_timer is not None:
            self.root.after_cancel(self._animation_timer)
            self._animation_timer = None
        self.root.withdraw()
        self.root.attributes("-alpha", 0.0)

    def _animate(self, target, duration_ms, on_finish=None):
        if self._animation_timer is not None:
            self.root.after_cancel(self._animation_timer)
            self._animation_timer = None
        initial = float(self.root.attributes("-alpha"))
        started = time.monotonic()

        def step():
            self._animation_timer = None
            progress = min(1.0, (time.monotonic() - started) * 1000 / duration_ms)
            eased = progress * progress * (3 - 2 * progress)
            self.root.attributes("-alpha", initial + (target - initial) * eased)
            if progress < 1:
                self._animation_timer = self.root.after(16, step)
            elif on_finish is not None:
                on_finish()

        step()

    def _fade_out(self):
        self._hide_timer = None
        self._animate(0.0, self.fade_out_ms, self._hide)

    def _poll(self):
        if self._stopped.is_set():
            self._hide()
            self.root.destroy()
            return
        with self._lock:
            pending, self._pending = self._pending, None
            hide, self._hide_requested = self._hide_requested, False
            new_theme, self._pending_theme = self._pending_theme, None
        if new_theme is not None:
            self._apply_theme(new_theme)
        if hide:
            self._hide()
        if pending is not None:
            self.root.update_idletasks()
            width = self.root.winfo_reqwidth()
            height = self.root.winfo_reqheight()
            position, prefer_below, rect = pending
            area = monitor_work_area(position)
            if rect is not None:
                x, y = rect_popup_position(rect, (width, height), area)
            else:
                x, y = popup_position(position, (width, height), area, prefer_below)
            # Native positioning also handles monitors with negative coordinates.
            self.root.geometry(f"{width}x{height}")
            self.root.attributes("-alpha", 0.0)
            # Windows owns the region after a successful SetWindowRgn call.
            rad = round(32 * self._scale)
            region = gdi32.CreateRoundRectRgn(0, 0, width + 1, height + 1, rad, rad)
            if region and not user32.SetWindowRgn(self._hwnd, region, True):
                gdi32.DeleteObject(region)
            self.root.deiconify()
            user32.SetWindowPos(
                self._hwnd, -1, x, y, width, height, 0x0010 | 0x0040)
            self._animate(1.0, self.fade_in_ms)
            if self._hide_timer is not None:
                self.root.after_cancel(self._hide_timer)
            hold_ms = max(self.fade_in_ms, self.duration_ms - self.fade_out_ms)
            self._hide_timer = self.root.after(hold_ms, self._fade_out)
        self.root.after(40, self._poll)

    def run(self):
        self.root.mainloop()
