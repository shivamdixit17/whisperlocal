#!/usr/bin/env python3
"""
WhisperLocal — push-to-talk local dictation for macOS.

Hold a trigger key → record → transcribe with mlx-whisper → paste at cursor.

Start it with the `whisperlocal` command; see whisperlocal/cli.py.
"""

from __future__ import annotations

import json
import datetime
import os
import subprocess
import sys
import threading
import time

from whisperlocal import config as cfg
from whisperlocal import keymap
from whisperlocal.config import FN_KEY, Settings
from whisperlocal.settings_manager import SettingsManager, Tier

# ─── Imports with friendly error messages ────────────────────────────────────────


def _check_import(module_name: str, pip_name: str | None = None):
    """Import a module, or explain how to get it and stop."""
    try:
        return __import__(module_name)
    except ImportError:
        pip_name = pip_name or module_name
        print(f"Error: missing package {module_name}")
        print(f"   Install with: pip install {pip_name}")
        print("   Or reinstall WhisperLocal: uv tool install --force whisperlocal")
        sys.exit(1)


_check_import("numpy")
_check_import("sounddevice")
_check_import("soundfile")
pyperclip = _check_import("pyperclip")
rumps = _check_import("rumps")
_check_import("pynput.keyboard", "pynput")

# Controller is deliberately not imported — constructing one reaches HIToolbox
# Text Services and crashes off the main thread. See paste_text().
from pynput.keyboard import Key, Listener  # noqa: E402

# ─── Cocoa imports for the menu bar icon and overlay ─────────────────────────────
try:
    import AppKit
    from AppKit import (
        NSBackingStoreBuffered,
        NSColor,
        NSFloatingWindowLevel,
        NSFontWeightRegular,
        NSImage,
        NSImageSymbolConfiguration,
        NSMakePoint,
        NSMakeRect,
        NSScreen,
        NSView,
        NSWindow,
        NSWindowStyleMaskBorderless,
    )

    HAS_COCOA = True
except ImportError:  # pragma: no cover - only on a broken PyObjC install
    HAS_COCOA = False
    print("Warning: PyObjC not available — overlay and icons disabled")

# Quartz synthesizes the Cmd+V keystroke directly. See paste_text().
try:
    from Quartz import (
        CGEventCreateKeyboardEvent,
        CGEventPost,
        CGEventSetFlags,
        CGEventSourceCreate,
        kCGEventFlagMaskCommand,
        kCGEventSourceStateHIDSystemState,
        kCGHIDEventTap,
    )

    HAS_QUARTZ = True
except ImportError:  # pragma: no cover
    HAS_QUARTZ = False
    print("Warning: Quartz not available — pasting disabled")

# Accessibility, used to find where the text cursor is so the recording dot can
# sit next to it instead of in a fixed corner.
try:
    from ApplicationServices import (
        AXUIElementCopyAttributeValue,
        AXUIElementCopyParameterizedAttributeValue,
        AXUIElementCreateSystemWide,
        AXUIElementSetMessagingTimeout,
        AXValueGetValue,
        kAXBoundsForRangeParameterizedAttribute,
        kAXFocusedUIElementAttribute,
        kAXPositionAttribute,
        kAXSelectedTextRangeAttribute,
        kAXSizeAttribute,
        kAXValueTypeCGPoint,
        kAXValueTypeCGRect,
        kAXValueTypeCGSize,
    )

    HAS_AX = True
except ImportError:  # pragma: no cover
    HAS_AX = False

# Event-tap symbols, used to watch the Fn key. pynput has no Fn key at all, so
# the only way to trigger on it is to read the raw modifier flags.
try:
    from CoreFoundation import (
        CFMachPortInvalidate,
        CFRunLoopAddSource,
        CFRunLoopGetMain,
        CFRunLoopRemoveSource,
        kCFRunLoopCommonModes,
    )
    from Quartz import (
        CFMachPortCreateRunLoopSource,
        CGEventGetFlags,
        CGEventMaskBit,
        CGEventTapCreate,
        CGEventTapEnable,
        kCGEventFlagMaskSecondaryFn,
        kCGEventFlagsChanged,
        kCGEventTapDisabledByTimeout,
        kCGEventTapDisabledByUserInput,
        kCGEventTapOptionListenOnly,
        kCGHeadInsertEventTap,
        kCGSessionEventTap,
    )

    HAS_EVENT_TAP = True
except ImportError:  # pragma: no cover
    HAS_EVENT_TAP = False


# ─── Main-thread boundary ────────────────────────────────────────────────────────


def run_on_main(fn) -> None:
    """Run `fn` on the main thread.

    AppKit, HIToolbox and Text Services all assert they are on the main dispatch
    queue, and on macOS those assertions are fatal — calling them from a worker
    is what produced the SIGTRAP in the transcription thread
    (dispatch_assert_queue_fail via TSMGetInputSourceProperty). Every call into
    those frameworks goes through here, so there is exactly one place that has
    to be right.
    """
    if not HAS_COCOA or threading.current_thread() is threading.main_thread():
        fn()
        return

    from PyObjCTools import AppHelper

    AppHelper.callAfter(fn)


# ─── Icons ───────────────────────────────────────────────────────────────────────

_symbol_cache: dict = {}


def sf_symbol_image(name: str, settings: Settings):
    """Render an SF Symbol as an NSImage for the menu bar.

    With a colour configured, the glyph is tinted and template mode is turned
    OFF — a template image is drawn as a mask, so macOS would throw the colour
    away and redraw it black or white. The trade-off is that a fixed colour no
    longer follows the menu bar between light and dark mode, which is why the
    default orange is one that reads on both.

    Returns None if Cocoa is missing or the symbol name is not available on this
    macOS version; callers fall back to text.
    """
    if not HAS_COCOA:
        return None

    rgb = settings.icon_rgb
    point_size = settings.icon_point_size
    key = (name, point_size, rgb)
    if key in _symbol_cache:
        return _symbol_cache[key]

    image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
    if image is not None:
        conf = NSImageSymbolConfiguration.configurationWithPointSize_weight_scale_(
            point_size, NSFontWeightRegular, 1
        )

        if rgb:
            tint = NSColor.colorWithCalibratedRed_green_blue_alpha_(*rgb, 1.0)
            conf = conf.configurationByApplyingConfiguration_(
                NSImageSymbolConfiguration.configurationWithHierarchicalColor_(tint)
            )

        image = image.imageWithSymbolConfiguration_(conf)
        image.setTemplate_(not rgb)

    _symbol_cache[key] = image
    return image


# ─── Dictation history ───────────────────────────────────────────────────────────


