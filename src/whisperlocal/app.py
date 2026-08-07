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
from whisperlocal.config import FN_KEY, Settings

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


np = _check_import("numpy")
sounddevice = _check_import("sounddevice")
sf = _check_import("soundfile")
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
        CFRunLoopAddSource,
        CFRunLoopGetMain,
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
            "model": self.settings.model_path,
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


# ─── Audio feedback ──────────────────────────────────────────────────────────────


class Sounds:
    """macOS system sounds for the four things that can happen."""

    START = "Tink"
    STOP = "Pop"
    DONE = "Glass"
    ERROR = "Basso"

    def __init__(self, enabled: bool = True):
        self.enabled = enabled

    def play(self, name: str) -> None:
        if not self.enabled:
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


# ─── Whisper transcriber ─────────────────────────────────────────────────────────


class Transcriber:
    """Manages the mlx-whisper model."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.model_path = settings.model_path
        self._mlx_whisper = None
        self._loaded = False

    def _ensure_loaded(self) -> None:
        """Import mlx_whisper on first use — it is slow to load."""
        if self._loaded:
            return
        print("Loading mlx-whisper...")
        try:
            import mlx_whisper
        except ImportError:
            print("Error: mlx-whisper is not installed. Run: pip install mlx-whisper")
            sys.exit(1)
        self._mlx_whisper = mlx_whisper
        self._loaded = True
        print(f"mlx-whisper loaded. Model: {self.model_path}")
        print("   (Weights download on first use — the first run takes longer.)")

    def transcribe(self, audio_path) -> str | None:
        """Transcribe an audio file. None if anything went wrong."""
        self._ensure_loaded()
        try:
            result = self._mlx_whisper.transcribe(
                str(audio_path),
                path_or_hf_repo=self.model_path,
                language=self.settings.whisper_language,
                fp16=self.settings.fp16,
            )
            return result.get("text", "").strip()
        except Exception as exc:
            print(f"Error: transcription failed: {exc}")
            return None

    def transcribe_words(self, audio, model_path: str | None = None):
        """Transcribe an audio array and return [(word, start, end), ...].

        Takes no prompt, deliberately. Passing previously transcribed text back
        in as `initial_prompt` creates a positive feedback loop: on near-silence
        the model simply continues the prompt, so one bad chunk primes the next
        and the session degenerates into a single token repeated forever
        ("ARP ARP ARP..."). Reproduced exactly — 2.5s of room tone with such a
        prompt yields 112 words of "ARP" in 1650ms; the same audio with no
        prompt yields nothing in 97ms.

        condition_on_previous_text is off for the same reason, guarding against
        a spiral within a single chunk.
        """
        self._ensure_loaded()
        try:
            result = self._mlx_whisper.transcribe(
                audio,
                path_or_hf_repo=model_path or self.model_path,
                language=self.settings.whisper_language,
                fp16=self.settings.fp16,
                word_timestamps=True,
                condition_on_previous_text=False,
            )
        except Exception as exc:
            print(f"Error: transcription failed: {exc}")
            return None

        return [
            (w["word"], w["start"], w["end"])
            for segment in result.get("segments", [])
            for w in (segment.get("words") or [])
        ]


# ─── Audio recorder ──────────────────────────────────────────────────────────────


class AudioRecorder:
    """Records audio from the default microphone."""

    BLOCKSIZE = 1024

    def __init__(self, settings: Settings):
        self.settings = settings
        self._frames: list = []
        self._stream = None
        self._recording = False
        self._start_time: float | None = None
        self._thread: threading.Thread | None = None
        self._samplerate = settings.sample_rate
        # Guards _frames. The drain thread appends to it while a live
        # dictation worker reads and trims it.
        self._lock = threading.Lock()

    def _device_samplerate(self) -> int:
        """Native sample rate of the default input device.

        Asking PortAudio for a rate the hardware doesn't run at (16 kHz on a mic
        that runs at 48 kHz) forces it through its rate-adapting code path, which
        segfaults on CoreAudio's realtime thread. Record at the native rate
        instead; whisper's ffmpeg loader downsamples to 16 kHz when it reads the
        file, so nothing downstream cares.
        """
        try:
            return int(sounddevice.query_devices(kind="input")["default_samplerate"])
        except Exception:
            return self.settings.sample_rate

    def start(self) -> None:
        """Open the microphone and start collecting frames."""
        with self._lock:
            self._frames = []
        self._recording = True
        self._start_time = time.time()
        self._samplerate = self._device_samplerate()

        self._stream = sounddevice.InputStream(
            samplerate=self._samplerate,
            channels=self.settings.channels,
            dtype="float32",
            blocksize=self.BLOCKSIZE,
        )
        self._stream.start()

        # Drain the stream from a thread we own. Handing a Python callback to
        # PortAudio runs the interpreter on CoreAudio's realtime IO thread and
        # crashes there (EXC_BAD_ACCESS in _PyEval_EvalFrameDefault, via cffi's
        # closure trampoline).
        def _drain():
            while self._recording:
                try:
                    data, overflowed = self._stream.read(self.BLOCKSIZE)
                except Exception as exc:
                    print(f"Warning: audio read error: {exc}")
                    break
                if overflowed:
                    print("Warning: audio buffer overflow")
                with self._lock:
                    self._frames.append(data.copy())

        self._thread = threading.Thread(target=_drain, daemon=True)
        self._thread.start()
        print("Recording...")

    def stop(self) -> float:
        """Close the microphone and return how long we recorded for."""
        self._recording = False

        # Join before closing: the drain thread must not be inside read() when
        # the stream goes away.
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

        duration = time.time() - self._start_time if self._start_time else 0.0
        self._start_time = None
        print(f"Recording stopped ({duration:.1f}s)")
        return duration

    def snapshot(self):
        """Everything captured so far, without stopping the recording.

        Live dictation re-transcribes the buffer while it is still filling.
        """
        with self._lock:
            frames = list(self._frames)
        if not frames:
            return np.zeros(0, dtype="float32"), self._samplerate
        return np.concatenate(frames, axis=0), self._samplerate

    def drop_leading(self, seconds: float) -> float:
        """Discard `seconds` of audio from the front of the buffer.

        Keeps live sessions inside whisper's 30s attention window, beyond which
        inference cost triples. Frames are fixed-size so this rounds down to a
        whole number of them, and returns how much was actually dropped so the
        caller can keep its own timeline in sync.
        """
        if seconds <= 0:
            return 0.0

        with self._lock:
            if not self._frames:
                return 0.0
            frames_to_drop = int(seconds * self._samplerate) // self.BLOCKSIZE
            frames_to_drop = min(frames_to_drop, len(self._frames))
            if frames_to_drop <= 0:
                return 0.0
            del self._frames[:frames_to_drop]

        return frames_to_drop * self.BLOCKSIZE / self._samplerate

    def save(self, filepath) -> bool:
        """Write the captured audio to a WAV file."""
        with self._lock:
            frames = list(self._frames)
        if not frames:
            return False

        audio_data = np.concatenate(frames, axis=0)
        filepath.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(filepath), audio_data, self._samplerate)
        return True

    @property
    def is_recording(self) -> bool:
        return self._recording


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

    def on_key_press(self, key=None) -> None:
        """Start the hold timer when a trigger key goes down."""
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
            t0 = time.perf_counter()
            text = self.transcriber.transcribe(audio_file)
            elapsed_ms = (time.perf_counter() - t0) * 1000

            status = "ok"
            if text and looks_degenerate(text, self.settings):
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


# ─── Menu bar app ────────────────────────────────────────────────────────────────


class WhisperLocalApp(rumps.App):
    """The menu bar application."""

    def __init__(self, engine: DictationEngine, settings: Settings):
        super().__init__(cfg.APP_NAME, title=None, quit_button=None)
        self.engine = engine
        self.settings = settings

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

        menu = [self.status_item, self.last_text_item]
        if settings.history_enabled:
            menu.append(rumps.MenuItem("Reveal History in Finder", callback=self._reveal_history))
        menu += [
            None,
            self.toggle_item,
            rumps.MenuItem("Permissions…", callback=self._permissions),
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

        if self.engine.enabled:
            self._set_menu_icon(self.settings.icon_idle)
            self.status_item.title = f"Idle — hold {self.settings.trigger_label} to dictate"
            print("Dictation enabled")
        else:
            self._set_menu_icon(self.settings.icon_disabled)
            self.status_item.title = "Disabled"
            print("Dictation disabled")

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

    def _quit(self, sender) -> None:
        print("WhisperLocal shutting down")
        rumps.quit_application()


# ─── Permissions ─────────────────────────────────────────────────────────────────

PANE = "x-apple.systempreferences:com.apple.preference.security"
PERMISSION_PANES = {
    "Accessibility": f"{PANE}?Privacy_Accessibility",
    "Input Monitoring": f"{PANE}?Privacy_ListenEvent",
    "Microphone": f"{PANE}?Privacy_Microphone",
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

    if cfg.MOUSE_LEFT in settings.trigger_keys:
        guard = settings.mouse_drag_cancel_px
        print("  Note: the left mouse button is also what every drag, text")
        print("        selection and window move holds down.")
        if guard:
            print(f"        Presses that travel more than {guard}px are treated as")
            print("        drags and cancelled before recording starts.")
        else:
            print("        The drag guard is OFF (mouse_drag_cancel_px = 0), so any")
            print("        press held long enough will record. Consider mouse_right.")
        print()

    risky = [k for k in settings.trigger_keys if k in cfg.RISKY_KEYS and k != cfg.MOUSE_LEFT]
    if risky:
        print(f"  Note: {', '.join(risky)} is used by other things on macOS.")
        print("        Holding it during one will start a recording.")
        print()

    print("  Not working? Run: whisperlocal doctor")
    print()

    transcriber = Transcriber(settings)
    overlay = FloatingOverlay(settings)
    sounds = Sounds(enabled=settings.sounds)
    engine = DictationEngine(transcriber, settings, overlay=overlay, sounds=sounds)

    # Start the listeners. Fn and ordinary keys use different mechanisms and
    # both can run at once, so a config mixing them gets two live listeners.
    started_any = False

    if settings.uses_fn:
        # The tap goes on the main runloop, so this must happen before
        # app.run() starts it.
        fn_listener = FnKeyListener(engine.on_key_press, engine.on_key_release)
        if fn_listener.start():
            print("Fn listener started")
            started_any = True
        else:
            print("Error: could not start the Fn listener")

    if settings.pynput_keys:
        listener = Listener(on_press=engine.on_key_press, on_release=engine.on_key_release)
        listener.daemon = True
        listener.start()
        print(f"Key listener started ({', '.join(settings.pynput_keys)})")
        started_any = True

    if settings.mouse_buttons:
        mouse_listener = MouseTriggerListener(
            settings,
            on_press=engine.on_key_press,
            on_release=engine.on_key_release,
            on_cancel=engine.cancel_trigger,
        )
        if mouse_listener.start():
            print(
                f"Mouse listener started ({', '.join(settings.mouse_buttons)}, "
                f"hold {settings.mouse_hold_threshold}s)"
            )
            started_any = True

    if not started_any:
        print("Error: no trigger listener could start — dictation will not fire.")
        print("   Grant Input Monitoring to your terminal, then try again.")

    def _prewarm():
        """Load the model now so the first dictation is not the slow one."""
        print("Pre-warming the model (the first run downloads weights)...")
        transcriber._ensure_loaded()
        print("Model ready")
        sounds.done()

    threading.Thread(target=_prewarm, daemon=True).start()

    print("Menu bar app starting...")
    print()

    app = WhisperLocalApp(engine, settings)

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
