"""
The API key for the cloud transcription backend, kept in the macOS Keychain.

The key is stored as a generic password on the login keychain under the
service "WhisperLocal". It never lands in the config file, and it never
appears on a command line either: `set_api_key` feeds the command to
`security -i` on stdin, so `ps` and the shell history only ever see
`/usr/bin/security -i`.

For development and CI the environment variable WHISPERLOCAL_API_KEY wins
over the keychain, so a test run never has to touch the real keychain.
"""

from __future__ import annotations

import os
import subprocess

SERVICE = "WhisperLocal"
ACCOUNT = "api_key"
ENV_VAR = "WHISPERLOCAL_API_KEY"
LABEL = "WhisperLocal API key"
SECURITY = "/usr/bin/security"

# `security` exits 44 (errSecItemNotFound) when there is no such item.
NOT_FOUND = 44


class KeychainError(RuntimeError):
    """The `security` tool failed to store or delete the key."""


def _run(args: list[str], *, input: str | None = None) -> subprocess.CompletedProcess:
    """Run `security`. Tests monkeypatch this so nothing touches the keychain."""
    return subprocess.run(
        args,
        input=input,
        capture_output=True,
        text=True,
        check=False,
    )


_warned = False


def _warn_once(message: str) -> None:
    global _warned
    if not _warned:
        _warned = True
        print(f"Warning: {message}")


def get_api_key() -> str | None:
    """The API key, or None if none is configured.

    The environment variable takes precedence over the keychain. A missing
    keychain item is simply None; any other failure (no `security` tool, a
    locked keychain the user declined to unlock) is None plus one warning.
    """
    env = os.environ.get(ENV_VAR)
    if env is not None and env.strip():
        return env.strip()

    try:
        proc = _run([SECURITY, "find-generic-password", "-s", SERVICE, "-a", ACCOUNT, "-w"])
    except (OSError, subprocess.SubprocessError) as exc:
        _warn_once(f"could not read the keychain: {exc}")
        return None

    if proc.returncode == 0:
        key = (proc.stdout or "").strip()
        return key or None
    if proc.returncode == NOT_FOUND:
        return None
    _warn_once(
        f"could not read the API key from the keychain "
        f"(security exited {proc.returncode}): {(proc.stderr or '').strip()}"
    )
    return None


def has_api_key() -> bool:
    return get_api_key() is not None


def set_api_key(key: str) -> None:
    """Store `key`, replacing any existing one.

    The command goes to `security -i` over stdin, so the key is never part of
    the argument list.
    """
    key = (key or "").strip()
    if not key:
        raise KeychainError("the API key must not be empty")
    if any(ch in key for ch in "\r\n\"\\"):
        raise KeychainError("the API key contains characters that cannot be stored")

    command = (
        f'add-generic-password -a "{ACCOUNT}" -s "{SERVICE}" -l "{LABEL}" -U -w "{key}"\n'
    )
    try:
        proc = _run([SECURITY, "-i"], input=command)
    except (OSError, subprocess.SubprocessError) as exc:
        raise KeychainError(f"could not run {SECURITY}: {exc}") from exc
    if proc.returncode != 0:
        raise KeychainError(
            f"security exited {proc.returncode}: {(proc.stderr or '').strip() or 'unknown error'}"
        )


def clear_api_key() -> None:
    """Remove the stored key. Nothing stored is not an error."""
    try:
        proc = _run([SECURITY, "delete-generic-password", "-s", SERVICE, "-a", ACCOUNT])
    except (OSError, subprocess.SubprocessError) as exc:
        raise KeychainError(f"could not run {SECURITY}: {exc}") from exc
    if proc.returncode in (0, NOT_FOUND):
        return
    raise KeychainError(
        f"security exited {proc.returncode}: {(proc.stderr or '').strip() or 'unknown error'}"
    )