class HistoryLog:
    """Appends one JSON object per dictation to a JSONL file.

    Failed attempts are recorded too, with the reason in `status`. That is the
    point of the log: how often the model hallucinates, and whether it tracks
    with short recordings or a particular app, is only visible if the failures
    are in there alongside the successes.

    A history write must never cost you a paste, so every error here is caught
    and reported rather than raised.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.path = settings.history_path
        self.enabled = settings.history_enabled
        self._lock = threading.Lock()
        self._warned = False

    def record(
        self,
        status: str,
        text: str | None = None,
        audio_seconds: float = 0.0,
        transcribe_ms: float | None = None,
        app: str | None = None,
        app_bundle_id: str | None = None,
        backend: str = "local",
        model: str | None = None,
    ) -> None:
        if not self.enabled:
            return

        words = len(text.split()) if text else 0
        entry = {
            "timestamp": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "status": status,
            # Statistics are kept either way; only the words themselves are
            # optional, so `history_text = false` still gives usable stats.
            "text": text if self.settings.history_text else None,
            "words": words,
            "chars": len(text) if text else 0,
            "audio_seconds": round(audio_seconds, 2),
            "transcribe_ms": round(transcribe_ms) if transcribe_ms is not None else None,
            "wpm": round(words / (audio_seconds / 60), 1) if words and audio_seconds > 0 else None,
            "app": app,
            "app_bundle_id": app_bundle_id,
            "model": model or self.settings.model_path,
            "backend": backend,
        }

        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as exc:
            # Warn once — a broken log should not spam the log it is breaking in.
            if not self._warned:
                self._warned = True
                print(f"Warning: could not write history to {self.path}: {exc}")


def frontmost_app() -> tuple[str | None, str | None]:
    """(name, bundle_id) of the app in front, or (None, None).

    Called from on_key_press, which with the Fn trigger already runs on the
    event tap's callback — i.e. the main thread — so this needs no marshalling.
    Measured at 0.001 ms, so it costs nothing on the key path.
    """
    if not HAS_COCOA:
        return None, None
    try:
        app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return None, None
        return app.localizedName(), app.bundleIdentifier()
    except Exception:
        return None, None


# ─── Finding the text cursor ─────────────────────────────────────────────────────

# How long to wait for an app to answer an Accessibility query. This runs on the
# key path — and with the Fn trigger, on the event tap's callback on the main
# runloop — so it must never block. Without a timeout, one busy or wedged app
# would hang the query indefinitely and macOS would disable the tap for being
# slow. A quarter second is far more than a healthy app needs, and anything
# slower falls back to the mouse pointer.
AX_TIMEOUT = 0.25

# Sanity bounds for a caret rectangle. A caret is zero-width by nature, so only
# the height is checked; anything outside this is a misreported element rather
# than a text cursor.
MIN_CARET_HEIGHT = 2
MAX_CARET_HEIGHT = 200


def _ax_attr(element, attribute):
    """Read one Accessibility attribute, or None."""
    try:
        err, value = AXUIElementCopyAttributeValue(element, attribute, None)
    except Exception:
        return None
    return None if err else value


def _ax_unpack(value, ax_type):
    """Turn an AXValue into a plain CoreGraphics struct, or None."""
    if value is None:
        return None
    try:
        ok, unpacked = AXValueGetValue(value, ax_type, None)
    except Exception:
        return None
    return unpacked if ok else None


def _focused_element_rect(element):
    """
    Bounding box of the focused control itself, in AX screen coordinates.

    The fallback for apps that expose a focused element but not a caret range —
    a text area still tells us roughly where to put the dot.
    """
    position = _ax_unpack(_ax_attr(element, kAXPositionAttribute), kAXValueTypeCGPoint)
    size = _ax_unpack(_ax_attr(element, kAXSizeAttribute), kAXValueTypeCGSize)
    if position is None or size is None:
        return None
    if size.height <= 0 or size.width <= 0:
        return None

    # Anchor to the top-left of the control rather than its centre: for a large
    # text area the top-left is where the text starts, and where the eye is.
    return (position.x, position.y, 0.0, min(size.height, MAX_CARET_HEIGHT))


def caret_screen_rect():
    """
    Where the text cursor is, as (x, y, width, height) in Accessibility screen
    coordinates — origin top-left, y increasing downward.

    Returns None when it cannot be determined, which is common: many Electron
    apps and some browser fields never implement these attributes. Callers fall
    back to the mouse pointer.
    """
    if not HAS_AX:
        return None

    try:
        system = AXUIElementCreateSystemWide()
        AXUIElementSetMessagingTimeout(system, AX_TIMEOUT)
    except Exception:
        return None

    element = _ax_attr(system, kAXFocusedUIElementAttribute)
    if element is None:
        return None

    try:
        AXUIElementSetMessagingTimeout(element, AX_TIMEOUT)
    except Exception:
        pass

    text_range = _ax_attr(element, kAXSelectedTextRangeAttribute)
    if text_range is None:
        return _focused_element_rect(element)

    try:
        err, bounds = AXUIElementCopyParameterizedAttributeValue(
            element, kAXBoundsForRangeParameterizedAttribute, text_range, None
        )
    except Exception:
        return _focused_element_rect(element)
    if err or bounds is None:
        return _focused_element_rect(element)

    rect = _ax_unpack(bounds, kAXValueTypeCGRect)
    if rect is None:
        return _focused_element_rect(element)

    height = rect.size.height
    if not (MIN_CARET_HEIGHT <= height <= MAX_CARET_HEIGHT):
        return _focused_element_rect(element)

    return (rect.origin.x, rect.origin.y, rect.size.width, height)


def mouse_screen_point():
    """The pointer, in Cocoa screen coordinates (origin bottom-left)."""
    if not HAS_COCOA:
        return None
    try:
        from AppKit import NSEvent

        point = NSEvent.mouseLocation()
        return (point.x, point.y)
    except Exception:
        return None


# ─── Fn key listener ─────────────────────────────────────────────────────────────


class FnKeyListener:
    """Watches the Fn (globe) key via a Quartz event tap.

    pynput cannot do this: Fn is not in its key enum and its macOS backend never
    looks at the flag. Fn is not a real keycode either — it only shows up as the
    kCGEventFlagMaskSecondaryFn bit on flagsChanged events, so the tap watches
    that bit go up and down.

    The tap is attached to the **main** runloop deliberately. macOS silently
    disables a tap whose callback is slow, so the callback does nothing but
    record a transition and hand off; all real work happens on worker threads.
    """

    def __init__(self, on_press, on_release):
        self.on_press = on_press
        self.on_release = on_release
        self._tap = None
        self._source = None
        self._down = False

    def _callback(self, proxy, event_type, event, refcon):
        # macOS disables the tap if it ever decides we are too slow; it tells us
        # by sending these instead of an event, and it stays dead until re-armed.
        if event_type in (kCGEventTapDisabledByTimeout, kCGEventTapDisabledByUserInput):
            print("Warning: Fn event tap was disabled by the system — re-enabling")
            CGEventTapEnable(self._tap, True)
            return event

        try:
            down = bool(CGEventGetFlags(event) & kCGEventFlagMaskSecondaryFn)
            if down != self._down:
                self._down = down
                (self.on_press if down else self.on_release)(FN_KEY)
        except Exception as exc:
            # Never let an exception escape into the tap callback.
            print(f"Warning: Fn tap callback error: {exc}")

        return event

    def start(self) -> bool:
        """Attach the tap. Must run on the main thread. True on success."""
        if not HAS_EVENT_TAP:
            print("Error: Quartz event tap unavailable — cannot watch the Fn key")
            return False

        self._tap = CGEventTapCreate(
            kCGSessionEventTap,
            kCGHeadInsertEventTap,
            kCGEventTapOptionListenOnly,  # observe only, never swallow the key
            CGEventMaskBit(kCGEventFlagsChanged),
            self._callback,
            None,
        )
        if not self._tap:
            print(
                "Error: could not create the Fn event tap — grant Input Monitoring "
                "to your terminal in System Settings → Privacy & Security"
            )
            return False

        self._source = CFMachPortCreateRunLoopSource(None, self._tap, 0)
        CFRunLoopAddSource(CFRunLoopGetMain(), self._source, kCFRunLoopCommonModes)
        CGEventTapEnable(self._tap, True)
        return True

    def stop(self) -> None:
        """Detach the tap. Must run on the main thread, like start()."""
        if self._tap is None:
            return
        try:
            CGEventTapEnable(self._tap, False)
            if self._source is not None:
                CFRunLoopRemoveSource(CFRunLoopGetMain(), self._source, kCFRunLoopCommonModes)
            CFMachPortInvalidate(self._tap)
        except Exception as exc:
            print(f"Warning: could not detach the Fn event tap cleanly: {exc}")
        finally:
            self._tap = None
            self._source = None
            self._down = False

    @property
    def active(self) -> bool:
        return self._tap is not None


# ─── Audio feedback ──────────────────────────────────────────────────────────────


class Sounds:
    """macOS system sounds for the four things that can happen."""

    START = "Tink"
    STOP = "Pop"
    DONE = "Glass"
    ERROR = "Basso"

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        # Set while a meeting is being recorded: the system-audio tap would
        # otherwise capture our own Tink and Pop.
        self.suppressed = False

    def play(self, name: str) -> None:
        if not self.enabled or self.suppressed:
            return
        path = f"/System/Library/Sounds/{name}.aiff"
        if os.path.exists(path):
            subprocess.Popen(
                ["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )

    def start(self) -> None:
        self.play(self.START)

    def stop(self) -> None:
        self.play(self.STOP)

    def done(self) -> None:
        self.play(self.DONE)

    def error(self) -> None:
        self.play(self.ERROR)


# ─── Engine pieces that live in their own modules ────────────────────────────────
# Re-exported so `from whisperlocal.app import Transcriber` keeps working.
from whisperlocal.audio import AudioRecorder  # noqa: E402
from whisperlocal.meetingdetect import DetectedMeeting, MeetingDetector  # noqa: E402
from whisperlocal.meetingrecorder import MeetingRecorder  # noqa: E402
from whisperlocal.meetings import MeetingStore  # noqa: E402
from whisperlocal.transcription.backends import BackendError, make_backend  # noqa: E402
from whisperlocal.transcription.local import Transcriber  # noqa: E402


# ─── Delivering the text ─────────────────────────────────────────────────────────

# Virtual keycode for "V" (kVK_ANSI_V). Virtual keycodes describe a physical key
# position, not the character printed on it, so this stays correct on QWERTZ and
# AZERTY layouts.
V_KEYCODE = 9


def _post_cmd_v() -> None:
    """Synthesize Cmd+V. Main thread only — call via run_on_main()."""
    source = CGEventSourceCreate(kCGEventSourceStateHIDSystemState)

    key_down = CGEventCreateKeyboardEvent(source, V_KEYCODE, True)
    key_up = CGEventCreateKeyboardEvent(source, V_KEYCODE, False)
    CGEventSetFlags(key_down, kCGEventFlagMaskCommand)
    CGEventSetFlags(key_up, kCGEventFlagMaskCommand)

    CGEventPost(kCGHIDEventTap, key_down)
    CGEventPost(kCGHIDEventTap, key_up)


def paste_text(text: str, paste_mode: str = "paste") -> bool:
    """Copy text to the clipboard and, in "paste" mode, paste it at the cursor.

    Deliberately does NOT use pynput's Controller. Constructing one calls
    get_unicode_to_keycode_map(), which reaches HIToolbox Text Services
    (TISCopyCurrentKeyboardInputSource / TSMGetInputSourceProperty) through
    ctypes. Those assert the main dispatch queue, so building a Controller on
    the transcription worker crashed the process with SIGTRAP. Posting the
    keystroke through Quartz skips the layout map entirely.
    """
    if not text:
        return False

    preview = text if len(text) <= 80 else text[:80] + "..."

    try:
        # pyperclip shells out to pbcopy — no AppKit involved, so this is safe
        # off the main thread, and keeping it here avoids blocking the runloop
        # for the settle delay below.
        pyperclip.copy(text)
    except Exception as exc:
        print(f"Error: could not copy to the clipboard: {exc}")
        return False

    if paste_mode == "clipboard":
        print(f'Copied: "{preview}"')
        return True

    if not HAS_QUARTZ:
        print("Error: paste unavailable, Quartz not imported")
        print("   The text is on your clipboard — press Cmd+V.")
        return False

    try:
        time.sleep(0.05)  # let the pasteboard settle before the keystroke
        run_on_main(_post_cmd_v)
        print(f'Pasted: "{preview}"')
        return True
    except Exception as exc:
        print(f"Error: paste failed: {exc}")
        print("   The text is on your clipboard — press Cmd+V.")
        return False


# ─── Hallucination guard ─────────────────────────────────────────────────────────


def looks_degenerate(text: str, settings: Settings) -> bool:
    """True if `text` looks like a decoder repetition loop rather than speech.

    Whisper gets stuck emitting one token over and over on short, noisy or
    half-captured audio — observed as "ARP ARP ARP...", "funny funny funny..."
    (223 words) and "On 25 25 25 25 25...". Two independent checks, because each
    catches what the other misses:

      * a long run of the same word back to back, which catches short loops
      * a low unique-word ratio, which catches long ones

    Tuned so real speech survives: "Hello, hello, hello." (a run of 3) and
    "no no no I really do not think that is right" (ratio 0.85) both pass.
    """
    words = [w.lower().strip(".,!?;:") for w in text.split()]
    if not words:
        return False

    run = best = 1
    for a, b in zip(words, words[1:]):
        run = run + 1 if a == b else 1
        best = max(best, run)
    if best >= settings.max_word_run:
        return True

    if len(words) < settings.repeat_min_words:
        return False
    return len(set(words)) / len(words) < settings.max_repeat_ratio


# ─── Screen geometry ─────────────────────────────────────────────────────────────
#
# Two coordinate systems meet here and they disagree about which way is up:
#
#   Accessibility  origin at the TOP-left of the primary screen, y grows DOWN
#   Cocoa windows  origin at the BOTTOM-left of the primary screen, y grows UP
#
# The flip is always against the *primary* screen's height, never the screen the
# point happens to be on — on a multi-monitor setup those differ, and using the
# wrong one puts the dot on the wrong display. These are kept as plain functions
# of their inputs so the arithmetic can be tested without a screen attached.


def ax_y_to_cocoa_y(ax_y: float, ax_height: float, primary_height: float) -> float:
    """Vertical centre of an AX rect, in Cocoa coordinates."""
    return primary_height - (ax_y + ax_height / 2.0)


def clamp_to_frame(x: float, y: float, size: float, frame) -> tuple[float, float]:
    """
    Keep a `size` x `size` window fully inside `frame`.

    frame is (origin_x, origin_y, width, height) in Cocoa coordinates, and may
    have a negative origin — a display arranged to the left of the primary one
    starts at a negative x.
    """
    fx, fy, fw, fh = frame
    x = min(max(x, fx), fx + fw - size)
    y = min(max(y, fy), fy + fh - size)
    return x, y


def _screens():
    """(frame, visible_frame) for each screen, as plain tuples."""
    if not HAS_COCOA:
        return []
    out = []
    for screen in NSScreen.screens():
        f, v = screen.frame(), screen.visibleFrame()
        out.append(
            (
                (f.origin.x, f.origin.y, f.size.width, f.size.height),
                (v.origin.x, v.origin.y, v.size.width, v.size.height),
            )
        )
    return out


def visible_frame_for_point(x: float, y: float):
    """The visible frame of the screen containing a Cocoa point, else the primary."""
    screens = _screens()
    if not screens:
        return None
    for frame, visible in screens:
        fx, fy, fw, fh = frame
        if fx <= x <= fx + fw and fy <= y <= fy + fh:
            return visible
    return screens[0][1]


def primary_screen_height() -> float:
    """Height of the primary screen — the origin for the AX/Cocoa flip."""
    screens = _screens()
    return screens[0][0][3] if screens else 0.0


# ─── Floating overlay ────────────────────────────────────────────────────────────


class FloatingOverlay:
    """
    A single small dot that follows your text cursor — the only visual feedback
    while recording or transcribing.

    Deliberately minimal: no panel, no text, no shadow. Red means recording,
    amber means transcribing. It breathes while recording so you can tell it is
    live, and holds steady while transcribing.

    It appears beside wherever you are actually typing. When the cursor cannot
    be located — plenty of apps never report it — it falls back to the mouse
    pointer, and finally to the bottom of the screen.
    """

    DOT_SIZE = 11
    # Fallback position: lifted clear of the Dock. Measured from the visible
    # area, which already excludes the Dock.
    BOTTOM_MARGIN = 54
    PULSE_INTERVAL = 0.5

    ALPHA_HIGH = 1.0
    ALPHA_LOW = 0.3

    RECORDING = (1.00, 0.23, 0.19)  # red
    TRANSCRIBING = (1.00, 0.72, 0.00)  # amber

    def __init__(self, settings: Settings):
        self.settings = settings
        self._window = None
        self._layer = None
        self._pulse_running = False
        self._pulse_thread: threading.Thread | None = None
        self._alpha_high = True
        if not settings.overlay or not HAS_COCOA:
            return
        self._build_window()

    # ── positioning ──────────────────────────────────────────────────────────

    def _bottom_origin(self) -> tuple[float, float] | None:
        """The fixed fallback: bottom-centre of the primary screen."""
        screens = _screens()
        if not screens:
            return None
        _, visible = screens[0]
        vx, vy, vw, _ = visible
        return (vx + (vw - self.DOT_SIZE) / 2.0, vy + self.BOTTOM_MARGIN)

    def _origin_for(self, caret_rect) -> tuple[float, float] | None:
        """
        Where the dot should sit, in Cocoa coordinates.

        Tries in order: the text cursor, the mouse pointer, the bottom of the
        screen. Which rungs are used depends on `overlay_anchor`.
        """
        anchor = self.settings.overlay_anchor
        dx = self.settings.overlay_offset_x
        dy = self.settings.overlay_offset_y
        half = self.DOT_SIZE / 2.0

        if anchor == "caret" and caret_rect:
            ax_x, ax_y, ax_w, ax_h = caret_rect
            x = ax_x + ax_w + dx
            y = ax_y_to_cocoa_y(ax_y, ax_h, primary_screen_height()) - half + dy
            frame = visible_frame_for_point(x, y)
            return clamp_to_frame(x, y, self.DOT_SIZE, frame) if frame else (x, y)

        if anchor in ("caret", "mouse"):
            point = mouse_screen_point()
            if point:
                # Below and right of the pointer, so it does not sit under the
                # arrow itself.
                x = point[0] + dx
                y = point[1] - self.DOT_SIZE - abs(dy or 4)
                frame = visible_frame_for_point(x, y)
                return clamp_to_frame(x, y, self.DOT_SIZE, frame) if frame else (x, y)

        return self._bottom_origin()

    def apply_settings(self, settings: Settings) -> None:
        """Pick up new settings. Anchor and offsets are read on every move, so
        only turning the overlay on or off needs work here."""
        self.settings = settings

        def _apply():
            if settings.overlay and HAS_COCOA and not self._window:
                self._build_window()
            elif not settings.overlay and self._window:
                self._stop_pulse()
                self._window.orderOut_(None)
                self._window = None
                self._layer = None

        run_on_main(_apply)

    def move_to(self, caret_rect=None) -> None:
        """Reposition the dot. Main thread only — call via run_on_main()."""
        if not self._window:
            return
        origin = self._origin_for(caret_rect)
        if origin:
            self._window.setFrameOrigin_(NSMakePoint(origin[0], origin[1]))

    def _build_window(self) -> None:
        """Create the borderless click-through dot window."""
        screen = NSScreen.mainScreen()
        if not screen:
            return

        # visibleFrame excludes the menu bar and Dock, so the dot never hides
        # behind either one.
        vf = screen.visibleFrame()
        size = self.DOT_SIZE
        x = vf.origin.x + (vf.size.width - size) / 2.0
        y = vf.origin.y + self.BOTTOM_MARGIN
        frame = NSMakeRect(x, y, size, size)

        self._window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False
        )
        self._window.setLevel_(NSFloatingWindowLevel + 1)  # above everything
        self._window.setOpaque_(False)
        self._window.setBackgroundColor_(NSColor.clearColor())
        self._window.setHasShadow_(False)
        self._window.setIgnoresMouseEvents_(True)  # click-through
        self._window.setCollectionBehavior_(1 << 0 | 1 << 4)  # all spaces + transient

        view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, size, size))
        view.setWantsLayer_(True)
        self._layer = view.layer()
        self._layer.setCornerRadius_(size / 2.0)
        self._layer.setMasksToBounds_(True)
        self._window.setContentView_(view)

        self._apply_color(self.RECORDING)

    def _apply_color(self, rgb) -> None:
        """Set the dot colour. Main thread only."""
        if not self._layer:
            return
        color = NSColor.colorWithCalibratedRed_green_blue_alpha_(*rgb, 1.0)
        self._layer.setBackgroundColor_(color.CGColor())

    def show_recording(self, caret_rect=None) -> None:
        """Show a breathing red dot, next to where the text will land."""
        if not self._window:
            return

        def _do_show():
            self.move_to(caret_rect)
            self._apply_color(self.RECORDING)
            self._window.setAlphaValue_(self.ALPHA_HIGH)
            self._window.orderFrontRegardless()
            self._start_pulse()

        run_on_main(_do_show)

    def show_transcribing(self) -> None:
        """Switch to a steady amber dot."""
        if not self._window:
            return

        def _do_update():
            self._apply_color(self.TRANSCRIBING)
            self._stop_pulse()

        run_on_main(_do_update)

    def hide(self) -> None:
        """Take the dot off screen."""
        if not self._window:
            return

        def _do_hide():
            self._stop_pulse()
            self._window.orderOut_(None)

        run_on_main(_do_hide)

    def _start_pulse(self) -> None:
        """Fade the dot in and out so it reads as live."""
        self._stop_pulse()
        self._pulse_running = True
        self._alpha_high = True

        def _pulse_loop():
            while self._pulse_running:
                time.sleep(self.PULSE_INTERVAL)
                if not self._pulse_running:
                    break

                def _toggle():
                    if not self._pulse_running or not self._window:
                        return
                    self._alpha_high = not self._alpha_high
                    self._window.setAlphaValue_(
                        self.ALPHA_HIGH if self._alpha_high else self.ALPHA_LOW
                    )

                try:
                    from PyObjCTools import AppHelper

                    AppHelper.callAfter(_toggle)
                except Exception:
                    pass

        self._pulse_thread = threading.Thread(target=_pulse_loop, daemon=True)
        self._pulse_thread.start()

    def _stop_pulse(self) -> None:
        """Stop pulsing and settle back to full opacity."""
        self._pulse_running = False

        def _restore():
            if self._window:
                self._window.setAlphaValue_(self.ALPHA_HIGH)
            self._alpha_high = True

        try:
            run_on_main(_restore)
        except Exception:
            pass


# ─── Trigger key resolution ──────────────────────────────────────────────────────


def resolve_trigger_tokens(settings: Settings) -> set:
    """
    Turn the configured key names into the tokens the listeners deliver.

    Three listeners feed the same engine and each reports differently: the Fn
    tap sends the string "fn", the mouse listener sends "mouse_left" and
    friends, and the keyboard listener sends a pynput Key member. All are
    hashable and compare cleanly, so the engine holds them in one set and treats
    every trigger the same regardless of which listener saw it.
    """
    tokens: set = set()
    for name in settings.trigger_keys:
        if name == FN_KEY or name in cfg.MOUSE_BUTTONS:
            tokens.add(name)
            continue
        key = getattr(Key, name, None)
        if key is None:  # pragma: no cover - guards against a pynput API change
            raise ValueError(f"This version of pynput does not expose the {name!r} key")
        tokens.add(key)
    return tokens


class MouseTriggerListener:
    """Watches mouse buttons, with a drag guard.

    Holding the left button is what dragging, selecting text and moving a window
    all do, so a plain press-and-hold trigger would fire constantly. This
    listener cancels the trigger as soon as the pointer travels further than
    `mouse_drag_cancel_px` from where it went down: a drag moves, someone
    holding still to talk does not.
    """

    def __init__(self, settings: Settings, on_press, on_release, on_cancel):
        self.settings = settings
        self.on_press = on_press
        self.on_release = on_release
        self.on_cancel = on_cancel
        self._listener = None
        self._down_token: str | None = None
        self._origin: tuple[int, int] | None = None

        from pynput import mouse

        self._mouse = mouse
        self._buttons = {
            mouse.Button.left: cfg.MOUSE_LEFT,
            mouse.Button.right: cfg.MOUSE_RIGHT,
            mouse.Button.middle: cfg.MOUSE_MIDDLE,
        }
        self._watched = {
            button: token
            for button, token in self._buttons.items()
            if token in settings.trigger_keys
        }

    def _on_click(self, x, y, button, pressed):
        token = self._watched.get(button)
        if token is None:
            return

        if pressed:
            self._down_token = token
            self._origin = (x, y)
            self.on_press(token)
        else:
            was_down, self._down_token, self._origin = self._down_token, None, None
            if was_down == token:
                self.on_release(token)

    def _on_move(self, x, y):
        """Cancel the trigger once the pointer has clearly started dragging."""
        limit = self.settings.mouse_drag_cancel_px
        if not limit or self._down_token is None or self._origin is None:
            return

        dx = x - self._origin[0]
        dy = y - self._origin[1]
        if (dx * dx + dy * dy) >= limit * limit:
            token, self._down_token, self._origin = self._down_token, None, None
            self.on_cancel(token)

    def start(self) -> bool:
        """Begin watching. Returns True if anything is actually being watched."""
        if not self._watched:
            return False
        self._listener = self._mouse.Listener(
            on_click=self._on_click, on_move=self._on_move
        )
        self._listener.daemon = True
        self._listener.start()
        return True

    def stop(self) -> None:
        if self._listener:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None
        self._down_token = None
        self._origin = None


class TriggerListeners:
    """The three trigger listeners as one unit, so the Settings page can swap
    the trigger keys without a restart.

    start(), stop() and restart() must run on the main thread: the Fn tap is
    attached to the main runloop, and pynput listeners are stopped from outside
    their own thread.
    """

    def __init__(self, engine: "DictationEngine"):
        self.engine = engine
        self.fn: FnKeyListener | None = None
        self.keyboard = None
        self.mouse: MouseTriggerListener | None = None
        self.settings: Settings | None = None

    def start(self, settings: Settings) -> bool:
        """Start every listener the config asks for. True if any started."""
        self.settings = settings
        engine = self.engine
        started_any = False

        if settings.uses_fn:
            self.fn = FnKeyListener(engine.on_key_press, engine.on_key_release)
            if self.fn.start():
                print("Fn listener started")
                started_any = True
            else:
                print("Error: could not start the Fn listener")
                self.fn = None

        if settings.pynput_keys:
            self.keyboard = Listener(on_press=engine.on_key_press, on_release=engine.on_key_release)
            self.keyboard.daemon = True
            self.keyboard.start()
            print(f"Key listener started ({', '.join(settings.pynput_keys)})")
            started_any = True

        if settings.mouse_buttons:
            self.mouse = MouseTriggerListener(
                settings,
                on_press=engine.on_key_press,
                on_release=engine.on_key_release,
                on_cancel=engine.cancel_trigger,
            )
            if self.mouse.start():
                print(
                    f"Mouse listener started ({', '.join(settings.mouse_buttons)}, "
                    f"hold {settings.mouse_hold_threshold}s)"
                )
                started_any = True
            else:
                self.mouse = None

        if not started_any:
            print("Error: no trigger listener could start — dictation will not fire.")
            print("   Grant Input Monitoring to WhisperLocal, then try again.")
        return started_any

    def stop(self) -> None:
        if self.fn:
            self.fn.stop()
            self.fn = None
            print("Fn listener stopped")
        if self.keyboard:
            try:
                self.keyboard.stop()
            except Exception:
                pass
            self.keyboard = None
            print("Key listener stopped")
        if self.mouse:
            self.mouse.stop()
            self.mouse = None
            print("Mouse listener stopped")

    def restart(self, settings: Settings) -> bool:
        """Swap to a new trigger set. Cuts short a recording in progress."""
        engine = self.engine
        if engine.state != engine.IDLE:
            engine.enabled = False
            engine.enabled = True
        self.stop()
        engine.trigger_tokens = resolve_trigger_tokens(settings)
        return self.start(settings)

    @property
    def fn_active(self) -> bool:
        return bool(self.fn and self.fn.active)

    def describe(self) -> dict:
        s = self.settings
        return {
            "fn": self.fn_active,
            "keys": list(s.pynput_keys) if (s and self.keyboard) else [],
            "mouse": list(s.mouse_buttons) if (s and self.mouse) else [],
        }


# ─── The state machine ───────────────────────────────────────────────────────────


class DictationEngine:
    """
    IDLE → (hold a trigger key past the threshold) → RECORDING
         → (release it) → TRANSCRIBING → IDLE

    With several trigger keys configured, the first one pressed owns the
    recording until it is released. A second trigger pressed mid-sentence is
    ignored, and releasing it does not cut the recording short.
    """

    IDLE = "idle"
    WAITING = "waiting"  # key is down but the hold threshold is not met yet
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"

    def __init__(
        self,
        transcriber: Transcriber,
        settings: Settings,
        overlay: FloatingOverlay | None = None,
        sounds: Sounds | None = None,
        on_state_change=None,
    ):
        self.transcriber = transcriber
        self.settings = settings
        self.recorder = AudioRecorder(settings)
        self.overlay = overlay
        self.sounds = sounds or Sounds(settings.sounds)
        self.on_state_change = on_state_change
        self.history = HistoryLog(settings)

        self.state = self.IDLE
        self.last_transcription = ""
        self.trigger_tokens = resolve_trigger_tokens(settings)
        self.backend = make_backend(settings, "dictation", transcriber=transcriber)

        # While the Settings page is listening for a new trigger key, every
        # press goes to this hook instead of starting a recording.
        self.capture_hook = None

        self._active_trigger = None  # which key owns the current hold
        self._threshold_timer: threading.Timer | None = None
        self._target_app: tuple[str | None, str | None] = (None, None)
        self._caret_rect = None
        self._enabled = True

    # ── state ────────────────────────────────────────────────────────────────

    def _set_state(self, new_state: str) -> None:
        """Move to a new state and tell the overlay and menu bar about it."""
        previous, self.state = self.state, new_state
        print(f"   State: {previous} → {new_state}")

        if self.overlay:
            if new_state == self.RECORDING:
                self.overlay.show_recording(self._caret_rect)
            elif new_state == self.TRANSCRIBING:
                self.overlay.show_transcribing()
            else:
                self.overlay.hide()

        if self.on_state_change:
            self.on_state_change(new_state)

    # ── key events ───────────────────────────────────────────────────────────

    def _identify(self, key) -> object | None:
        """Return the trigger token for `key`, or None if it isn't one."""
        if key in self.trigger_tokens:
            return key
        return None

    def apply_settings(self, settings: Settings) -> None:
        """Pick up new settings without a restart.

        Everything downstream reads `self.settings.<field>` at the moment it
        needs it, so swapping the reference is most of the work. The few things
        built from settings at construction time are rebuilt here.
        """
        self.settings = settings
        self.recorder.settings = settings
        self.sounds.enabled = settings.sounds
        self.history = HistoryLog(settings)
        self.trigger_tokens = resolve_trigger_tokens(settings)
        self.transcriber.apply_settings(settings)
        self.backend = make_backend(settings, "dictation", transcriber=self.transcriber)
        if self.overlay:
            self.overlay.apply_settings(settings)

    def on_key_press(self, key=None) -> None:
        """Start the hold timer when a trigger key goes down."""
        if self.capture_hook is not None:
            # Only the Fn tap has to be shared: KeyCapture runs its own
            # keyboard and mouse listeners, so everything else is dropped.
            if key == FN_KEY:
                self.capture_hook(key, True)
            return
        if not self._enabled:
            return

        token = self._identify(key)
        if token is None:
            return

        # Another trigger already owns this hold; ignore the extra key rather
        # than restarting or double-recording.
        if self._active_trigger is not None or self.state != self.IDLE:
            return

        self._active_trigger = token

        # Capture the target app now, not after transcribing: this is the app
        # you were in when you started talking, and it is the app the text will
        # land in. Cheap enough to do on the key path.
        self._target_app = frontmost_app()

        # And where its text cursor is, for the same reason — this is the last
        # moment before anything can steal focus. Bounded by AX_TIMEOUT, and
        # returns None rather than raising, so a slow app costs a fallback
        # position and never a missed recording.
        self._caret_rect = (
            caret_screen_rect() if self.settings.overlay_anchor == "caret" else None
        )

        self._set_state(self.WAITING)

        # Opening the audio stream takes tens of milliseconds, and with the Fn
        # trigger this runs on the event tap's callback — on the main runloop.
        # Blocking there stutters the UI and can make macOS decide the tap is
        # too slow and disable it. So recording always starts on a timer thread,
        # even when the delay is zero.
        #
        # Mouse buttons carry a longer threshold than keys; see
        # Settings.threshold_for.
        self._threshold_timer = threading.Timer(
            self.settings.threshold_for(token), self._threshold_reached
        )
        self._threshold_timer.daemon = True
        self._threshold_timer.start()

    def cancel_trigger(self, token=None) -> None:
        """Abandon a hold that turned out to be something else.

        The mouse listener calls this when a press starts travelling: that is a
        drag, not someone holding still to dictate. Only a hold that has not yet
        become a recording is cancelled — once you are actually recording, you
        are free to move the mouse around while you talk.
        """
        if token is not None and token != self._active_trigger:
            return
        if self.state != self.WAITING:
            return

        if self._threshold_timer:
            self._threshold_timer.cancel()
            self._threshold_timer = None
        self._active_trigger = None
        self._set_state(self.IDLE)
        print("   (Dragged — trigger cancelled)")

    def on_key_release(self, key=None) -> None:
        """Cancel a short tap, or finish a real recording."""
        if self.capture_hook is not None:
            if key == FN_KEY:
                self.capture_hook(key, False)
            return
        token = self._identify(key)
        if token is None or token != self._active_trigger:
            # Either not a trigger at all, or a different trigger key than the
            # one that started this hold. Letting go of it must not cut the
            # recording short.
            return

        self._active_trigger = None

        if self.state == self.WAITING:
            if self._threshold_timer:
                self._threshold_timer.cancel()
                self._threshold_timer = None
            self._set_state(self.IDLE)
            print("   (Quick tap — ignored)")

        elif self.state == self.RECORDING:
            self._stop_and_transcribe()

    def _threshold_reached(self) -> None:
        """The key was held long enough — open the microphone."""
        if self.state != self.WAITING:
            return

        self.sounds.start()
        self._set_state(self.RECORDING)
        try:
            self.recorder.start()
        except Exception as exc:
            print(f"Error: could not start recording: {exc}")
            print("   Grant Microphone permission to your terminal, then try again.")
            self.sounds.error()
            self._active_trigger = None
            self._set_state(self.IDLE)

    def _stop_and_transcribe(self) -> None:
        """Stop recording, transcribe off the main thread, then paste."""
        duration = self.recorder.stop()
        self.sounds.stop()

        app, bundle = self._target_app

        if duration < self.settings.min_recording_duration:
            print("   (Too short — discarded)")
            self.history.record("too_short", audio_seconds=duration, app=app, app_bundle_id=bundle)
            self._set_state(self.IDLE)
            return

        audio_file = cfg.temp_audio_file()
        if not self.recorder.save(audio_file):
            self.sounds.error()
            self.history.record("no_audio", audio_seconds=duration, app=app, app_bundle_id=bundle)
            self._set_state(self.IDLE)
            return

        self._set_state(self.TRANSCRIBING)

        def _do_transcribe():
            backend = self.backend
            t0 = time.perf_counter()
            status = "ok"
            text = None
            try:
                text = backend.transcribe_file(
                    audio_file, language=self.settings.whisper_language, timestamps=False
                ).text
            except BackendError as exc:
                print(f"Error: {backend.name} backend failed: {exc} — nothing pasted")
                self.sounds.error()
                status = "backend_error"
            elapsed_ms = (time.perf_counter() - t0) * 1000
            if backend.name != "local":
                print(f"   Transcribed via {backend.name} ({backend.model}) in {elapsed_ms:.0f} ms")

            if status != "ok":
                pass
            elif text and looks_degenerate(text, self.settings):
                # Whisper loops on short or noisy audio and emits one word
                # hundreds of times. Better to paste nothing than to dump that
                # into whatever you had focused.
                print(f"   (Discarded hallucinated output: {text[:60]!r})")
                self.sounds.error()
                status = "hallucination"
            elif not text:
                status = "empty"

            # Log every outcome, including the discarded ones — the failures are
            # what make the hallucination rate measurable.
            self.history.record(
                status,
                text=text,
                audio_seconds=duration,
                transcribe_ms=elapsed_ms,
                app=app,
                app_bundle_id=bundle,
                backend=backend.name,
                model=backend.model,
            )

            if status == "ok":
                self.sounds.done()
                paste_text(text, self.settings.paste_mode)
                self.last_transcription = text
            elif status == "empty":
                self.sounds.error()
                print("   (No speech recognized)")

            self._set_state(self.IDLE)

            try:
                audio_file.unlink()
            except OSError:
                pass

        threading.Thread(target=_do_transcribe, daemon=True).start()

    # ── on/off ───────────────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value
        if not value and self.state == self.RECORDING:
            self.recorder.stop()
            self._active_trigger = None
            self._set_state(self.IDLE)


