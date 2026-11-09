"""Program exclusions and their settings window."""

import ctypes
from ctypes import wintypes as wt
import ntpath
import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import psutil

from app_icon import set_window_icon


def process_name(value):
    name = ntpath.basename(value.strip().strip('"')).casefold()
    if (not name.endswith(".exe") or name == ".exe"
            or any(char in name for char in '<>:"/\\|?*')):
        raise ValueError("Укажите имя программы с расширением .exe, например chrome.exe.")
    return name


def updated_exclusions(current, names, excluded):
    selected = {process_name(name) for name in names}
    result = {name.casefold() for name in current}
    return sorted(result | selected if excluded else result - selected)


def running_programs():
    """Show programs with visible windows, avoiding a list of system services."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wt.LPARAM]
    user32.IsWindowVisible.argtypes = [wt.HWND]
    user32.GetWindowTextLengthW.argtypes = [wt.HWND]
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    names = set()

    @callback_type
    def visit(hwnd, _):
        if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd):
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value and pid.value != os.getpid():
                try:
                    names.add(process_name(psutil.Process(pid.value).name()))
                except (psutil.Error, ValueError):
                    pass
        return True

    if not user32.EnumWindows(visit, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return sorted(names)


class ProgramSettings:
    def __init__(self, parent, get_excluded, on_change, process_provider=running_programs):
        self.get_excluded = get_excluded
        self.on_change = on_change
        self.process_provider = process_provider
        self._programs = []
        self._results = queue.Queue()
        self._loading = False
        self._poll_timer = None
        self._sort_column = "program"
        self._sort_descending = False
        self.window = tk.Toplevel(parent)
        self.window.title("AutoCopy — Настройки программ")
        set_window_icon(self.window)
        self.window.geometry("680x550")
        self.window.minsize(590, 480)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        body = ttk.Frame(self.window, padding=20)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(4, weight=1, minsize=100)
        ttk.Label(body, text="Где работает AutoCopy", font=("Segoe UI", 16, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(body, text="Автокопирование работает во всех программах, кроме исключённых.\n"
                            "Выделите программу и выберите действие.").grid(row=1, column=0, sticky="w", pady=(6, 14))

        toolbar = ttk.Frame(body)
        toolbar.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        self.search = tk.StringVar()
        search = ttk.Entry(toolbar, textvariable=self.search)
        search.pack(side="left", fill="x", expand=True)
        ttk.Button(toolbar, text="Обновить список", command=self.refresh).pack(side="left", padx=(8, 0))
        ttk.Label(body, text="Поиск по имени программы · в списке открытые приложения и исключения",
                  foreground="#606060").grid(row=3, column=0, sticky="w", pady=(0, 8))

        listing = ttk.Frame(body)
        listing.grid(row=4, column=0, sticky="nsew")
        self.table = ttk.Treeview(listing, columns=("program", "status"), show="headings",
                                  selectmode="extended", height=10)
        self.table.heading("program", text="Программа", command=lambda: self.sort_by("program"))
        self.table.heading("status", text="Автокопирование", command=lambda: self.sort_by("status"))
        self.table.column("program", width=350, minwidth=200)
        self.table.column("status", width=150, minwidth=120)
        scrollbar = ttk.Scrollbar(listing, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scrollbar.set)
        self.table.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        actions = ttk.Frame(body)
        actions.grid(row=5, column=0, sticky="ew", pady=10)
        ttk.Button(actions, text="Исключить выбранные", command=lambda: self.change_selected(True)).pack(side="left")
        ttk.Button(actions, text="Разрешить выбранные", command=lambda: self.change_selected(False)).pack(side="left", padx=8)
        ttk.Separator(body).grid(row=6, column=0, sticky="ew", pady=(2, 10))
        ttk.Label(body, text="Программа не запущена? Укажите имя или выберите её .exe:").grid(row=7, column=0, sticky="w")
        manual = ttk.Frame(body)
        manual.grid(row=8, column=0, sticky="ew", pady=(6, 0))
        self.program_name = tk.StringVar()
        entry = ttk.Entry(manual, textvariable=self.program_name)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda event: self.exclude_entered())
        ttk.Button(manual, text="Исключить", command=self.exclude_entered).pack(side="left", padx=8)
        ttk.Button(manual, text="Выбрать .exe…", command=self.browse).pack(side="left")
        self.status = tk.StringVar(value="")
        ttk.Label(body, textvariable=self.status, wraplength=600).grid(row=9, column=0, sticky="w", pady=(12, 0))
        self.search.trace_add("write", lambda *_: self.render())
        self.render()
        self.refresh()
        self.window.update_idletasks()
        min_width = max(590, self.window.winfo_reqwidth())
        min_height = max(480, self.window.winfo_reqheight() - listing.winfo_reqheight() + 100)
        self.window.minsize(min_width, min_height)
        self.window.geometry(f"{max(680, min_width)}x{max(550, min_height)}")
        self._poll_timer = self.window.after(60, self._poll)

    def sort_by(self, column):
        self._sort_descending = not self._sort_descending if column == self._sort_column else False
        self._sort_column = column
        self.render()

    def render(self):
        selected = set(self.table.selection())
        excluded = {name.casefold() for name in self.get_excluded()}
        names = sorted(set(self._programs) | excluded)
        if self._sort_column == "status":
            # Keep names alphabetical within each status group in both directions.
            names.sort(key=lambda name: name not in excluded, reverse=self._sort_descending)
        elif self._sort_descending:
            names.reverse()
        for column, label in (("program", "Программа"), ("status", "Автокопирование")):
            arrow = (" ▼" if self._sort_descending else " ▲") if column == self._sort_column else ""
            self.table.heading(column, text=label + arrow)
        query = self.search.get().strip().casefold()
        self.table.delete(*self.table.get_children())
        for name in names:
            if query in name:
                self.table.insert("", "end", iid=name, values=(
                    name, "Исключена" if name in excluded else "Разрешена"))
                if name in selected:
                    self.table.selection_add(name)

    def refresh(self):
        if self._loading:
            return
        self._loading = True
        self.status.set("Обновляю список открытых программ…")

        def collect():
            try:
                self._results.put((self.process_provider(), None))
            except Exception as error:
                self._results.put((None, error))

        threading.Thread(target=collect, daemon=True).start()

    def _poll(self):
        try:
            programs, error = self._results.get_nowait()
        except queue.Empty:
            pass
        else:
            self._loading = False
            if error is None:
                self._programs = programs
                self.render()
                self.status.set("Изменения сохраняются автоматически и действуют сразу.")
            else:
                self.status.set("Не удалось получить список программ. Можно выбрать .exe вручную.")
        self._poll_timer = self.window.after(60, self._poll)

    def change(self, names, excluded):
        try:
            self.on_change(names, excluded)
        except (ValueError, OSError) as error:
            messagebox.showerror("Не удалось сохранить настройки", str(error), parent=self.window)
            return False
        self.render()
        self.status.set("Сохранено. Программы исключены." if excluded else
                        "Сохранено. Автокопирование в выбранных программах разрешено.")
        return True

    def change_selected(self, excluded):
        names = self.table.selection()
        if not names:
            self.status.set("Сначала выделите программу в списке.")
            return
        self.change(names, excluded)

    def exclude_entered(self):
        if self.change([self.program_name.get()], True):
            self.program_name.set("")

    def browse(self):
        path = filedialog.askopenfilename(parent=self.window, title="Выберите программу для исключения",
                                          filetypes=[("Программы Windows", "*.exe")])
        if path:
            self.change([path], True)

    def show(self):
        self.window.deiconify()
        self.window.lift()
        self.window.focus_force()
        self.render()

    def close(self):
        if self.window.winfo_exists():
            if self._poll_timer is not None:
                self.window.after_cancel(self._poll_timer)
                self._poll_timer = None
            self.window.destroy()
