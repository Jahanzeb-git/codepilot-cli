"""
cli/__main__.py  –  Entry point for `codepilot` command and `python -m cli`
"""

from __future__ import annotations

import sys


def main() -> None:
    """Entry-point registered in pyproject.toml."""
    try:
        from .app import run_cli
        run_cli()
    except KeyboardInterrupt:
        print("\n   Goodbye.")
        sys.exit(0)


if __name__ == "__main__":
    main()