# ─── Trigger key capture (for the Settings page) ─────────────────────────────────


class KeyCapture:
    """
    "Press the key you want to dictate with."

    Runs its own keyboard and mouse listeners for the duration, so any button
    at all can be offered, and borrows the app's Fn tap through the engine's
    capture hook (or starts a temporary one when Fn is not a trigger). While
    it is listening the engine drops every press, so trying keys out does not
    start recordings.

    A mouse button only counts if held for MOUSE_HOLD — the click on the
    "Record key" button itself, and any stray click, is ignored.
    """

    TIMEOUT = 10.0
    MOUSE_HOLD = 0.6

    def __init__(self, engine: DictationEngine, listeners: TriggerListeners):
        self.engine = engine
        self.listeners = listeners
        self._lock = threading.Lock()
        self._state: dict = {"state": "idle"}
        self._deadline = 0.0
        self._keyboard = None
        self._mouse = None
        self._fn: FnKeyListener | None = None
        self._timer: threading.Timer | None = None
        self._mouse_down: tuple[str, float] | None = None
        self._fn_down_at: float | None = None

    # ── public ───────────────────────────────────────────────────────────

    def start(self) -> dict:
        with self._lock:
            self._teardown_locked()
            self._state = {"state": "waiting"}
            self._deadline = time.time() + self.TIMEOUT
            self.engine.capture_hook = self._on_fn

            from pynput import keyboard, mouse

            self._keyboard = keyboard.Listener(on_press=self._on_key)
            self._keyboard.daemon = True
            self._keyboard.start()
            self._mouse = mouse.Listener(on_click=self._on_click)
            self._mouse.daemon = True
            self._mouse.start()

            if not self.listeners.fn_active and HAS_EVENT_TAP:
                fn = FnKeyListener(lambda k: self._on_fn(k, True), lambda k: self._on_fn(k, False))
                self._fn = fn
                run_on_main(fn.start)

            self._timer = threading.Timer(self.TIMEOUT, lambda: self._finish("timeout"))
            self._timer.daemon = True
            self._timer.start()
        return self.state()

    def state(self) -> dict:
        with self._lock:
            out = dict(self._state)
            if out["state"] in ("waiting", "unsupported"):
                out["seconds_left"] = max(0, round(self._deadline - time.time(), 1))
            return out

    def cancel(self) -> None:
        self._finish("cancelled")

    # ── listeners ────────────────────────────────────────────────────────

    def _on_key(self, key) -> None:
        if getattr(key, "name", None) == "esc":
            self._finish("cancelled")
            return
        token = keymap.token_for_key(key)
        if token:
            self._finish("captured", token)
            return
        with self._lock:
            if self._state.get("state") in ("waiting", "unsupported"):
                self._state = {
                    "state": "unsupported",
                    "pressed": keymap.key_display_name(key),
                }

    def _on_click(self, x, y, button, pressed) -> None:
        token = keymap.token_for_button(getattr(button, "name", ""))
        if token is None:
            return
        now = time.time()
        if pressed:
            self._mouse_down = (token, now)
            return
        down = self._mouse_down
        self._mouse_down = None
        if down and down[0] == token and now - down[1] >= self.MOUSE_HOLD:
            self._finish("captured", token)

    def _on_fn(self, key, pressed: bool) -> None:
        if pressed:
            self._finish("captured", FN_KEY)

    # ── teardown ─────────────────────────────────────────────────────────

    def _finish(self, state: str, token: str | None = None) -> None:
        with self._lock:
            if self._state.get("state") not in ("waiting", "unsupported"):
                return
            self._teardown_locked()
            if token:
                info = keymap.label_for(token)
                self._state = {
                    "state": "captured",
                    "token": token,
                    "label": info,
                    "risky": token in cfg.RISKY_KEYS,
                }
            else:
                self._state = {"state": state}
        print(f"Key capture: {self._state}")

    def _teardown_locked(self) -> None:
        self.engine.capture_hook = None
        if self._timer:
            self._timer.cancel()
            self._timer = None
        for listener in (self._keyboard, self._mouse):
            if listener:
                try:
                    listener.stop()
                except Exception:
                    pass
        self._keyboard = self._mouse = None
        if self._fn:
            fn, self._fn = self._fn, None
            run_on_main(fn.stop)
        self._mouse_down = None


