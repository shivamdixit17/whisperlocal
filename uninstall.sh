#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# WhisperLocal uninstaller
#
#   curl -fsSL https://raw.githubusercontent.com/shivamdixit17/whisperlocal/main/uninstall.sh | bash
#
# Removes the whisperlocal command, your settings and the cached audio.
# Your dictation history is deliberately NOT touched — delete
# ~/Library/Application Support/WhisperLocal/ yourself if you want it gone.
# Downloaded model weights are listed but kept, because the Hugging Face cache
# is shared with any other ML tool you use. To remove those as well:
#
#   curl -fsSL .../uninstall.sh | REMOVE_MODELS=1 bash
#
# uv and ffmpeg are always left installed — you probably use them elsewhere.
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

TOOL_NAME="whisperlocal"

BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'
YELLOW=$'\033[33m'; BLUE=$'\033[34m'; RESET=$'\033[0m'

ok()   { printf "%s✓%s %s\n" "$GREEN" "$RESET" "$1"; }
warn() { printf "%s!%s %s\n" "$YELLOW" "$RESET" "$1"; }
info() { printf "%s→%s %s\n" "$BLUE" "$RESET" "$1"; }

printf "\n%s┌────────────────────────────────────────────┐%s\n" "$BOLD" "$RESET"
printf "%s│  🎙️  WhisperLocal — Uninstall              │%s\n" "$BOLD" "$RESET"
printf "%s└────────────────────────────────────────────┘%s\n\n" "$BOLD" "$RESET"

export PATH="$HOME/.local/bin:$PATH"

removed_anything=0

# ── The menu bar app, login item and bundle ──────────────────────────────────
# Done first, while the command that knows how to undo it still exists.
if command -v whisperlocal &>/dev/null; then
    whisperlocal uninstall-app 2>&1 | sed 's/^/  /' || true
    removed_anything=1
fi

# ── The command itself ───────────────────────────────────────────────────────
if command -v uv &>/dev/null && uv tool list 2>/dev/null | grep -q "^${TOOL_NAME}"; then
    uv tool uninstall "$TOOL_NAME" >/dev/null 2>&1
    ok "Removed the whisperlocal command"
    removed_anything=1
elif command -v whisperlocal &>/dev/null; then
    warn "whisperlocal is on your PATH but was not installed by uv."
    printf "    It lives at: %s\n" "$(command -v whisperlocal)"
    printf "    Remove it with whatever installed it (pip uninstall whisperlocal, etc.)\n"
else
    info "The whisperlocal command is not installed"
fi

# ── Settings and scratch files ───────────────────────────────────────────────
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/whisperlocal"
CACHE_DIR="$HOME/Library/Caches/WhisperLocal"

if [[ -d "$CONFIG_DIR" ]]; then
    rm -rf "$CONFIG_DIR"
    ok "Removed your settings ($CONFIG_DIR)"
    removed_anything=1
fi

if [[ -d "$CACHE_DIR" ]]; then
    rm -rf "$CACHE_DIR"
    ok "Removed cached audio ($CACHE_DIR)"
    removed_anything=1
fi

# ── Model weights (kept unless asked) ────────────────────────────────────────
HUB="$HOME/.cache/huggingface/hub"
models=()
if [[ -d "$HUB" ]]; then
    while IFS= read -r dir; do
        [[ -n "$dir" ]] && models+=("$dir")
    done < <(find "$HUB" -maxdepth 1 -type d -name 'models--mlx-community--whisper-*' 2>/dev/null)
fi

if (( ${#models[@]} > 0 )); then
    printf "\n%sWhisper models in your Hugging Face cache:%s\n" "$BOLD" "$RESET"
    for m in "${models[@]}"; do
        printf "   %-6s %s\n" "$(du -sh "$m" 2>/dev/null | cut -f1)" "$(basename "$m")"
    done

    if [[ "${REMOVE_MODELS:-}" == "1" ]]; then
        rm -rf "${models[@]}"
        printf "\n"
        ok "Removed the model weights"
        removed_anything=1
    else
        printf "\n%sKept — this cache is shared with other Hugging Face tools.%s\n" "$DIM" "$RESET"
        printf "To delete them too, re-run with:  REMOVE_MODELS=1\n"
    fi
fi

printf "\n"
if (( removed_anything )); then
    printf "%sDone.%s WhisperLocal has been removed.\n" "$GREEN" "$RESET"
else
    printf "%sNothing to do — WhisperLocal was not installed.%s\n" "$DIM" "$RESET"
fi
printf "%suv and ffmpeg were left installed.%s\n\n" "$DIM" "$RESET"
