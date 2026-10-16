"""Surgical edits to server.toml: append or remove one [plugins.<id>] section.

Edits are textual so hand-written formatting and comments elsewhere in the file survive;
tomli-w renders the appended section. The result is re-parsed and compared against the
original before it is written: a failed check refuses the edit instead of corrupting the
config.
"""

from __future__ import annotations

import copy
import os
import re
import tempfile
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomli_w

from .errors import RocketError

_ANY_HEADER_RE = re.compile(r"^\s*\[")


@dataclass(frozen=True)
class PaperPin:
    """New [paper] values; ``build=None`` removes the build line (it belongs to one version)."""

    version: str
    build: int | None


@dataclass(frozen=True)
class PluginPin:
    """New [plugins.<id>] values; ``asset=None`` removes the asset line."""

    version: str
    asset: str | None


def update_version_pins(
    path: Path,
    *,
    paper: PaperPin | None = None,
    plugins: Mapping[str, PluginPin],
) -> None:
    """Rewrite version/asset/build fields in place; everything else is preserved byte for byte.

    ``paper=None`` leaves the [paper] section untouched; only the listed plugin ids are
    touched. The result is re-parsed and compared against the intended change before it is
    written, exactly like the add/remove edits.
    """
    raw_text = _read(path)
    before = _parse(raw_text, path)
    expected = _expected_pins(before, path, paper=paper, plugins=plugins)

    lines = raw_text.split("\n")
    if paper is not None:
        _edit_section(
            lines,
            _section_header_re("paper"),
            {"version": ('"', paper.version, '"'), "build": ("", paper.build, "")},
            path,
        )
    for plugin_id, pin in plugins.items():
        _edit_section(
            lines,
            _section_header_re(f"plugins.{plugin_id}"),
            {"version": ('"', pin.version, '"'), "asset": ('"', pin.asset, '"')},
            path,
        )
    new_text = "\n".join(lines)

    after = _parse(new_text, path)
    if after != expected:
        raise RocketError(
            f"the edit would change more than the intended version pins; {path} was left unchanged",
            hint="edit the fields by hand",
        )
    _write(path, new_text)


def _expected_pins(
    before: dict[str, Any],
    path: Path,
    *,
    paper: PaperPin | None,
    plugins: Mapping[str, PluginPin],
) -> dict[str, Any]:
    """The parsed config with exactly the intended pin changes applied."""
    expected = copy.deepcopy(before)
    if paper is not None:
        paper_table = expected.get("paper")
        if not isinstance(paper_table, dict):
            raise RocketError(f"[paper] in {path} is not a table; edit the file by hand")
        paper_table["version"] = paper.version
        if paper.build is None:
            paper_table.pop("build", None)
        else:
            paper_table["build"] = paper.build
    plugins_table = expected.get("plugins", {})
    if not isinstance(plugins_table, dict):
        raise RocketError(f"[plugins] in {path} is not a table; edit the file by hand")
    for plugin_id, pin in plugins.items():
        entry = plugins_table.get(plugin_id)
        if not isinstance(entry, dict):
            raise RocketError(
                f"cannot locate a [plugins.{plugin_id}] table in {path}",
                hint="the entry may be declared with inline dotted keys; edit it by hand",
            )
        entry["version"] = pin.version
        if pin.asset is None:
            entry.pop("asset", None)
        else:
            entry["asset"] = pin.asset
    return expected


def _section_header_re(section: str) -> re.Pattern[str]:
    return re.compile(r"^\s*\[\s*" + re.escape(section) + r"\s*\]\s*(?:#.*)?$")


def _edit_section(
    lines: list[str],
    header: re.Pattern[str],
    fields: dict[str, tuple[str, object, str]],
    path: Path,
) -> None:
    """Apply field changes inside the first section matching ``header`` (in place).

    Each field value is (prefix, value, suffix) for rendering: strings pass the quotes,
    integers pass empty strings and None removes the line.
    """
    start = next((index for index, line in enumerate(lines) if header.match(line)), None)
    if start is None:
        raise RocketError(
            f"cannot locate the section header in {path}",
            hint="entries declared with inline dotted keys must be edited by hand",
        )
    end = start + 1
    while end < len(lines) and not _ANY_HEADER_RE.match(lines[end]):
        end += 1
    for key, (prefix, value, suffix) in fields.items():
        assignment = re.compile(
            r"^(\s*)" + re.escape(key) + r"(\s*=\s*)(?:\"[^\"]*\"|[^\s#]+)(\s*(?:#.*)?)$"
        )
        found: int | None = None
        found_match: re.Match[str] | None = None
        for index in range(start + 1, end):
            candidate = assignment.match(lines[index])
            if candidate is not None:
                found, found_match = index, candidate
                break
        if value is None:
            if found is not None:
                del lines[found]
                end -= 1
        elif found is not None and found_match is not None:
            lines[found] = (
                f"{found_match.group(1)}{key}{found_match.group(2)}"
                f"{prefix}{value}{suffix}{found_match.group(3)}"
            )
        else:
            # insert right after the version line (canonical order) or the header
            anchor = next(
                (
                    index
                    for index in range(start + 1, end)
                    if lines[index].lstrip().startswith("version")
                ),
                start,
            )
            lines.insert(anchor + 1, f"{key} = {prefix}{value}{suffix}")
            end += 1


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