# ─── Process restart ─────────────────────────────────────────────────────────────


def restart_process(delay: float = 0.3) -> None:
    """Replace this process with a fresh copy of itself.

    exec keeps the PID, so the supervisor in start.sh sees nothing and counts
    no failure. Scheduled rather than immediate so an HTTP response asking for
    the restart can still reach the browser.
    """

    def _go():
        print("Restarting WhisperLocal...")
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            os.execv(sys.executable, list(sys.orig_argv))
        except Exception as exc:  # pragma: no cover - exec failing is unusual
            print(f"Error: restart failed: {exc}")
            # 75 = EX_TEMPFAIL: the supervisor restarts on any non-zero exit.
            os._exit(75)

    timer = threading.Timer(delay, lambda: run_on_main(_go))
    timer.daemon = True
    timer.start()


# ─── Menu bar app ────────────────────────────────────────────────────────────────


class WhisperLocalApp(rumps.App):
    """The menu bar application."""

    def __init__(self, engine: DictationEngine, settings: Settings, web=None):
        super().__init__(cfg.APP_NAME, title=None, quit_button=None)
        self.engine = engine
        self.settings = settings
        self.web = web  # WebServer, or None when the dashboard is off
        self.on_quit = None  # extra shutdown work, set by run()

        # rumps seeds the status item from this attribute when the app launches
        # (NSApp.setStatusBarIcon reads _app['_icon_nsimage'], and _app is this
        # instance's __dict__). Setting it here means the menu bar comes up as
        # an icon straight away, with no text flash.
        self._icon_nsimage = sf_symbol_image(settings.icon_idle, settings)

        # Checkable items use the native NSMenuItem checkmark (item.state)
        # rather than emoji in the label — same convention as system menus.
        self.toggle_item = rumps.MenuItem("Enabled", callback=self._toggle)
        self.toggle_item.state = 1
        self.status_item = rumps.MenuItem("Idle")
        self.status_item.set_callback(None)
        self.last_text_item = rumps.MenuItem("Last: (none)", callback=self._copy_last)

        # Meetings. The recorder and detector are attached by run(); until
        # then the items are hidden.
        self.recorder: MeetingRecorder | None = None
        self.detector: MeetingDetector | None = None
        self.meeting_start_item = rumps.MenuItem("Start Meeting Recording", callback=self._start_meeting)
        self.meeting_stop_item = rumps.MenuItem("Stop Meeting Recording", callback=self._stop_meeting)
        self.meeting_stop_item.hidden = True
        self._meeting_timer = rumps.Timer(self._tick_meeting, 1)
        self._detected: DetectedMeeting | None = None

        menu = [
            self.status_item,
            self.last_text_item,
            None,
            self.meeting_start_item,
            self.meeting_stop_item,
            rumps.MenuItem("Meetings…", callback=lambda _: self.open_web("meetings")),
            None,
            rumps.MenuItem("Dashboard…", callback=lambda _: self.open_web("dashboard")),
            rumps.MenuItem("Settings…", callback=lambda _: self.open_web("settings")),
            rumps.MenuItem("Reveal History in Finder", callback=self._reveal_history),
            None,
            self.toggle_item,
            rumps.MenuItem("Permissions…", callback=self._permissions),
            rumps.MenuItem("Restart", callback=self._restart),
            None,
            rumps.MenuItem("Quit", callback=self._quit),
        ]
        self.menu = menu

        self.engine.on_state_change = self._on_state_change

    def _set_menu_icon(self, symbol_name: str) -> None:
        """Put an SF Symbol in the menu bar. Main thread only.

        Falls back to the app name as text if the symbol can't be rendered, so
        the status item is never invisible.
        """
        nsapp = getattr(self, "_nsapp", None)
        statusitem = getattr(nsapp, "nsstatusitem", None) if nsapp else None
        if statusitem is None:
            return

        image = sf_symbol_image(symbol_name, self.settings)
        if image is None:
            statusitem.setImage_(None)
            statusitem.setTitle_(cfg.APP_NAME)
            return

        statusitem.setTitle_("")
        statusitem.setImage_(image)

    def apply_settings(self, settings: Settings) -> None:
        """New settings: redraw the icon and the idle label."""
        self.settings = settings
        self._on_state_change(self.engine.state)

    def open_web(self, fragment: str = "") -> None:
        """Open the dashboard or settings page in the default browser."""
        if self.web is None:
            rumps.alert(
                title="WhisperLocal — dashboard",
                message="The dashboard is switched off (web_enabled = false in config.toml).",
                ok="OK",
            )
            return
        subprocess.Popen(["open", self.web.url(fragment)])

    def _on_state_change(self, state: str) -> None:
        """Reflect the engine state in the icon and the status line."""
        s = self.settings
        icons = {
            DictationEngine.IDLE: s.icon_idle,
            DictationEngine.WAITING: s.icon_waiting,
            DictationEngine.RECORDING: s.icon_recording,
            DictationEngine.TRANSCRIBING: s.icon_transcribing,
        }
        labels = {
            DictationEngine.IDLE: f"Idle — hold {s.trigger_label} to dictate",
            DictationEngine.WAITING: "Hold detected…",
            DictationEngine.RECORDING: "Recording…",
            DictationEngine.TRANSCRIBING: "Transcribing…",
        }

        # _set_state calls this from the pynput listener thread, a
        # threading.Timer thread and the transcription worker. Every assignment
        # below mutates an NSStatusItem, so all of it has to land on the main
        # thread or it trips the same AppKit assertion that killed the paste.
        def _apply():
            meeting = self.recorder.status() if self.recorder else None
            if state == DictationEngine.IDLE and meeting and meeting.get("recording"):
                self._set_menu_icon(s.icon_meeting)
                self.status_item.title = self._meeting_label(meeting)
            elif state == DictationEngine.IDLE and self._detected is not None:
                self._set_menu_icon(s.icon_meeting_detected)
                self.status_item.title = f"Meeting detected in {self._detected.label} — record it?"
            elif not self.engine.enabled and state == DictationEngine.IDLE:
                self._set_menu_icon(s.icon_disabled)
                self.status_item.title = "Disabled"
            else:
                self._set_menu_icon(icons.get(state, s.icon_idle))
                self.status_item.title = labels.get(state, "Unknown")

            text = self.engine.last_transcription
            if text:
                display = text[:60] + "…" if len(text) > 60 else text
                self.last_text_item.title = f"Last: {display}"

        run_on_main(_apply)

    def _toggle(self, sender) -> None:
        """Turn dictation on or off without quitting."""
        self.engine.enabled = not self.engine.enabled
        sender.state = 1 if self.engine.enabled else 0
        print("Dictation enabled" if self.engine.enabled else "Dictation disabled")
        self._on_state_change(self.engine.state)

    def _permissions(self, sender) -> None:
        """Re-check permissions on demand, and say so when nothing is missing."""
        missing = missing_permissions(self.settings)
        if missing:
            prompt_for_permissions(missing)
        else:
            rumps.alert(
                title="WhisperLocal — permissions",
                message="Everything WhisperLocal needs has been granted.",
                ok="Good",
            )

    def _reveal_history(self, sender) -> None:
        """Show the history file in Finder."""
        path = self.settings.history_path
        if path.exists():
            subprocess.Popen(["open", "-R", str(path)])
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            subprocess.Popen(["open", str(path.parent)])

    def _copy_last(self, sender) -> None:
        """Put the last transcription back on the clipboard."""
        text = self.engine.last_transcription
        if not text:
            return
        pyperclip.copy(text)
        rumps.notification(cfg.APP_NAME, "Copied to clipboard", text[:100])

    # ── meetings ─────────────────────────────────────────────────────────

    def attach_meetings(self, recorder: MeetingRecorder, detector: MeetingDetector | None) -> None:
        self.recorder = recorder
        self.detector = detector
        recorder.on_event(self._on_meeting_event)

    @staticmethod
    def _meeting_label(status: dict) -> str:
        secs = int(status.get("elapsed_s") or 0)
        if secs >= 3600:
            clock = f"{secs // 3600}:{secs % 3600 // 60:02d}:{secs % 60:02d}"
        else:
            clock = f"{secs // 60}:{secs % 60:02d}"
        where = f" — {status['app']}" if status.get("app") else ""
        return f"Recording meeting {clock}{where}"

    def start_meeting(self, *, trigger: str = "manual", meeting: DetectedMeeting | None = None) -> None:
        """Begin a meeting recording. Safe from any thread; errors go to an alert."""
        if self.recorder is None:
            return
        app = meeting.label if meeting else None
        bundle = meeting.bundle_id if meeting else None

        def _go():
            try:
                self.recorder.start(trigger=trigger, app=app, app_bundle_id=bundle)
            except Exception as exc:
                print(f"Error: could not start the meeting recording: {exc}")
                run_on_main(lambda: rumps.alert(
                    title="WhisperLocal — meeting recording",
                    message=f"Could not start recording:\n{exc}",
                    ok="OK",
                ))
                return
            if self.detector:
                run_on_main(self.detector.notify_recording_started)
            self._maybe_explain_system_audio()

        threading.Thread(target=_go, daemon=True).start()

    def stop_meeting(self) -> None:
        if self.recorder is None:
            return
        threading.Thread(target=self.recorder.stop, daemon=True).start()
        if self.detector:
            self.detector.notify_recording_stopped()

    def _start_meeting(self, sender) -> None:
        self.start_meeting(trigger="manual", meeting=self._detected)

    def _stop_meeting(self, sender) -> None:
        self.stop_meeting()

    def _maybe_explain_system_audio(self) -> None:
        """First refusal of the system-audio tap: say where to grant it."""
        from whisperlocal import systemaudio

        denial = systemaudio.last_denial
        if not denial or getattr(self, "_explained_audio", False):
            return
        self._explained_audio = True

        def _ask():
            choice = rumps.alert(
                title="WhisperLocal — other participants' audio",
                message=(
                    "macOS did not allow WhisperLocal to capture system audio, so this "
                    "meeting is being recorded from your microphone only.\n\n"
                    "To include the other participants, allow WhisperLocal under "
                    "System Settings → Privacy & Security → System Audio Recording Only, "
                    "then start the next recording."
                ),
                ok="Open Settings",
                cancel="Not now",
            )
            if choice == 1:
                open_permission_pane("System Audio Recording")

        run_on_main(_ask)

    def _on_meeting_event(self, name: str, data: dict) -> None:
        def _apply():
            recording = bool(self.recorder and self.recorder.recording)
            self.meeting_start_item.hidden = recording
            self.meeting_stop_item.hidden = not recording
            if name == "started":
                self._meeting_timer.start()
            elif name in ("stopped", "discarded", "failed"):
                self._meeting_timer.stop()
            elif name == "system_audio_unavailable":
                self._maybe_explain_system_audio()
            try:
                if name == "finalized":
                    words = data.get("words") or 0
                    rumps.notification(cfg.APP_NAME, "Meeting transcript ready", f"{words:,} words saved")
                elif name == "failed":
                    rumps.notification(cfg.APP_NAME, "Meeting transcription failed", str(data.get("error") or ""))
            except Exception as exc:
                print(f"Warning: notification failed: {exc}")
            self._on_state_change(self.engine.state)

        run_on_main(_apply)

    def _tick_meeting(self, _timer) -> None:
        if self.engine.state == DictationEngine.IDLE:
            self._on_state_change(self.engine.state)

    # ── detection ────────────────────────────────────────────────────────

    def on_meeting_detected(self, meeting: DetectedMeeting) -> None:
        """Main thread, from the detector. Ask, or just start."""
        self._detected = meeting
        s = self.settings
        if self.recorder and self.recorder.recording:
            return
        if s.meeting_auto_record:
            self.start_meeting(trigger="auto", meeting=meeting)
            return
        if s.meeting_prompt == "notification":
            self.sounds_ping()
            try:
                rumps.notification(
                    cfg.APP_NAME,
                    f"{meeting.label} call detected",
                    "Record and transcribe this meeting?",
                    data={"kind": "meeting"},
                    action_button="Record",
                    other_button="Not now",
                )
            except Exception as exc:
                # Notification delivery is not guaranteed for a script-launched
                # process; the dialog always works.
                print(f"Warning: notification failed ({exc}) — asking with a dialog")
                self._show_meeting_panel(meeting)
                return
            if self.detector:
                self.detector.mark_prompted()
        elif s.meeting_prompt == "panel":
            self._show_meeting_panel(meeting)
        else:
            self.sounds_ping()
        self._on_state_change(self.engine.state)

    def sounds_ping(self) -> None:
        try:
            self.engine.sounds.play("Ping")
        except Exception:
            pass

    def _show_meeting_panel(self, meeting: DetectedMeeting) -> None:
        """A dialog ask, for when notifications are not delivered."""
        choice = rumps.alert(
            title=f"Record the {meeting.label} call?",
            message="WhisperLocal can record and transcribe this meeting on your Mac.",
            ok="Record",
            cancel="Not now",
        )
        if choice == 1:
            self.start_meeting(trigger="prompt", meeting=meeting)
        elif self.detector:
            self.detector.dismiss()

    def on_meeting_ended(self, meeting: DetectedMeeting) -> None:
        """Main thread, from the detector: the call is over, stop recording."""
        self._detected = None
        if self.recorder and self.recorder.recording:
            self.stop_meeting()
        self._on_state_change(self.engine.state)

    def on_detector_state(self, state: str, meeting: DetectedMeeting | None) -> None:
        if state in (MeetingDetector.NO_MEETING, MeetingDetector.RECORDING):
            self._detected = None
        self._on_state_change(self.engine.state)

    def _restart(self, sender) -> None:
        restart_process()

    def _quit(self, sender) -> None:
        print("WhisperLocal shutting down")
        if self.recorder and self.recorder.recording:
            self.status_item.title = "Finishing the meeting transcript…"
            try:
                self.recorder.shutdown()
            except Exception as exc:
                print(f"Warning: meeting shutdown failed: {exc}")
        if self.on_quit:
            try:
                self.on_quit()
            except Exception as exc:
                print(f"Warning: shutdown step failed: {exc}")
        if self.web:
            self.web.stop()
        rumps.quit_application()


