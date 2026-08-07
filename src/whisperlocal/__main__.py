"""Allow `python -m whisperlocal` alongside the `whisperlocal` command."""

from whisperlocal.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
