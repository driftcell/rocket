"""Shared console helpers: ANSI styling, error printing and the rocket log handler."""

from __future__ import annotations

import logging
import os
import sys
from typing import TextIO


def style(text: str, code: str, *, stream: TextIO) -> str:
    """Wrap text in an ANSI SGR code when the stream supports color."""
    if not _use_color(stream):
        return text
    return f"\x1b[{code}m{text}\x1b[0m"


def print_error(message: str, *, hint: str | None = None) -> None:
    """Print a one-line ``error:`` message (and optional dimmed hint) to stderr."""
    text = f"{style('error:', '1;31', stream=sys.stderr)} {message}"
    if hint:
        text += " " + style(f"(hint: {hint})", "2", stream=sys.stderr)
    print(text, file=sys.stderr)


def install_logging(level: int = logging.WARNING) -> logging.Handler:
    """Attach a stderr handler to the rocket logger for this run; removed afterwards."""
    logger = logging.getLogger("rocket")
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(level)
    return handler


def remove_logging(handler: logging.Handler) -> None:
    logging.getLogger("rocket").removeHandler(handler)
    handler.close()


def _use_color(stream: TextIO) -> bool:
    if os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return False
    try:
        return stream.isatty()
    except (AttributeError, ValueError):  # pragma: no cover - closed or unusual streams
        return False
