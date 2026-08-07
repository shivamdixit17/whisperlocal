#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# WhisperLocal installer
#
#   curl -fsSL https://raw.githubusercontent.com/shivamdixit17/whisperlocal/main/install.sh | bash
#
# Installs uv and ffmpeg if they are missing, then installs WhisperLocal as an
# isolated command-line tool. Nothing is installed into your system Python.
#
# To remove everything again:  ./install.sh --uninstall
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

REPO_URL="https://github.com/shivamdixit17/whisperlocal.git"
TOOL_NAME="whisperlocal"

BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'
YELLOW=$'\033[33m'; BLUE=$'\033[34m'; RESET=$'\033[0m'

info()  { printf "%s→%s %s\n" "$BLUE" "$RESET" "$1"; }
ok()    { printf "%s✓%s %s\n" "$GREEN" "$RESET" "$1"; }
warn()  { printf "%s!%s %s\n" "$YELLOW" "$RESET" "$1"; }
die()   { printf "%s✗%s %s\n" "$RED" "$RESET" "$1" >&2; exit 1; }

banner() {
    printf "\n%s┌────────────────────────────────────────────┐%s\n" "$BOLD" "$RESET"
    printf "%s│  🎙️  WhisperLocal — %-22s │%s\n" "$BOLD" "$1" "$RESET"
    printf "%s└────────────────────────────────────────────┘%s\n\n" "$BOLD" "$RESET"
}

# ── Make sure this machine can actually run it ───────────────────────────────
check_platform() {
    [[ "$(uname -s)" == "Darwin" ]] || die \
        "WhisperLocal only runs on macOS (this looks like $(uname -s))."

    if [[ "$(uname -m)" != "arm64" ]]; then
        die "WhisperLocal needs an Apple Silicon Mac (M1 or newer).
   This Mac reports '$(uname -m)'. MLX cannot run on Intel Macs."
    fi
    ok "macOS on Apple Silicon"
}

ensure_path() {
    # uv puts tool executables in ~/.local/bin.
    export PATH="$HOME/.local/bin:$PATH"
}

# ── uv: the Python package manager we install through ────────────────────────
ensure_uv() {
    if command -v uv &>/dev/null; then
        ok "uv $(uv --version | awk '{print $2}')"
        return
    fi

    info "Installing uv (a fast Python package manager)..."
    curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 \
        || die "Could not install uv. See https://docs.astral.sh/uv/ to install it yourself."
    ensure_path
    command -v uv &>/dev/null || die \
        "uv installed but is not on your PATH. Open a new terminal and run this script again."
    ok "uv installed"
}

# ── ffmpeg: mlx-whisper shells out to it to decode audio ─────────────────────
ensure_ffmpeg() {
    if command -v ffmpeg &>/dev/null; then
        ok "ffmpeg"
        return
    fi

    if ! command -v brew &>/dev/null; then
        die "ffmpeg is required but missing, and Homebrew is not installed.
   Install Homebrew first:
     /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\"
   Then run this installer again."
    fi

    info "Installing ffmpeg via Homebrew (this can take a minute)..."
    brew install ffmpeg >/dev/null 2>&1 || die "brew install ffmpeg failed. Run it yourself to see why."
    ok "ffmpeg installed"
}

install_tool() {
    info "Installing WhisperLocal (downloads ~1 GB of dependencies — grab a coffee)..."
    uv tool install --force "git+${REPO_URL}" >/dev/null 2>&1 \
        || die "Installation failed. Re-run without the pipe to see the full error:
     curl -fsSL https://raw.githubusercontent.com/shivamdixit17/whisperlocal/main/install.sh -o install.sh
     bash install.sh"
    uv tool update-shell >/dev/null 2>&1 || true
    ok "WhisperLocal installed"
}

