# Changelog

All notable changes to WhisperLocal are recorded here.
This project follows [Semantic Versioning](https://semver.org/).

## [1.1.0] — 2026-08-07

The engine that was actually in daily use, packaged properly — plus triggers
you can pick.

### Added

- **Several triggers at once.** `trigger_keys` takes a list, and holding any one
  of them dictates. Whichever you press first owns the recording until you let
  go, so pressing a second trigger mid-sentence no longer cuts you off.
- **Mouse buttons as triggers** — `mouse_left`, `mouse_right`, `mouse_middle`,
  for dictating when you are not near the keyboard. Two guards make this
  workable rather than maddening:
  - `mouse_hold_threshold` (default `1.0`, minimum `0.3`) is separate from the
    key threshold, which defaults to `0` and would otherwise make every click
    start a recording.
  - `mouse_drag_cancel_px` (default `10`) cancels the trigger once a press
    starts travelling, because that is a drag or a text selection, not someone
    holding still to talk. It applies only *before* recording starts — once you
    are recording you can move the mouse freely.
- **`whisperlocal stats`** — the dictation history summary, previously a
  standalone `history_stats.py`. Words, speaking rate, transcription latency,
  outcome rates including hallucinations, per-app breakdown and words per day.
- **`whisperlocal doctor`** now also checks whether the Fn event tap can be
  created, which is the single most common reason the Fn trigger silently does
  nothing.
- **`history_text = false`** keeps the statistics without storing the
  transcribed words.
- **Layered configuration** — defaults, then `~/.config/whisperlocal/config.toml`,
  then `WHISPERLOCAL_*` environment variables, then command-line flags. The old
  single-value `trigger_key` is still accepted.
- **`--trigger`** to try a different trigger for one run without editing config.

### Changed

- Fn (globe) is now the default trigger, replacing Right Option.
- `hold_threshold` defaults to `0.0` — recording starts the instant the key goes
  down, so you can talk straight away. Stray taps are still discarded by
  `min_recording_duration`.
- The overlay is a single small dot near the bottom of the screen — red and
  breathing while recording, steady amber while transcribing — instead of a
  panel with text.
- Menu bar icons are SF Symbols, tinted, rather than emoji.
- The model is chosen in config rather than from a menu. Every switch in the old
  menu pulled another 85–500 MB into the Hugging Face cache and left it there.
- History is written to Application Support, deliberately not Documents, which
  is iCloud-synced on many Macs and would upload a full record of everything
  dictated.

### Fixed

Carried over from the version in daily use, none of which was in 1.0.0:

- **Whisper repetition loops were being pasted.** On short or noisy audio the
  decoder gets stuck emitting one token — observed as 112 words of "ARP", 223 of
  "funny". Output is now checked for a long repeated run and for a low
  unique-word ratio, and discarded rather than dumped into whatever you had
  focused. Tuned against measured data so real speech survives.
- **Recording at a rate the hardware does not run at segfaulted CoreAudio.**
  Asking PortAudio for 16 kHz on a 48 kHz mic forces its rate-adapting path,
  which crashes on the realtime thread. Recording now runs at the device's
  native rate; whisper's ffmpeg loader downsamples on read.
- **Audio was drained on CoreAudio's realtime thread.** Handing a Python
  callback to PortAudio ran the interpreter there and crashed with
  EXC_BAD_ACCESS via cffi's closure trampoline. A thread we own drains the
  stream instead.
- **Pasting crashed the process with SIGTRAP.** Constructing pynput's
  `Controller` reaches HIToolbox Text Services, which asserts the main dispatch
  queue — fatal off the main thread. Quartz posts the keystroke directly,
  skipping the layout map.
- **Menu bar updates ran off the main thread**, tripping the same AppKit
  assertion. All of it now goes through a single `run_on_main()` boundary.
- **The Fn key could not be used as a trigger at all.** It is not a keycode,
  only a modifier flag bit, so pynput cannot see it. A Quartz event tap watches
  the flag, re-arming itself if macOS disables it for slowness.

## [1.0.0] — 2026-08-07

First public release: packaging, one-command install and uninstall, and the
fixes below.

### Added

- One-command `install.sh` and `uninstall.sh`.
- `whisperlocal doctor` and `whisperlocal config`.
- `paste_mode = "clipboard"` for apps that block synthetic input.
- `language = "auto"`.
- Installable package with a `whisperlocal` entry point.

### Fixed

- **Model repository IDs were broken.** `mlx-community/whisper-base` and
  `whisper-small` no longer resolve and fail with HTTP 401. All model IDs use
  the working `-mlx` repositories.
- **`trigger_key` was ignored** — documented as configurable but hardcoded.
- **The app only ran from its own directory**; recordings were written next to
  the source files.
- **Intel Macs got an opaque MLX crash** instead of an explanation.
- **Switching models could raise a `RuntimeError`** by renaming menu items while
  iterating the menu keyed by those names.

[1.1.0]: https://github.com/shivamdixit17/whisperlocal/releases/tag/v1.1.0
[1.0.0]: https://github.com/shivamdixit17/whisperlocal/releases/tag/v1.0.0
