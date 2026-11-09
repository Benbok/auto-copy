import array
import io
import threading
import time
import unittest
import wave
from unittest.mock import patch

import feedback
from feedback import CopyPopup, make_soft_sound


class FeedbackTests(unittest.TestCase):
    def test_sound_is_short_quiet_and_has_no_abrupt_edges(self):
        with wave.open(io.BytesIO(make_soft_sound()), "rb") as wav:
            self.assertEqual(wav.getnchannels(), 1)
            self.assertEqual(wav.getsampwidth(), 2)
            self.assertGreater(wav.getnframes() / wav.getframerate(), 0.3)
            self.assertLessEqual(wav.getnframes() / wav.getframerate(), 0.6)
            samples = array.array("h", wav.readframes(wav.getnframes()))
        self.assertGreater(max(samples), 100)
        self.assertLess(max(abs(s) for s in samples), 4000)
        self.assertLess(abs(samples[0]), 10)
        self.assertLess(abs(samples[-1]), 10)
        # A bell rings out: its early body is louder than its tail.
        early = samples[2205:6615]
        tail = samples[-4410:]
        self.assertGreater(sum(s * s for s in early), 4 * sum(s * s for s in tail))

    def setUp(self):
        self.popup = CopyPopup()
        self.addCleanup(self.close_popup)

    def close_popup(self):
        for timer in self.popup.root.tk.call("after", "info"):
            self.popup.root.after_cancel(timer)
        self.popup.root.destroy()

    def pump(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.popup.root.update()
            time.sleep(0.01)

    def test_burst_shows_only_latest_copy_in_same_window_without_focus(self):
        import ctypes
        before = ctypes.windll.user32.GetForegroundWindow()
        window = self.popup.root.winfo_id()
        self.popup.show()
        self.pump(0.1)
        for i in range(50):
            self.popup.show()
        self.pump(0.15)
        self.assertEqual(self.popup.title.cget("text"), "Текст скопирован")
        self.assertEqual(self.popup.root.winfo_id(), window)
        self.assertEqual(ctypes.windll.user32.GetForegroundWindow(), before)

    def test_copy_worker_can_update_popup(self):
        worker = threading.Thread(target=self.popup.show)
        worker.start()
        worker.join(timeout=1)
        self.pump(0.15)
        self.assertFalse(worker.is_alive())
        self.assertTrue(self.popup.root.winfo_ismapped())

    def test_popup_has_clipped_round_corners(self):
        import ctypes
        from ctypes import wintypes as wt
        gdi32 = ctypes.windll.gdi32
        gdi32.CreateRectRgn.restype = wt.HANDLE
        gdi32.PtInRegion.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_int]
        gdi32.DeleteObject.argtypes = [wt.HANDLE]
        user32 = ctypes.windll.user32
        user32.GetWindowRgn.argtypes = [wt.HWND, wt.HANDLE]
        self.popup.show()
        self.pump(0.15)
        region = gdi32.CreateRectRgn(0, 0, 0, 0)
        try:
            self.assertNotEqual(user32.GetWindowRgn(self.popup._hwnd, region), 0)
            self.assertFalse(gdi32.PtInRegion(region, 0, 0))
            self.assertTrue(gdi32.PtInRegion(region, 100, 30))
        finally:
            gdi32.DeleteObject(region)

    def test_new_copy_restarts_hide_timer(self):
        self.popup.duration_ms = 250
        self.popup.show()
        self.pump(0.18)
        self.popup.show()
        self.pump(0.18)
        self.assertTrue(self.popup.root.winfo_ismapped())
        self.pump(0.25)
        self.assertFalse(self.popup.root.winfo_ismapped())

    def test_disabling_notifications_discards_pending_popup(self):
        self.popup.show()
        self.popup.hide()
        self.pump(0.15)
        self.assertFalse(self.popup.root.winfo_ismapped())

    def test_popup_uses_selection_endpoint_and_stays_there(self):
        with patch.object(feedback, "monitor_work_area", return_value=(0, 0, 1920, 1080)):
            self.popup.show((400, 300))
            self.pump(0.18)
            self.assertEqual(self.popup.root.winfo_x(), 428)
            self.assertEqual(self.popup.root.winfo_y(), 300 - self.popup.root.winfo_height() // 2)
            self.pump(0.1)
            self.assertEqual(self.popup.root.winfo_x(), 428)

    def test_fades_in_then_out_and_new_copy_cancels_old_fade(self):
        self.popup.duration_ms = 600
        self.popup.show((400, 300))
        self.pump(0.07)
        self.assertGreater(float(self.popup.root.attributes("-alpha")), 0)
        self.assertLess(float(self.popup.root.attributes("-alpha")), 1)
        self.pump(0.18)
        self.assertAlmostEqual(float(self.popup.root.attributes("-alpha")), 1)
        self.pump(0.25)
        self.assertLess(float(self.popup.root.attributes("-alpha")), 1)
        self.popup.show((500, 400))
        self.pump(0.25)
        self.assertTrue(self.popup.root.winfo_ismapped())
        self.assertAlmostEqual(float(self.popup.root.attributes("-alpha")), 1)
        self.pump(0.5)
        self.assertFalse(self.popup.root.winfo_ismapped())

    def test_default_theme_is_dark(self):
        self.assertEqual(self.popup.panel.cget("bg"), "#202024")
        self.assertEqual(self.popup.title.cget("fg"), "#ffffff")

    def test_set_theme_light_updates_colors(self):
        self.popup.set_theme("light")
        self.pump(0.1)
        self.assertEqual(self.popup.panel.cget("bg"), "#ffffff")
        self.assertEqual(self.popup.title.cget("fg"), "#18181b")

    def test_resolve_theme_system(self):
        self.assertIn(feedback.resolve_theme("system"), ("dark", "light"))
        self.assertEqual(feedback.resolve_theme("dark"), "dark")
        self.assertEqual(feedback.resolve_theme("light"), "light")


class PlacementTests(unittest.TestCase):
    def test_prefers_right_then_below_and_stays_inside_monitor(self):
        for anchor, area, expected in [
            ((400, 300), (0, 0, 1920, 1080), (428, 270)),
            ((1584, 300), (0, 0, 1920, 1080), (1612, 270)),
            ((1585, 300), (0, 0, 1920, 1080), (1435, 332)),
            ((1900, 300), (0, 0, 1920, 1080), (1612, 332)),
            ((1900, 1070), (0, 0, 1920, 1080), (1612, 978)),
            ((-500, -200), (-1920, -1080, 0, 0), (-472, -230)),
            ((-5, -5), (-1920, -1080, 0, 0), (-308, -97)),
        ]:
            with self.subTest(anchor=anchor):
                self.assertEqual(feedback.popup_position(anchor, (300, 60), area), expected)

    def test_mouse_release_coordinates_are_queued_for_copy(self):
        import queue
        from auto_copy import AutoCopy, DEFAULT_CONFIG, mouse
        app = AutoCopy.__new__(AutoCopy)
        app.cfg = dict(DEFAULT_CONFIG)
        app.jobs = queue.Queue()
        app.last_click_time = 0
        app.last_click_pos = (0, 0)
        app.on_click(100, 100, mouse.Button.left, True)
        app.on_click(400, 300, mouse.Button.left, False)
        self.assertEqual(app.jobs.get_nowait(), ((400, 300), True))

    def release(self, start, end, recent_click=False, mode=None):
        import queue
        from auto_copy import AutoCopy, DEFAULT_CONFIG, mouse
        app = AutoCopy.__new__(AutoCopy)
        app.cfg = dict(DEFAULT_CONFIG)
        if mode:
            app.cfg["copy_direction"] = mode
        app.jobs = queue.Queue()
        app.last_click_time = time.monotonic() if recent_click else 0
        app.last_click_pos = end
        app.on_click(*start, mouse.Button.left, True)
        app.on_click(*end, mouse.Button.left, False)
        return app.jobs

    def test_reverse_drags_are_not_copied(self):
        for start, end in [
            ((700, 300), (400, 300)),  # right to left on one line
            ((700, 300), (400, 310)),  # same line, small vertical wobble
            ((700, 500), (400, 300)),  # upward multiline
            ((400, 500), (700, 300)),  # upward, even if it ends to the right
        ]:
            with self.subTest(start=start, end=end):
                self.assertTrue(self.release(start, end).empty())

    def test_forward_drags_are_copied(self):
        for start, end in [((400, 300), (700, 300)), ((400, 300), (700, 305)),
                           ((700, 300), (400, 500))]:
            with self.subTest(start=start, end=end):
                self.assertFalse(self.release(start, end).empty())

    def test_right_to_left_mode_copies_only_reverse_drags(self):
        for start, end, copied in [
            ((700, 300), (400, 300), True), ((700, 500), (400, 300), True),
            ((400, 300), (700, 300), False), ((400, 300), (700, 500), False),
        ]:
            with self.subTest(start=start, end=end):
                self.assertEqual(
                    not self.release(start, end, mode="right_to_left").empty(), copied)

    def test_any_mode_copies_every_direction(self):
        for start, end in [((700, 300), (400, 300)), ((400, 300), (700, 300)),
                           ((700, 500), (400, 300)), ((400, 300), (700, 500))]:
            with self.subTest(start=start, end=end):
                self.assertFalse(self.release(start, end, mode="any").empty())

    def test_unknown_mode_falls_back_to_left_to_right(self):
        self.assertTrue(self.release((700, 300), (400, 300), mode="bogus").empty())
        self.assertFalse(self.release((400, 300), (700, 300), mode="bogus").empty())

    def test_double_click_is_copied_in_every_mode(self):
        for mode in ("left_to_right", "right_to_left", "any"):
            with self.subTest(mode=mode):
                self.assertFalse(
                    self.release((100, 75), (100, 75), recent_click=True, mode=mode).empty())

    def test_word_selection_is_placed_below_even_when_right_has_space(self):
        self.assertEqual(feedback.popup_position((100, 75), (240, 60),
                                                (0, 0, 1920, 1080), prefer_below=True),
                         (8, 107))

    def test_double_click_is_copied_and_placed_below(self):
        self.assertEqual(self.release((100, 75), (100, 75), recent_click=True).get_nowait(),
                         ((100, 75), True))

    def test_rect_popup_goes_right_of_selection_end_without_overlap(self):
        area = (0, 0, 1920, 1080)
        rect = (300, 200, 500, 220)  # last line of the selection
        x, y = feedback.rect_popup_position(rect, (240, 40), area)
        self.assertEqual(x, 500 + feedback.RECT_GAP)
        self.assertEqual(y, 210 - 20)

    def test_rect_popup_moves_below_end_when_right_edge_is_full(self):
        area = (0, 0, 1920, 1080)
        rect = (1500, 200, 1800, 220)
        x, y = feedback.rect_popup_position(rect, (240, 40), area)
        self.assertGreaterEqual(y, 220)  # does not cover the selection
        self.assertLessEqual(x + 240, 1920 - 8)

    def test_selection_end_rect_uses_last_rectangle(self):
        import selection
        flat = [10, 20, 100, 18, 10, 40, 60, 18]
        self.assertEqual(selection.last_rect(flat), (10, 40, 70, 58))
        self.assertIsNone(selection.last_rect([]))
        self.assertEqual(selection.last_rect([10, 20, 0, 18, 10, 40, 60, 18]),
                         (10, 40, 70, 58))
        self.assertIsNone(selection.last_rect([10, 20, 0, 18]))


if __name__ == "__main__":
    unittest.main()
