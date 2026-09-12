"""keychain: every `security` call is intercepted, nothing touches the real keychain."""

from __future__ import annotations

import subprocess

import pytest

from whisperlocal import keychain


class FakeRun:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, args, *, input=None):
        self.calls.append((list(args), input))
        return subprocess.CompletedProcess(args, self.returncode, self.stdout, self.stderr)


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    monkeypatch.delenv(keychain.ENV_VAR, raising=False)
    monkeypatch.setattr(keychain, "_warned", False)


def test_env_var_wins(monkeypatch):
    fake = FakeRun(0, "from-keychain\n")
    monkeypatch.setattr(keychain, "_run", fake)
    monkeypatch.setenv(keychain.ENV_VAR, "  from-env  ")
    assert keychain.get_api_key() == "from-env"
    assert fake.calls == []
    assert keychain.has_api_key()


def test_reads_from_keychain(monkeypatch):
    fake = FakeRun(0, "sk-abc\n")
    monkeypatch.setattr(keychain, "_run", fake)
    assert keychain.get_api_key() == "sk-abc"
    args, stdin = fake.calls[0]
    assert args[0] == keychain.SECURITY
    assert "find-generic-password" in args
    assert "-w" in args
    assert stdin is None


def test_not_found_is_none(monkeypatch, capsys):
    monkeypatch.setattr(keychain, "_run", FakeRun(keychain.NOT_FOUND, "", "not found"))
    assert keychain.get_api_key() is None
    assert not keychain.has_api_key()
    assert "Warning" not in capsys.readouterr().out


def test_other_failure_is_none_with_one_warning(monkeypatch, capsys):
    monkeypatch.setattr(keychain, "_run", FakeRun(36, "", "locked"))
    assert keychain.get_api_key() is None
    assert keychain.get_api_key() is None
    assert capsys.readouterr().out.count("Warning") == 1


def test_set_sends_key_on_stdin_not_argv(monkeypatch):
    fake = FakeRun(0)
    monkeypatch.setattr(keychain, "_run", fake)
    keychain.set_api_key("  sk-secret-123 ")
    args, stdin = fake.calls[0]
    assert args == [keychain.SECURITY, "-i"]
    assert "sk-secret-123" not in " ".join(args)
    assert stdin is not None and stdin.endswith("\n")
    assert "add-generic-password" in stdin
    assert f'-a "{keychain.ACCOUNT}"' in stdin
    assert f'-s "{keychain.SERVICE}"' in stdin
    assert "-U" in stdin
    assert '-w "sk-secret-123"' in stdin


def test_set_rejects_empty_and_failures(monkeypatch):
    fake = FakeRun(0)
    monkeypatch.setattr(keychain, "_run", fake)
    with pytest.raises(keychain.KeychainError):
        keychain.set_api_key("   ")
    assert fake.calls == []

    monkeypatch.setattr(keychain, "_run", FakeRun(1, "", "boom"))
    with pytest.raises(keychain.KeychainError, match="boom"):
        keychain.set_api_key("sk-x")


def test_clear_tolerates_missing(monkeypatch):
    fake = FakeRun(keychain.NOT_FOUND)
    monkeypatch.setattr(keychain, "_run", fake)
    keychain.clear_api_key()
    args, _ = fake.calls[0]
    assert "delete-generic-password" in args

    monkeypatch.setattr(keychain, "_run", FakeRun(0))
    keychain.clear_api_key()

    monkeypatch.setattr(keychain, "_run", FakeRun(1, "", "nope"))
    with pytest.raises(keychain.KeychainError):
        keychain.clear_api_key()