post_install() {
    printf "\n%sOne thing left: permissions.%s\n\n" "$BOLD" "$RESET"
    cat <<'EOF'
macOS grants these to the app that LAUNCHES WhisperLocal — your terminal —
not to WhisperLocal itself. Open System Settings → Privacy & Security and
add your terminal to all three:

   • Input Monitoring   so it can see the trigger while other apps are focused,
                        and create the Fn event tap
   • Microphone         so it can hear you
   • Accessibility      so it can paste the text at your cursor

Then quit and reopen your terminal for them to take effect.

EOF
    printf "%sVerify everything:%s  whisperlocal doctor\n" "$BOLD" "$RESET"
    printf "%sStart dictating:%s    whisperlocal\n\n" "$BOLD" "$RESET"
    printf "%sHold the Fn (globe) key, speak, let go.%s\n\n" "$DIM" "$RESET"

    if ! command -v whisperlocal &>/dev/null; then
        warn "'whisperlocal' is not on your PATH yet — open a new terminal, or run:"
        printf "    export PATH=\"\$HOME/.local/bin:\$PATH\"\n\n"
    fi
}

# ── Uninstall ────────────────────────────────────────────────────────────────
uninstall() {
    banner "Uninstall"
    ensure_path

    if command -v uv &>/dev/null && uv tool list 2>/dev/null | grep -q "^${TOOL_NAME}"; then
        uv tool uninstall "$TOOL_NAME" >/dev/null 2>&1 && ok "Removed the whisperlocal command"
    else
        warn "whisperlocal was not installed via uv — nothing to remove"
    fi

    local config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/whisperlocal"
    local cache_dir="$HOME/Library/Caches/WhisperLocal"

    if [[ -d "$config_dir" ]]; then
        rm -rf "$config_dir" && ok "Removed your settings ($config_dir)"
    fi
    if [[ -d "$cache_dir" ]]; then
        rm -rf "$cache_dir" && ok "Removed cached audio ($cache_dir)"
    fi

    # Model weights are shared with any other Hugging Face tool you use, so we
    # only ever remove the specific Whisper models WhisperLocal downloaded, and
    # only when asked.
    local hub="$HOME/.cache/huggingface/hub"
    local models=()
    if [[ -d "$hub" ]]; then
        while IFS= read -r dir; do
            [[ -n "$dir" ]] && models+=("$dir")
        done < <(find "$hub" -maxdepth 1 -type d -name 'models--mlx-community--whisper-*' 2>/dev/null)
    fi

    if (( ${#models[@]} > 0 )); then
        printf "\n%sDownloaded Whisper models still on disk:%s\n" "$BOLD" "$RESET"
        for m in "${models[@]}"; do
            printf "   %s  %s\n" "$(du -sh "$m" 2>/dev/null | cut -f1)" "$(basename "$m")"
        done

        if [[ "${REMOVE_MODELS:-}" == "1" ]]; then
            rm -rf "${models[@]}" && ok "Removed the model weights"
        else
            printf "\n%sKept.%s Other Hugging Face tools may share this cache.\n" "$DIM" "$RESET"
            printf "To delete them too:  REMOVE_MODELS=1 bash install.sh --uninstall\n"
        fi
    fi

    # uv itself is left alone on purpose — you may well use it for other things.
    printf "\n%sDone.%s WhisperLocal is gone. (uv and ffmpeg were left installed.)\n\n" "$GREEN" "$RESET"
}

main() {
    case "${1:-}" in
        --uninstall|-u|uninstall)
            uninstall
            ;;
        --help|-h)
            cat <<'EOF'
WhisperLocal installer

  bash install.sh              install WhisperLocal (and uv/ffmpeg if missing)
  bash install.sh --uninstall  remove WhisperLocal, its settings and cache
  bash install.sh --help       show this message

Environment:
  REMOVE_MODELS=1              with --uninstall, also delete downloaded
                               Whisper model weights

Docs: https://github.com/shivamdixit17/whisperlocal
EOF
            ;;
        "")
            banner "Install"
            check_platform
            ensure_path
            ensure_uv
            ensure_ffmpeg
            install_tool
            post_install
            ;;
        *)
            die "Unknown option: $1  (try --help)"
            ;;
    esac
}

main "$@"
