# Changelog

All notable changes to WhisperLocal are recorded here.
This project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Planned

- **Local dictation analytics (v1.1)** — an opt-in, on-device view of where
  your effort goes: word volume and time grouped by app and topic, KPIs such as
  sessions per day and active hours, trends week over week, and a local
  dashboard via `whisperlocal stats`. Off by default, stored only on your Mac,
  never uploaded, and deletable with one command. See the roadmap in the README.

## [1.0.0] — 2026-08-07

First public release. WhisperLocal went from a script that ran in its own
folder to a tool anyone can install with one command.

### Added

- **One-command install** — `install.sh` sets up uv, ffmpeg and WhisperLocal,
  then explains the macOS permissions it needs.
- **One-command uninstall** — `uninstall.sh` removes the command, settings and
  cached audio. Model weights are kept unless `REMOVE_MODELS=1` is set, because
  the Hugging Face cache is shared with other tools.
- **`whisperlocal doctor`** — checks architecture, Python, ffmpeg, microphone,
  Accessibility permission and whether the model is downloaded, with a deep
  link to each System Settings pane that needs attention.
- **`whisperlocal config`** — `--init`, `--show`, `--path` for a real config
  file at `~/.config/whisperlocal/config.toml`.
- **Layered configuration** — defaults, then config file, then `WHISPERLOCAL_*`
  environment variables, then command-line flags.
- **`paste_mode = "clipboard"`** — copy transcriptions without simulating ⌘V,
  for apps that block synthetic input or if you would rather not grant
  Accessibility permission.
- **`language = "auto"`** — let Whisper detect the spoken language.
- **More models** — `medium` and `large-v3-turbo` join tiny, base and small.
- **Installable package** — `pyproject.toml` with a `whisperlocal` entry point.

### Fixed

- **Model repository IDs were broken.** `mlx-community/whisper-base` and
  `whisper-small` no longer resolve and fail with HTTP 401 on download. All
  model IDs now use the working `-mlx` suffixed repositories, so a fresh
  install can actually fetch weights.
- **`trigger_key` was ignored.** The setting existed and was documented, but
  the key was hardcoded to Right Option. It now works, and accepts
  `alt_r`, `alt_l`, `cmd_r`, `ctrl_r`, `shift_r` and `f13`–`f19`. An
  unsupported value fails at startup with the list of valid options instead of
  silently never triggering.
- **The app only ran from its own directory.** Recorded audio was written next
  to the source files; it now goes to `~/Library/Caches/WhisperLocal/`.
- **Intel Macs got an opaque MLX crash.** They now get an explanation, checked
  before any heavy import.
- **Switching models could raise a `RuntimeError`.** The menu callback renamed
  items while iterating the menu that is keyed by those names.
- **A missing `numpy` bypassed the friendly import error** it was supposed to
  produce.
- **Microphone failures were unhandled** — starting a recording without
  permission now explains itself instead of raising.

### Changed

- `fp16` now defaults to `true`, matching mlx-whisper's own default. The
  previous hardcoded `false` gave up half precision on hardware built for it.
- `setup.sh` and `start.sh` are gone, replaced by `install.sh` and the
  installed `whisperlocal` command. The from-source workflow is now
  `uv sync && uv run whisperlocal`.

[1.0.0]: https://github.com/shivamdixit17/whisperlocal/releases/tag/v1.0.0
