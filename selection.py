"""Locate the end of the current text selection via UI Automation (Windows)."""

_uia = None
_failed = False


def last_rect(flat):
    """(left, top, right, bottom) of the last non-empty rect in [l, t, w, h, ...]."""
    rects = [flat[i:i + 4] for i in range(0, len(flat) - len(flat) % 4, 4)]
    for left, top, width, height in reversed(rects):
        if width > 0 and height > 0:
            return (round(left), round(top), round(left + width), round(top + height))
    return None


def _init():
    global _uia
    import comtypes
    import comtypes.client
    comtypes.CoInitialize()
    comtypes.client.GetModule("UIAutomationCore.dll")
    from comtypes.gen import UIAutomationClient as UIA
    _uia = (UIA, comtypes.CoCreateInstance(
        UIA.CUIAutomation._reg_clsid_, interface=UIA.IUIAutomation,
        clsctx=comtypes.CLSCTX_INPROC_SERVER))


def selection_end_rect():
    """Screen rect of the last line of the selection in the focused control.

    Returns None when the app does not expose a text selection; callers
    should then fall back to the mouse position. Call from one thread only.
    """
    global _failed
    if _failed:
        return None
    try:
        if _uia is None:
            _init()
        UIA, automation = _uia
        element = automation.GetFocusedElement()
        pattern = element.GetCurrentPattern(UIA.UIA_TextPatternId)
        if not pattern:
            return None
        ranges = pattern.QueryInterface(UIA.IUIAutomationTextPattern).GetSelection()
        if ranges.Length == 0:
            return None
        flat = ranges.GetElement(ranges.Length - 1).GetBoundingRectangles()
        return last_rect(list(flat or ()))
    except ImportError:
        _failed = True  # comtypes missing: stay on the mouse fallback
        return None
    except Exception:
        return None
