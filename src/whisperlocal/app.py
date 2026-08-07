#!/usr/bin/env python3
"""
WhisperLocal — push-to-talk local dictation for macOS.

Hold the trigger key → record → transcribe with mlx-whisper → paste at cursor.

Run it with the `whisperlocal` command rather than calling this module
directly; see whisperlocal/cli.py.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

from whisperlocal import config as cfg

# ─── Imports with friendly error messages ────────────────────────────────────────


def _check_import(module_name: str, pip_name: str | None = None):
    """Import a module, or explain how to get it and stop."""
    try:
        return __import__(module_name)
    except ImportError:
        pip_name = pip_name or module_name
        print(f"❌ Missing package: {module_name}")
        print(f"   Install with: pip install {pip_name}")
        print("   Or reinstall WhisperLocal: uv tool install --force whisperlocal")
        sys.exit(1)


np = _check_import("numpy")
sounddevice = _check_import("sounddevice")
sf = _check_import("soundfile")
pyperclip = _check_import("pyperclip")
rumps = _check_import("rumps")
_check_import("pynput.keyboard", "pynput")

from pynput.keyboard import Controller, Key, Listener  # noqa: E402

# ─── Cocoa imports for the floating overlay ──────────────────────────────────────
try:
    from AppKit import (
        NSBackingStoreBuffered,
        NSColor,
        NSFloatingWindowLevel,
        NSFont,
        NSMakeRect,
        NSScreen,
        NSTextField,
        NSVisualEffectBlendingModeBehindWindow,
        NSVisualEffectMaterialHUDWindow,
        NSVisualEffectView,
        NSWindow,
        NSWindowStyleMaskBorderless,
    )

    HAS_COCOA = True
except ImportError:  # pragma: no cover - only on a broken PyObjC install
    HAS_COCOA = False
    print("⚠️  PyObjC not available — the floating overlay is disabled")


# ─── Trigger key resolution ──────────────────────────────────────────────────────


def resolve_trigger_key(name: str) -> Key:
    """
    Turn a config key name such as "alt_r" into the pynput Key to watch for.

    Raises ValueError with the full list of options if the name is not one
    we support, so a typo in config.toml fails loudly at startup instead of
    silently never triggering.
    """
    if name not in cfg.TRIGGER_KEYS:
        raise ValueError(
            f"Unsupported trigger_key {name!r}. "
            f"Choose one of: {', '.join(cfg.TRIGGER_KEYS)}"
        )

    key = getattr(Key, name, None)
    if key is None:  # pragma: no cover - guards against a pynput API change
        raise ValueError(f"This version of pynput does not expose the {name!r} key")
    return key


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
        """Play a system sound without blocking."""
        if not self.enabled:
            return
        path = f"/System/Library/Sounds/{name}.aiff"
        if os.path.exists(path):
            subprocess.Popen(
                ["afplay", path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
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
    """Wraps mlx-whisper and holds the currently selected model."""

    def __init__(self, settings: cfg.Settings):
        self.settings = settings
        self.model_key = settings.model
        self.model_path = settings.model_path
        self._mlx_whisper = None
        self._loaded = False

    def _ensure_loaded(self) -> None:
        """Import mlx_whisper on first use — it is slow to load."""
        if self._loaded:
            return
        print("📦 Loading mlx-whisper...")
        try:
            import mlx_whisper
        except ImportError:
            print("❌ mlx-whisper is not installed. Run: pip install mlx-whisper")
            sys.exit(1)
        self._mlx_whisper = mlx_whisper
        self._loaded = True
        print(f"✅ mlx-whisper ready. Model: {self.model_path}")
        print("   (Weights download on first use — the first run takes longer.)")

    def transcribe(self, audio_path) -> str | None:
        """Transcribe an audio file. Returns None if anything went wrong."""
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
            print(f"❌ Transcription failed: {exc}")
            return None

    def switch_model(self, model_key: str) -> None:
        """Switch models. The new weights load on the next transcription."""
        if model_key not in cfg.MODEL_OPTIONS:
            return
        self.model_key = model_key
        self.model_path = cfg.MODEL_OPTIONS[model_key]
        self._loaded = False
        print(f"🔄 Model switched to {model_key} ({self.model_path})")


# ─── Audio recorder ──────────────────────────────────────────────────────────────


class AudioRecorder:
    """Captures audio from the default input device."""

    def __init__(self, settings: cfg.Settings):
        self.settings = settings
        self._frames: list = []
        self._stream = None
        self._recording = False
        self._start_time: float | None = None

    def start(self) -> None:
        """Open the microphone and start collecting frames."""
        self._frames = []
        self._recording = True
        self._start_time = time.time()

        def callback(indata, frames, time_info, status):
            if status:
                print(f"⚠️  Audio status: {status}")
            if self._recording:
                self._frames.append(indata.copy())

        self._stream = sounddevice.InputStream(
            samplerate=self.settings.sample_rate,
            channels=1,
            dtype="float32",
            callback=callback,
            blocksize=1024,
        )
        self._stream.start()
        print("🔴 Recording...")

    def stop(self) -> float:
        """Close the microphone and return how long we recorded for."""
        self._recording = False
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

        duration = time.time() - self._start_time if self._start_time else 0.0
        self._start_time = None
        print(f"⏹️  Recording stopped ({duration:.1f}s)")
        return duration

    def save(self, filepath) -> bool:
        """Write the captured audio to a WAV file."""
        if not self._frames:
            return False
        audio = np.concatenate(self._frames, axis=0)
        filepath.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(filepath), audio, self.settings.sample_rate)
        return True

    @property
    def is_recording(self) -> bool:
        return self._recording


# ─── Delivering the text ─────────────────────────────────────────────────────────


def deliver_text(text: str, paste_mode: str) -> bool:
    """
    Put the transcription where the user wants it.

    "paste" copies and then presses Cmd+V, which needs Accessibility
    permission. "clipboard" only copies — useful in apps that reject
    synthetic keystrokes, or if you would rather not grant Accessibility.
    """
    if not text:
        return False

    preview = text if len(text) <= 80 else text[:80] + "..."

    try:
        pyperclip.copy(text)
    except Exception as exc:
        print(f"❌ Could not copy to the clipboard: {exc}")
        return False

    if paste_mode == "clipboard":
        print(f'📋 Copied: "{preview}"')
        return True

    try:
        # Give the clipboard a moment to settle before we paste from it.
        time.sleep(0.05)
        keyboard = Controller()
        keyboard.press(Key.cmd)
        keyboard.press("v")
        keyboard.release("v")
        keyboard.release(Key.cmd)
    except Exception as exc:
        print(f"❌ Could not paste: {exc}")
        print("   The text is on your clipboard — press Cmd+V to paste it.")
        print("   To fix this, grant Accessibility permission to your terminal.")
        return False

    print(f'📋 Pasted: "{preview}"')
    return True


# ─── Floating overlay HUD ────────────────────────────────────────────────────────


class FloatingOverlay:
    """
    A translucent HUD near the top of the screen showing what the app is
    doing, so you are never guessing whether the microphone is open.
    """

    WINDOW_WIDTH = 260
    WINDOW_HEIGHT = 60
    CORNER_RADIUS = 16

    def __init__(self, enabled: bool = True):
        self._window = None
        self._label = None
        self._dot = None
        self._dot_visible = True
        self._pulse_running = False
        self._pulse_thread: threading.Thread | None = None

        if enabled and HAS_COCOA:
            self._build_window()

    def _build_window(self) -> None:
        """Create the borderless, click-through, always-on-top window."""
        screen = NSScreen.mainScreen()
        if not screen:
            return
        screen_frame = screen.frame()

        x = (screen_frame.size.width - self.WINDOW_WIDTH) / 2
        y = screen_frame.size.height - self.WINDOW_HEIGHT - 80
        frame = NSMakeRect(x, y, self.WINDOW_WIDTH, self.WINDOW_HEIGHT)

        self._window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame,
            NSWindowStyleMaskBorderless,
            NSBackingStoreBuffered,
            False,
        )
        self._window.setLevel_(NSFloatingWindowLevel + 1)
        self._window.setOpaque_(False)
        self._window.setBackgroundColor_(NSColor.clearColor())
        self._window.setHasShadow_(True)
        self._window.setIgnoresMouseEvents_(True)  # click-through
        self._window.setCollectionBehavior_(1 << 0 | 1 << 4)  # all spaces + transient

        content_view = NSVisualEffectView.alloc().initWithFrame_(
            NSMakeRect(0, 0, self.WINDOW_WIDTH, self.WINDOW_HEIGHT)
        )
        content_view.setMaterial_(NSVisualEffectMaterialHUDWindow)
        content_view.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
        content_view.setState_(1)
        content_view.setWantsLayer_(True)
        content_view.layer().setCornerRadius_(self.CORNER_RADIUS)
        content_view.layer().setMasksToBounds_(True)

        self._dot = self._make_label(NSMakeRect(20, 17, 26, 26), "🔴", bold=False, size=18)
        content_view.addSubview_(self._dot)

        self._label = self._make_label(
            NSMakeRect(50, 17, self.WINDOW_WIDTH - 70, 26), "Recording...", size=15
        )
        content_view.addSubview_(self._label)

        self._window.setContentView_(content_view)

    @staticmethod
    def _make_label(frame, text: str, *, bold: bool = True, size: int = 15):
        """Build a non-interactive, transparent text field."""
        field = NSTextField.alloc().initWithFrame_(frame)
        field.setStringValue_(text)
        field.setFont_(
            NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size)
        )
        field.setTextColor_(NSColor.whiteColor())
        field.setBezeled_(False)
        field.setDrawsBackground_(False)
        field.setEditable_(False)
        field.setSelectable_(False)
        return field

    @staticmethod
    def _on_main_thread(action) -> None:
        """Cocoa is not thread-safe; funnel every UI change to the main thread."""
        if threading.current_thread() is threading.main_thread():
            action()
            return
        try:
            from PyObjCTools import AppHelper

            AppHelper.callAfter(action)
        except Exception:
            pass

    def show(self, text: str = "Recording...", icon: str = "🔴") -> None:
        """Bring the overlay on screen."""
        if not self._window:
            return

        def _show():
            self._label.setStringValue_(text)
            self._dot.setStringValue_(icon)
            self._window.orderFrontRegardless()

        self._on_main_thread(_show)
        self._start_pulse()

    def hide(self) -> None:
        """Take the overlay off screen."""
        if not self._window:
            return
        self._stop_pulse()
        self._on_main_thread(lambda: self._window.orderOut_(None))

    def update_text(self, text: str, icon: str | None = None) -> None:
        """Change what the overlay says without hiding it."""
        if not self._window:
            return

        def _update():
            self._label.setStringValue_(text)
            if icon:
                self._dot.setStringValue_(icon)

        self._on_main_thread(_update)

    def _start_pulse(self) -> None:
        """Blink the dot so a frozen overlay is obvious."""
        if self._pulse_running:
            return
        self._pulse_running = True
        self._dot_visible = True

        def _loop():
            while self._pulse_running:
                time.sleep(0.6)
                if not self._pulse_running:
                    break

                def _toggle():
                    self._dot_visible = not self._dot_visible
                    self._dot.setHidden_(not self._dot_visible)

                self._on_main_thread(_toggle)

        self._pulse_thread = threading.Thread(target=_loop, daemon=True)
        self._pulse_thread.start()

    def _stop_pulse(self) -> None:
        """Stop blinking and leave the dot visible."""
        self._pulse_running = False
        if not self._dot:
            return

        def _restore():
            self._dot.setHidden_(False)
            self._dot_visible = True

        self._on_main_thread(_restore)


# ─── The state machine ───────────────────────────────────────────────────────────


class DictationEngine:
    """
    IDLE → (hold the trigger key past the threshold) → RECORDING
         → (release) → TRANSCRIBING → IDLE
    """

    IDLE = "idle"
    WAITING = "waiting"  # key is down but the hold threshold is not met yet
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"

    def __init__(
        self,
        transcriber: Transcriber,
        settings: cfg.Settings,
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

        self.state = self.IDLE
        self.trigger_key = resolve_trigger_key(settings.trigger_key)
        self.last_transcription = ""

        self._threshold_timer: threading.Timer | None = None
        self._enabled = True

    # ── state ────────────────────────────────────────────────────────────────

    def _set_state(self, new_state: str) -> None:
        """Move to a new state and tell the overlay and menu bar about it."""
        previous, self.state = self.state, new_state
        print(f"   State: {previous} → {new_state}")

        if self.overlay:
            if new_state == self.RECORDING:
                self.overlay.show("Recording...", "🔴")
            elif new_state == self.TRANSCRIBING:
                self.overlay.update_text("Transcribing...", "⚙️")
            else:
                self.overlay.hide()

        if self.on_state_change:
            self.on_state_change(new_state)

    # ── key events ───────────────────────────────────────────────────────────

    def _is_trigger_key(self, key) -> bool:
        return key == self.trigger_key

    def on_key_press(self, key) -> None:
        """Start the hold timer when the trigger key goes down."""
        if not self._enabled or not self._is_trigger_key(key):
            return

        if self.state == self.IDLE:
            self._set_state(self.WAITING)
            self._threshold_timer = threading.Timer(
                self.settings.hold_threshold, self._threshold_reached
            )
            self._threshold_timer.daemon = True
            self._threshold_timer.start()

    def on_key_release(self, key) -> None:
        """Cancel a short tap, or finish a real recording."""
        if not self._is_trigger_key(key):
            return

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
            print(f"❌ Could not start recording: {exc}")
            print("   Grant Microphone permission to your terminal, then try again.")
            self.sounds.error()
            self._set_state(self.IDLE)

    def _stop_and_transcribe(self) -> None:
        """Close the microphone, then transcribe off the main thread."""
        duration = self.recorder.stop()
        self.sounds.stop()

        if duration < self.settings.min_recording_duration:
            print("   (Too short — discarded)")
            self._set_state(self.IDLE)
            return

        audio_file = cfg.temp_audio_file()
        if not self.recorder.save(audio_file):
            print("   (Nothing was recorded)")
            self.sounds.error()
            self._set_state(self.IDLE)
            return

        self._set_state(self.TRANSCRIBING)

        def _work():
            try:
                text = self.transcriber.transcribe(audio_file)
                if text:
                    self.sounds.done()
                    deliver_text(text, self.settings.paste_mode)
                    self.last_transcription = text
                else:
                    self.sounds.error()
                    print("   (No speech recognized)")
            finally:
                try:
                    audio_file.unlink()
                except OSError:
                    pass
                self._set_state(self.IDLE)

        threading.Thread(target=_work, daemon=True).start()

    # ── on/off ───────────────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value
        if not value and self.state == self.RECORDING:
            self.recorder.stop()
            self._set_state(self.IDLE)


# ─── Menu bar app ────────────────────────────────────────────────────────────────


class WhisperLocalApp(rumps.App):
    """The 🎙️ in your menu bar."""

    def __init__(self, engine: DictationEngine, transcriber: Transcriber):
        super().__init__(cfg.APP_NAME, title=cfg.ICON_IDLE, quit_button=None)
        self.engine = engine
        self.transcriber = transcriber

        self.status_item = rumps.MenuItem("Status: Idle")
        self.status_item.set_callback(None)
        self.last_text_item = rumps.MenuItem("Last: (none)", callback=self._copy_last)
        self.toggle_item = rumps.MenuItem("Enabled ✅", callback=self._toggle)

        self.model_menu = rumps.MenuItem("Model")
        for key in cfg.MODEL_OPTIONS:
            marker = "●" if key == transcriber.model_key else "○"
            self.model_menu.add(
                rumps.MenuItem(f"{marker} {key}", callback=self._make_model_callback(key))
            )

        self.menu = [
            self.status_item,
            self.last_text_item,
            None,
            self.toggle_item,
            self.model_menu,
            None,
            rumps.MenuItem("Quit", callback=self._quit),
        ]

        self.engine.on_state_change = self._on_state_change

    def _on_state_change(self, state: str) -> None:
        """Reflect the engine state in the icon and the status line."""
        icons = {
            DictationEngine.IDLE: cfg.ICON_IDLE,
            DictationEngine.WAITING: cfg.ICON_WAITING,
            DictationEngine.RECORDING: cfg.ICON_RECORDING,
            DictationEngine.TRANSCRIBING: cfg.ICON_TRANSCRIBING,
        }
        self.title = icons.get(state, cfg.ICON_IDLE)

        hint = f"Hold {self.engine.settings.trigger_label} to dictate"
        labels = {
            DictationEngine.IDLE: f"Status: Idle — {hint}",
            DictationEngine.WAITING: "Status: Hold detected...",
            DictationEngine.RECORDING: "Status: 🔴 Recording...",
            DictationEngine.TRANSCRIBING: "Status: ⚙️ Transcribing...",
        }
        self.status_item.title = labels.get(state, "Status: Unknown")

        text = self.engine.last_transcription
        if text:
            display = text if len(text) <= 60 else text[:60] + "..."
            self.last_text_item.title = f"Last: {display}"

    def _toggle(self, sender) -> None:
        """Turn dictation on or off without quitting."""
        self.engine.enabled = not self.engine.enabled
        if self.engine.enabled:
            sender.title = "Enabled ✅"
            self.title = cfg.ICON_IDLE
            print("▶️  Dictation enabled")
        else:
            sender.title = "Disabled ❌"
            self.title = "⏸️"
            print("⏸️  Dictation disabled")

    def _copy_last(self, sender) -> None:
        """Put the last transcription back on the clipboard."""
        text = self.engine.last_transcription
        if not text:
            return
        pyperclip.copy(text)
        rumps.notification(cfg.APP_NAME, "Copied", text[:100])

    def _make_model_callback(self, model_key: str):
        """Build the click handler for one entry in the Model submenu."""

        def callback(sender):
            self.transcriber.switch_model(model_key)
            # rumps keys its menu dict by title, so snapshot the keys before
            # renaming anything — mutating titles mid-iteration breaks it.
            for item_key in list(self.model_menu.keys()):
                item = self.model_menu[item_key]
                name = item.title.lstrip("●○ ").strip()
                item.title = f"{'●' if name == model_key else '○'} {name}"
            rumps.notification(
                cfg.APP_NAME, "Model changed", f"Now using {model_key}"
            )

        return callback

    def _quit(self, sender) -> None:
        print("👋 WhisperLocal shutting down...")
        rumps.quit_application()


# ─── Entry point ─────────────────────────────────────────────────────────────────


def run(settings: cfg.Settings) -> int:
    """Start the menu bar app. Blocks until the user quits."""
    try:
        resolve_trigger_key(settings.trigger_key)
    except ValueError as exc:
        print(f"❌ {exc}")
        print(f"   Edit {cfg.config_path()} to fix it.")
        return 1

    print("=" * 60)
    print("  🎙️  WhisperLocal — push-to-talk dictation")
    print("=" * 60)
    print()
    print(f"  Trigger:   Hold {settings.trigger_label} for {settings.hold_threshold}s")
    print(f"  Model:     {settings.model} ({settings.model_path})")
    print(f"  Language:  {settings.language}")
    print(f"  Delivery:  {settings.paste_mode}")
    print(f"  Config:    {cfg.config_path()}")
    print()
    print("  Not working? Run: whisperlocal doctor")
    print()

    transcriber = Transcriber(settings)
    overlay = FloatingOverlay(enabled=settings.overlay)
    sounds = Sounds(enabled=settings.sounds)
    engine = DictationEngine(transcriber, settings, overlay=overlay, sounds=sounds)

    listener = Listener(on_press=engine.on_key_press, on_release=engine.on_key_release)
    listener.daemon = True
    listener.start()
    print(f"✅ Listening for {settings.trigger_label}")

    def _prewarm():
        """Load the model now so the first dictation is not the slow one."""
        print("🔄 Warming up the model (the first run downloads weights)...")
        transcriber._ensure_loaded()
        print("✅ Ready")
        sounds.done()

    threading.Thread(target=_prewarm, daemon=True).start()

    print("✅ Menu bar app starting — look for 🎙️ in your menu bar")
    print()
    WhisperLocalApp(engine, transcriber).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(run(cfg.load()))