@rumps.notifications
def _on_notification(info) -> None:
    """Clicks on our notifications. Only the meeting prompt carries data."""
    try:
        data = info.data if hasattr(info, "data") else dict(info)
    except Exception:
        data = {}
    if not isinstance(data, dict) or data.get("kind") != "meeting":
        return
    app = getattr(rumps.App, "*app_instance", None)
    if app is None:
        return
    activation = getattr(info, "activation_type", None)
    if activation in ("action_button_clicked", "contents_clicked", None):
        app.start_meeting(trigger="prompt", meeting=app._detected)
    elif app.detector:
        app.detector.dismiss()


# ─── What the web layer sees ─────────────────────────────────────────────────────


class RunningAppContext:
    """The live app, as the HTTP handlers see it. See web/api.py AppContext.

    HTTP handlers run on their own threads; nothing here may touch AppKit
    directly. Plain attribute reads on the engine are fine.
    """

    running = True

    def __init__(
        self,
        *,
        settings: SettingsManager,
        engine: DictationEngine,
        listeners: TriggerListeners,
        transcriber: Transcriber,
        capture: KeyCapture,
    ):
        from whisperlocal import __version__, keychain

        self.settings = settings
        self.engine = engine
        self.listeners = listeners
        self.transcriber = transcriber
        self.capture = capture
        self.version = __version__
        self.meetings = None  # MeetingStore, set once meetings are wired up
        self.recorder = None  # MeetingRecorder
        self.detector = None  # MeetingDetector
        self._keychain = keychain
        self._started = time.time()

    @property
    def history_path(self):
        return self.settings.current.history_path

    def status(self) -> dict:
        engine = self.engine
        s = self.settings.current
        text = engine.last_transcription
        out = {
            "running": True,
            "state": engine.state,
            "enabled": engine.enabled,
            "last_transcription": (text[:120] + "…") if len(text) > 120 else text,
            "model": s.model_path,
            "model_loaded": self.transcriber._loaded,
            "memory": self.transcriber.memory_report(),
            "listeners": self.listeners.describe(),
            "trigger_label": s.trigger_label,
            "backend": engine.backend.name,
            "uptime_s": round(time.time() - self._started),
            "capture": self.capture.state(),
            "meeting": self.recorder.status() if self.recorder else None,
        }
        return out

    def start_capture(self) -> dict:
        return self.capture.start()

    def capture_state(self) -> dict:
        return self.capture.state()

    def cancel_capture(self) -> None:
        self.capture.cancel()

    def request_restart(self) -> None:
        restart_process()

    def api_key_set(self) -> bool:
        return self._keychain.has_api_key()

    def set_api_key(self, key: str) -> None:
        self._keychain.set_api_key(key)

    def clear_api_key(self) -> None:
        self._keychain.clear_api_key()


