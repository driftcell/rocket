"""Shared console helpers: terminal styling, error printing and the rocket log handler."""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, TextIO

if TYPE_CHECKING:
    from blessed import Terminal


def terminal(stream: TextIO | None = None) -> Terminal:
    """A blessed Terminal bound to the stream; styles degrade to plain text off a TTY."""
    from blessed import Terminal  # lazy import, like questionary: plain runs stay fast

    return Terminal(stream=stream if stream is not None else sys.stdout)


def print_error(message: str, *, hint: str | None = None) -> None:
    """Print a one-line ``error:`` message (and optional dimmed hint) to stderr."""
    term = terminal(sys.stderr)
    text = f"{term.bold_red('error:')} {message}"
    if hint:
        text += " " + term.dim(f"(hint: {hint})")
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


def stdin_is_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):  # pragma: no cover - closed or unusual streams
        return False
