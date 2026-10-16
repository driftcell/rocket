"""Surgical edits to server.toml: append or remove one [plugins.<id>] section.

Edits are textual so hand-written formatting and comments elsewhere in the file survive;
tomli-w renders the appended section. The result is re-parsed and compared against the
original before it is written: a failed check refuses the edit instead of corrupting the
config.
"""

from __future__ import annotations

import os
import re
import tempfile
import tomllib
from pathlib import Path
from typing import Any

import tomli_w

from .errors import RocketError

_ANY_HEADER_RE = re.compile(r"^\s*\[")


def add_plugin_section(
    path: Path,
    *,
    plugin_id: str,
    provider: str,
    project: str,
    version: str,
    asset: str | None,
) -> None:
    """Append a [plugins.<id>] section; every other byte of the file is preserved."""
    raw_text = _read(path)
    before = _parse(raw_text, path)
    _plugins_before(before, path)

    entry: dict[str, object] = {
        "provider": provider,
        "project": project,
        "version": version,
    }
    if asset is not None:
        entry["asset"] = asset
    section = tomli_w.dumps({"plugins": {plugin_id: entry}})

    new_text = raw_text
    if new_text and not new_text.endswith("\n"):
        new_text += "\n"
    if new_text.strip():
        new_text += "\n"
    new_text += section

    after = _parse(new_text, path)
    plugins_after = after.get("plugins", {})
    added = plugins_after.get(plugin_id) if isinstance(plugins_after, dict) else None
    if added != entry:
        raise RocketError(
            f"internal error: the rewritten {path} did not round-trip the new entry; "
            "the file was left unchanged",
        )
    _write(path, new_text)


def remove_plugin_section(path: Path, plugin_id: str) -> None:
    """Delete the [plugins.<id>] section; every other plugin entry must survive."""
    raw_text = _read(path)
    before = _parse(raw_text, path)
    plugins_before = _plugins_before(before, path)

    header = re.compile(
        r"^\s*\[\s*plugins\s*\.\s*(?:"
        + re.escape(plugin_id)
        + r'|"'
        + re.escape(plugin_id)
        + r'")\s*\]\s*(?:#.*)?$'
    )
    lines = raw_text.split("\n")
    start = next((index for index, line in enumerate(lines) if header.match(line)), None)
    if start is None:
        raise RocketError(
            f"cannot locate the [plugins.{plugin_id}] section header in {path}",
            hint="the entry may be declared with dotted keys under [plugins]; remove it by hand",
        )
    end = start + 1
    while end < len(lines) and not _ANY_HEADER_RE.match(lines[end]):
        end += 1
    del lines[start:end]
    new_text = "\n".join(lines).rstrip("\n") + "\n"

    after = _parse(new_text, path)
    plugins_after = after.get("plugins", {})
    if not isinstance(plugins_after, dict):
        raise _refused(path)
    expected = {key: value for key, value in plugins_before.items() if key != plugin_id}
    if plugins_after != expected:
        raise _refused(path)
    _write(path, new_text)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RocketError(
            f"cannot read config file {path}: {exc}",
            hint="point --config at an existing server.toml",
        ) from exc


def _write(path: Path, text: str) -> None:
    temp_path: Path | None = None
    try:
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        os.close(descriptor)
        temp_path = Path(temp_name)
        temp_path.write_text(text, encoding="utf-8")
        os.replace(temp_path, path)
    except OSError as exc:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise RocketError(
            f"cannot write config file {path}: {exc}",
            hint="check the config directory write permissions and free space",
        ) from exc


def _parse(text: str, path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise RocketError(f"{path} is not valid TOML ({exc}); the file was left unchanged") from exc


def _plugins_before(raw: dict[str, Any], path: Path) -> dict[str, Any]:
    plugins = raw.get("plugins", {})
    if not isinstance(plugins, dict):
        raise RocketError(
            f"[plugins] in {path} is not a table; edit the file by hand",
        )
    return plugins


def _refused(path: Path) -> RocketError:
    return RocketError(
        f"the edit would change more than the [plugins] entry being removed; "
        f"{path} was left unchanged",
        hint="remove the section by hand",
    )