# ─── Permissions ─────────────────────────────────────────────────────────────────

PANE = "x-apple.systempreferences:com.apple.preference.security"
PERMISSION_PANES = {
    "Accessibility": f"{PANE}?Privacy_Accessibility",
    "Input Monitoring": f"{PANE}?Privacy_ListenEvent",
    "Microphone": f"{PANE}?Privacy_Microphone",
    "System Audio Recording": f"{PANE}?Privacy_AudioCapture",
}


def open_permission_pane(name: str) -> None:
    """Open one System Settings privacy pane."""
    link = PERMISSION_PANES.get(name)
    if link:
        subprocess.Popen(["open", link])


def missing_permissions(settings: Settings) -> list[str]:
    """
    Which permissions still need a manual toggle.

    Microphone is not listed: macOS prompts for it natively the first time we
    open the input stream, and the bundle's NSMicrophoneUsageDescription
    supplies the explanation. Accessibility and Input Monitoring have no such
    prompt — the user has to find them in System Settings, which is exactly why
    this exists.
    """
    missing = []

    try:
        from ApplicationServices import AXIsProcessTrusted

        if not AXIsProcessTrusted():
            missing.append("Accessibility")
    except ImportError:
        pass

    if settings.uses_fn and HAS_EVENT_TAP:
        tap = CGEventTapCreate(
            kCGSessionEventTap,
            kCGHeadInsertEventTap,
            kCGEventTapOptionListenOnly,
            CGEventMaskBit(kCGEventFlagsChanged),
            lambda *a: None,
            None,
        )
        if not tap:
            missing.append("Input Monitoring")

    return missing


