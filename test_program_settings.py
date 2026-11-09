import copy
from contextlib import nullcontext
from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

import auto_copy
from program_settings import ProgramSettings, updated_exclusions


class ExclusionTests(unittest.TestCase):
    def test_exclude_normalizes_path_and_deduplicates_without_mutation(self):
        original = ["cmd.exe", "Chrome.EXE"]
        self.assertEqual(updated_exclusions(original, [r'C:\Apps\CHROME.exe', "Notepad.exe"], True),
                         ["chrome.exe", "cmd.exe", "notepad.exe"])
        self.assertEqual(original, ["cmd.exe", "Chrome.EXE"])

    def test_allow_removes_only_requested_program(self):
        self.assertEqual(updated_exclusions(["cmd.exe", "chrome.exe"], ["CHROME.EXE"], False),
                         ["cmd.exe"])

    def test_invalid_names_do_not_change_exclusions(self):
        for name in ("", "*.exe", "chrome", "file.txt"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                updated_exclusions(["cmd.exe"], [name], True)

    def test_changes_are_saved_and_applied_immediately(self):
        app = auto_copy.AutoCopy.__new__(auto_copy.AutoCopy)
        app.cfg = copy.deepcopy(auto_copy.DEFAULT_CONFIG)
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(auto_copy, "CONFIG_DIR", directory), patch.object(
                    auto_copy, "CONFIG_PATH", str(Path(directory) / "config.json")):
                app.set_programs_excluded([r"C:\Apps\Example.exe"], True)
                self.assertIn("example.exe", app.cfg["excluded_processes"])
                self.assertIn("example.exe", auto_copy.load_config()["excluded_processes"])
                app.set_programs_excluded(["Example.exe"], False)
                self.assertNotIn("example.exe", auto_copy.load_config()["excluded_processes"])
                self.assertIn("cmd.exe", app.cfg["excluded_processes"])

    def test_save_failure_keeps_previous_settings(self):
        app = auto_copy.AutoCopy.__new__(auto_copy.AutoCopy)
        app.cfg = copy.deepcopy(auto_copy.DEFAULT_CONFIG)
        before = copy.deepcopy(app.cfg)
        with patch.object(auto_copy, "save_config", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                app.set_programs_excluded(["example.exe"], True)
        self.assertEqual(app.cfg, before)

    def test_excluded_program_does_not_send_copy_shortcut(self):
        app = auto_copy.AutoCopy.__new__(auto_copy.AutoCopy)
        app.cfg = dict(auto_copy.DEFAULT_CONFIG, excluded_processes=["chrome.exe"])
        with patch.object(auto_copy, "modifiers_held", return_value=False), patch.object(
                auto_copy, "foreground_process_name", return_value="chrome.exe"), patch.object(
                auto_copy, "foreground_process_id", return_value=-1), patch.object(
                auto_copy, "clipboard_seq") as clipboard:
            app.try_copy()
        clipboard.assert_not_called()

    def test_allowed_program_still_copies_after_an_exclusion_is_removed(self):
        app = auto_copy.AutoCopy.__new__(auto_copy.AutoCopy)
        app.cfg = dict(auto_copy.DEFAULT_CONFIG, excluded_processes=updated_exclusions(
            ["cmd.exe", "chrome.exe"], ["chrome.exe"], False))
        app.kb = Mock()
        app.kb.pressed.return_value = nullcontext()
        app.announce = Mock()
        with patch.object(auto_copy, "foreground_process_id", return_value=-1), patch.object(
                auto_copy, "foreground_process_name", return_value="chrome.exe"), patch.object(
                auto_copy, "modifiers_held", return_value=False), patch.object(
                auto_copy, "clipboard_seq", side_effect=[1, 2]), patch.object(
                auto_copy.time, "sleep"), patch.object(auto_copy.pyperclip, "paste", return_value="copied"), patch.object(
                auto_copy, "selection_end_rect", return_value=None):
            app.try_copy((400, 300))
        app.kb.press.assert_called_once()
        app.announce.assert_called_once_with("copied", (400, 300), False, None)

    def test_settings_window_is_never_sent_a_copy_shortcut(self):
        app = auto_copy.AutoCopy.__new__(auto_copy.AutoCopy)
        with patch.object(auto_copy, "foreground_process_id", return_value=auto_copy.os.getpid()), patch.object(
                auto_copy, "clipboard_seq") as clipboard:
            app.try_copy()
        clipboard.assert_not_called()


class SettingsWindowTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.excluded = ["cmd.exe"]

        def change(names, excluded):
            self.excluded = updated_exclusions(self.excluded, names, excluded)

        self.settings = ProgramSettings(self.root, lambda: self.excluded, change,
                                        process_provider=lambda: ["chrome.exe", "notepad.exe"])
        self.addCleanup(self.close)
        self.pump()

    def close(self):
        self.settings.close()
        self.root.destroy()

    def pump(self):
        for _ in range(12):
            self.root.update()
            time.sleep(0.01)

    def test_running_and_excluded_apps_can_be_excluded_and_allowed(self):
        self.assertEqual(set(self.settings.table.get_children()),
                         {"cmd.exe", "chrome.exe", "notepad.exe"})
        self.settings.table.selection_set("chrome.exe")
        self.settings.change_selected(True)
        self.assertIn("chrome.exe", self.excluded)
        self.assertEqual(self.settings.table.set("chrome.exe", "status"), "Исключена")
        self.settings.table.selection_set("chrome.exe")
        self.settings.change_selected(False)
        self.assertNotIn("chrome.exe", self.excluded)
        self.assertEqual(self.settings.table.set("chrome.exe", "status"), "Разрешена")

    def test_manually_adding_an_inactive_app_and_filtering(self):
        self.settings.program_name.set("Example.EXE")
        self.settings.exclude_entered()
        self.assertIn("example.exe", self.excluded)
        self.settings.search.set("EXAMPLE")
        self.assertEqual(self.settings.table.get_children(), ("example.exe",))

    def test_bottom_controls_keep_full_height_at_minimum_window_size(self):
        width, height = self.settings.window.minsize()
        self.settings.window.geometry(f"{width}x{height}")
        self.pump()

        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)

        controls = [widget for widget in descendants(self.settings.window)
                    if (widget.winfo_class() == "TEntry" and
                        str(widget.cget("textvariable")) == str(self.settings.program_name))
                    or (widget.winfo_class() == "TButton" and
                        widget.cget("text") in ("Исключить", "Выбрать .exe…"))]
        self.assertEqual(len(controls), 3)
        for widget in controls:
            with self.subTest(widget=widget.winfo_class()):
                self.assertTrue(widget.winfo_ismapped())
                self.assertGreaterEqual(widget.winfo_height(), widget.winfo_reqheight())
                self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(),
                                     self.settings.window.winfo_rooty() + self.settings.window.winfo_height())

    def click_heading(self, column):
        self.root.tk.call(self.settings.table.heading(column, "command"))

    def test_program_heading_toggles_alphabetical_order_and_keeps_selection(self):
        self.settings.table.selection_set("chrome.exe")
        self.click_heading("program")
        self.assertEqual(self.settings.table.get_children(),
                         ("notepad.exe", "cmd.exe", "chrome.exe"))
        self.assertEqual(self.settings.table.selection(), ("chrome.exe",))
        self.assertIn("▼", self.settings.table.heading("program", "text"))
        self.click_heading("program")
        self.assertEqual(self.settings.table.get_children(),
                         ("chrome.exe", "cmd.exe", "notepad.exe"))
        self.assertIn("▲", self.settings.table.heading("program", "text"))

    def test_status_heading_toggles_groups(self):
        self.click_heading("status")
        self.assertEqual(self.settings.table.get_children(),
                         ("cmd.exe", "chrome.exe", "notepad.exe"))
        self.click_heading("status")
        self.assertEqual(self.settings.table.get_children(),
                         ("chrome.exe", "notepad.exe", "cmd.exe"))

    def test_sort_is_reapplied_after_filter_refresh_and_status_change(self):
        self.click_heading("status")
        self.settings.table.selection_set("notepad.exe")
        self.settings.change_selected(True)
        self.assertEqual(self.settings.table.get_children(),
                         ("cmd.exe", "notepad.exe", "chrome.exe"))
        self.settings.search.set("chrome")
        self.assertEqual(self.settings.table.get_children(), ("chrome.exe",))
        self.settings.search.set("")
        self.settings.refresh()
        self.pump()
        self.assertEqual(self.settings.table.get_children(),
                         ("cmd.exe", "notepad.exe", "chrome.exe"))


if __name__ == "__main__":
    unittest.main()
