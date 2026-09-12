# Changelog

All notable changes to WhisperLocal are recorded here.
This project follows [Semantic Versioning](https://semver.org/).

## [1.3.1] — 2026-09-12

- **Top terms spans the dashboard.** The card sat in the left half of a
  two-column row with nothing beside it; it now takes the full width, so the
  word and phrase lists have room instead of an empty box next to them.

## [1.3.0] — 2026-09-12

Meetings get recorded and transcribed, the stats get a real dashboard and a
settings page, and there is an opt-in cloud backend for the times a bigger
model is worth the upload.

### Added

- **Meeting recording.** When Zoom, Teams, FaceTime, Slack, Webex, Discord or
  a browser call (Google Meet and friends) has the microphone open, a
  notification offers to record it. `meeting_auto_record = true` starts without
  asking and stops when the call ends; `meeting_prompt` chooses a notification,
  a dialog or nothing. There are also Start / Stop Meeting Recording items in
  the menu for calls nothing detects.

  Detection watches CoreAudio's per-process "input running" flag — the same
  thing that lights the orange dot in the menu bar — filtered to processes
  that look like meeting apps, so it needs no plugin, no account and no
  calendar.

  It captures your microphone and, through a CoreAudio process tap (macOS
  14.2+), the other participants — which is why they are heard even when you
  are on headphones. The first recording asks for "System Audio Recording
  Only"; refused, or on an older macOS, it falls back to the microphone alone
  or to a virtual device named in `meeting_system_device` (BlackHole, say).
  Audio is written in segments of about a minute and transcribed while the
  call is still running (`meeting_transcribe_live`), with "You" / "Others"
  labels from the two tracks, so the transcript is ready moments after you
  hang up. Nothing is held in memory; a crash loses at most the segment being
  written.

  Each meeting is a folder under
  `~/Library/Application Support/WhisperLocal/meetings/<id>/` —
  `meeting.json`, `transcript.json`, `transcript.md` and, with
  `meeting_keep_audio`, `audio/mic.flac` and `audio/system.flac`. Plain files,
  so a meeting can be read or deleted in the Finder. Browse, search, export
  (Markdown, text, JSON) and delete them from the Meetings page or with
  `whisperlocal meetings list|show|search|export|delete|transcribe`. Meeting
  totals are on the dashboard and in `whisperlocal stats --meetings`.

- **A settings page and an analytics dashboard**, served to your own browser
  from 127.0.0.1 (`web_port`, default 47311; `web_enabled = false` turns the
  server off). Open them from the menu bar — Dashboard…, Settings…,
  Meetings… — or with `whisperlocal dashboard`. The link carries a per-run
  token that becomes a cookie, only loopback is accepted, and the page loads
  nothing from the internet: Chart.js ships inside the package.

  Settings save into `config.toml` with your comments preserved and apply
  immediately — changing the trigger keys restarts the listeners without
  restarting the app. Only the `web_*` settings need a restart, and there is a
  Restart button (and a Restart menu item) for that. A "Record key" button
  captures the next key or held mouse button, so nobody has to know that
  their spare key is called `f13`. A setting overridden by an environment
  variable is shown read-only, since the file would be ignored.

- **Analytics over `history.jsonl`**: totals and the time saved against
  typing, words per day and per week with the week-over-week change, an
  hour × weekday heatmap, outcome and hallucination-rate trends, a per-app
  breakdown, speaking-rate distribution, transcription latency by model (p50
  and p95), dictation lengths, streaks, top words and bigrams, and a
  searchable table of recent dictations. `whisperlocal stats --json` prints
  the same data for anything else to read.

- **Cloud transcription, opt-in.** `dictation_backend` and `meeting_backend`
  are each `"local"` (the default — nothing on the network) or `"api"`, which
  posts audio to any server speaking the OpenAI `/audio/transcriptions`
  protocol (`api_base_url`, `api_model`, `api_timeout_seconds`) — OpenAI,
  Groq, a faster-whisper server of your own. The two are chosen separately so
  quick dictation can stay local while long meetings go to a bigger model, or
  the reverse.

  The key lives in the macOS Keychain, stored by `whisperlocal api-key set`,
  never in `config.toml`. Audio is re-encoded to 16 kHz mono AAC with ffmpeg
  before upload, so an hour of meeting is a few megabytes rather than the raw
  recording. A failed request pastes nothing and is logged as `backend_error`
  in the history, whose entries now carry a `backend` field.

- **`whisperlocal dashboard`**, **`api-key`**, **`meetings`**, and
  `stats --json` / `--meetings`.

### Changed

- **The menu bar icon is monochrome.** It is a template image now, so it
  follows light and dark mode like the system's own status icons, drawn from
  the waveform glyph family. Config files written by `config --init` before
  1.3 contain the old orange colour and the old glyph names as literal values;
  those are recognised as the pre-1.3 defaults and ignored with a one-line
  note, so the change reaches everyone rather than only fresh installs. Set
  `icon_color` or the `icon_*` names to something else and they are honoured.
  The Dock and Finder icon is unchanged: it lives in the sealed bundle, and
  touching it would cost another permission re-grant.

- **One logo.** A waveform-in-a-circle mark (`docs/logo.svg`) is the favicon
  and brand mark of the web pages and the image at the top of the README; the
  menu bar uses the matching SF Symbol `waveform` family. The README gained
  screenshots of the dashboard, settings and meetings pages, taken from the
  real UI on generated sample data.

- **The app bundle gains one line.** Meeting recording needs
  `NSAudioCaptureUsageDescription` in `Info.plist`. In practice the ad-hoc
  seal of this script-launched bundle turned out not to cover the plist, so
  on macOS 26 the cdhash — and the Accessibility and Input Monitoring grants —
  survived the rewrite. `whisperlocal install-app` checks the signature
  before and after: if it did change on your Mac, it clears the stale toggles
  with `tccutil` so a fresh prompt appears, and says so. This is the only
  planned change to the sealed files.

- **`app.py` is split up.** Audio capture (`audio.py`), the local engine, the
  backends and long-form transcription (`transcription/local.py`,
  `backends.py`, `longform.py`), meeting detection, recording and storage
  (`meetingdetect.py`, `meetingrecorder.py`, `meetings.py`), the system-audio
  tap (`systemaudio.py`), the web server (`web/`), live settings and the
  comment-preserving config writer (`settings_manager.py`,
  `config_writer.py`), key names (`keymap.py`), the dashboard numbers
  (`analytics.py`) and the Keychain (`keychain.py`) are their own modules.
  Everything that does not need macOS has tests: a pytest suite of 271 tests
  runs on Linux CI, which until now could only check that the package built.

- New dependency: `pyobjc-framework-CoreAudio`, for the process tap.

## [1.2.1] — 2026-08-07

### Fixed

- **It was using about 1.6 GB of memory.** Almost none of that was the model,
  which is ~140 MB. MLX keeps freed GPU buffers in a cache to reuse them, and
  unbounded that reached ~950 MB after a few dictations and was never returned —
  absurd for something that idles in the menu bar all day.

  The cache is now capped (`mlx_cache_mb`, default 128 MB) and dropped entirely
  after a minute without dictation (`idle_release_seconds`). Idle footprint goes
  from ~1.6 GB to ~430 MB; a dictation costs about 35 ms more.

  Measured with whisper-base, five transcriptions each:

  | `mlx_cache_mb` | median | footprint |
  |---|---:|---:|
  | unlimited (old) | 230 ms | 1423 MB |
  | 128 (new default) | 265 ms | 660 MB |
  | 64 | 298 ms | 556 MB |
  | 0 | 364 ms | 491 MB |

- `whisperlocal doctor` now reports the running app's actual memory use.

## [1.2.0] — 2026-08-07

It installs as a real Mac app now, and the recording dot follows your text
cursor instead of sitting in a corner.

### Added

- **A proper menu bar app.** The installer builds
  `~/Applications/WhisperLocal.app`, signs it, registers it as a Login Item and
  launches it. The terminal is needed exactly once, to install. After that it
  starts at login and survives reboots — no command to run, no window to keep
  open.

  This also fixes the worst part of the old setup: macOS attached the
  Accessibility and Input Monitoring grants to *whichever terminal* started the
  app, so they broke as soon as you used a different one. They now belong to
  WhisperLocal itself.

  The bundle is byte-identical across every release and every machine, and
  `install-app` compares before it writes. macOS keys those grants to the
  bundle's cdhash, so an upgrade that rebuilt it would silently revoke them.
  Verified: a bundle built from scratch produces the same cdhash as one that has
  been installed and granted for weeks.

- **`whisperlocal install-app` / `uninstall-app`** — install, repair or remove
  the app and its login item.

- **The recording dot follows your text cursor.** It appears next to wherever
  you are actually typing, on whichever monitor, rather than fixed above the
  Dock. New `overlay_anchor` (`"caret"`, `"mouse"`, `"bottom"`) with
  `overlay_offset_x` / `overlay_offset_y`.

  Cursor position is read through the Accessibility API with a 0.25 s messaging
  timeout — this runs on the key path, and with the Fn trigger that is the event
  tap's callback on the main runloop, exactly where a blocking call gets the tap
  disabled for being slow. Apps that do not report a cursor (many Electron apps,
  some browser fields) fall back to the mouse pointer, then to the bottom of the
  screen.

- **`whisperlocal probe-caret`** — shows what the focused app reports about its
  cursor, so "why is the dot at my mouse in this app?" has an answer.

- **A first-run permissions prompt** from inside the app, with a button that
  opens the right System Settings pane, plus a **Permissions…** menu item to
  re-check. The microphone still uses the native macOS prompt.

### Fixed

- **Restarting could leave the old process running**, giving two Fn listeners
  and two menu bar icons. `pgrep -f "-m whisperlocal"` parses the leading `-m`
  as an option and matches nothing, silently — so stopping the supervisor
  orphaned its child rather than killing it.

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

[1.3.1]: https://github.com/shivamdixit17/whisperlocal/releases/tag/v1.3.1
[1.3.0]: https://github.com/shivamdixit17/whisperlocal/releases/tag/v1.3.0
[1.2.1]: https://github.com/shivamdixit17/whisperlocal/releases/tag/v1.2.1
[1.2.0]: https://github.com/shivamdixit17/whisperlocal/releases/tag/v1.2.0
[1.1.0]: https://github.com/shivamdixit17/whisperlocal/releases/tag/v1.1.0
[1.0.0]: https://github.com/shivamdixit17/whisperlocal/releases/tag/v1.0.0