def prompt_for_permissions(missing: list[str]) -> None:
    """
    Ask for the permissions that macOS will not prompt for on its own.

    Shown once per launch and only while something is actually missing, so it
    stops appearing as soon as the user has granted everything.
    """
    if not missing:
        return

    why = {
        "Accessibility": "to paste transcribed text at your cursor",
        "Input Monitoring": "to notice the trigger key while another app is focused",
    }
    lines = [f"• {name} — {why.get(name, '')}" for name in missing]

    message = (
        "WhisperLocal needs "
        + ("a permission" if len(missing) == 1 else "some permissions")
        + " that macOS only grants by hand:\n\n"
        + "\n".join(lines)
        + "\n\nOpen Settings and switch WhisperLocal on, then quit and reopen it "
        "from the menu bar.\n\nDictation will not work until you do."
    )

    def _ask():
        try:
            response = rumps.alert(
                title="WhisperLocal — one more step",
                message=message,
                ok="Open Settings",
                cancel="Later",
            )
            if response:
                for name in missing:
                    open_permission_pane(name)
        except Exception as exc:
            print(f"Warning: could not show the permissions prompt: {exc}")

    run_on_main(_ask)


# ─── Entry point ─────────────────────────────────────────────────────────────────


def run(settings: Settings) -> int:
    """Start the menu bar app. Blocks until the user quits."""
    try:
        resolve_trigger_tokens(settings)
    except ValueError as exc:
        print(f"Error: {exc}")
        print(f"   Edit {cfg.config_path()} to fix it.")
        return 1

    print("=" * 60)
    print("  WhisperLocal — push-to-talk dictation")
    print("=" * 60)
    print()
    held = f"for {settings.hold_threshold}s" if settings.hold_threshold else "(no delay)"
    print(f"  Trigger:   Hold {settings.trigger_label} {held}")
    if settings.mouse_buttons:
        print(f"             Mouse buttons need {settings.mouse_hold_threshold}s")
    print(f"  Model:     {settings.model_path}")
    print(f"  Language:  {settings.language}")
    print(f"  Delivery:  {settings.paste_mode}")
    if settings.history_enabled:
        kind = "text + stats" if settings.history_text else "stats only"
        print(f"  History:   {settings.history_path} ({kind})")
    else:
        print("  History:   off")
    print(f"  Config:    {cfg.config_path()}")
    print()

    for note in cfg.trigger_warnings(settings):
        print(f"  Note: {note}")
        print()

    print("  Not working? Run: whisperlocal doctor")
    print()

    transcriber = Transcriber(settings)
    overlay = FloatingOverlay(settings)
    sounds = Sounds(enabled=settings.sounds)
    engine = DictationEngine(transcriber, settings, overlay=overlay, sounds=sounds)

    # Start the listeners. Fn and ordinary keys use different mechanisms and
    # both can run at once, so a config mixing them gets two live listeners.
    # The Fn tap goes on the main runloop, so this must happen before
    # app.run() starts it.
    listeners = TriggerListeners(engine)
    listeners.start(settings)

    def _prewarm():
        """Load the model now so the first dictation is not the slow one."""
        print("Pre-warming the model (the first run downloads weights)...")
        transcriber.warm()
        print("Model ready")
        sounds.done()

    threading.Thread(target=_prewarm, daemon=True).start()

    # Settings can change while running: the Settings page writes config.toml
    # and pushes the new values through here.
    settings_mgr = SettingsManager(settings)
    capture = KeyCapture(engine, listeners)

    web = None
    if settings.web_enabled:
        from whisperlocal.web.server import WebConfig, WebServer

        ctx = RunningAppContext(
            settings=settings_mgr,
            engine=engine,
            listeners=listeners,
            transcriber=transcriber,
            capture=capture,
        )
        web = WebServer(ctx, WebConfig(port=settings.web_port))
        try:
            url = web.start()
            print(f"Dashboard: {url.split('?')[0]}")
        except Exception as exc:
            print(f"Warning: dashboard not started: {exc}")
            web = None

    print("Menu bar app starting...")
    print()

    app = WhisperLocalApp(engine, settings, web=web)

    # Meetings: the store and recorder always exist (the menu items and the
    # CLI use them); detection only runs when meeting_enabled.
    store = MeetingStore(settings.meeting_root)
    recorder = MeetingRecorder(settings, store, transcriber, engine=engine, sounds=sounds)
    detector = MeetingDetector(
        settings,
        on_detected=app.on_meeting_detected,
        on_ended=app.on_meeting_ended,
        on_state=app.on_detector_state,
    )
    app.attach_meetings(recorder, detector)
    if web is not None:
        ctx.meetings = store
        ctx.recorder = recorder
        ctx.detector = detector
    if settings.meeting_enabled:
        detector.start()

    def _on_settings_changed(old: Settings, new: Settings, tiers: dict) -> None:
        engine.apply_settings(new)
        app.apply_settings(new)
        recorder.apply_settings(new)
        run_on_main(lambda: detector.apply_settings(new))
        if tiers.get(Tier.LISTENERS):
            run_on_main(lambda: listeners.restart(new))
        if tiers.get(Tier.RESTART):
            print(f"Settings changed that need a restart: {', '.join(tiers[Tier.RESTART])}")

    settings_mgr.subscribe(_on_settings_changed)

    # Ask for anything macOS will not prompt for on its own, once the menu bar
    # exists so the alert has an app to belong to.
    missing = missing_permissions(settings)
    if missing:
        print(f"Missing permissions: {', '.join(missing)}")

        # A short delay so the menu bar item is up before the alert appears.
        # The timer stops itself — rumps timers otherwise repeat forever, and
        # nobody wants this dialog every second.
        def _prompt_once(timer):
            timer.stop()
            prompt_for_permissions(missing)

        rumps.Timer(_prompt_once, 1).start()

    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(run(cfg.load()))
