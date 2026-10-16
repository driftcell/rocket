"""Offline tests for rocket.lock reading, validation, serialization and writing."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from rocket.errors import LockError
from rocket.lockfile import (
    LOCK_VERSION,
    LockData,
    LockedEntry,
    load_lock,
    serialize_lock,
    write_lock,
)
from rocket.models import ArtifactKind, Checksum

SHA256 = "ab" * 32
SHA512 = "cd" * 64


def paper_entry(**overrides: object) -> LockedEntry:
    entry = LockedEntry(
        kind=ArtifactKind.PAPER,
        provider="fill",
        project="paper",
        version="26.1.2",
        url="https://fill-data.papermc.io/v1/objects/ab/paper-26.1.2-42.jar",
        upstream_filename="paper-26.1.2-42.jar",
        size=1234,
        checksum=Checksum(algorithm="sha256", value=SHA256),
        detail="build 42",
        source_chain=("fill",),
        build=42,
    )
    return replace(entry, **overrides)


def plugin_entry(**overrides: object) -> LockedEntry:
    entry = LockedEntry(
        kind=ArtifactKind.PLUGIN,
        provider="hangar",
        project="Owner/Good",
        version="1.0.0",
        url="https://hangarcdn.papermc.io/plugins/Owner/Good/versions/1.0.0/PAPER/Good.jar",
        upstream_filename="Good-1.0.0.jar",
        size=99,
        checksum=Checksum(algorithm="sha512", value=SHA512),
        source_chain=("hangar",),
    )
    return replace(entry, **overrides)


def test_round_trip(tmp_path: Path) -> None:
    lock = LockData(paper=paper_entry(), plugins={"good": plugin_entry()})
    path = tmp_path / "rocket.lock"

    assert write_lock(path, lock) is True
    assert write_lock(path, lock) is False  # identical content: no rewrite

    loaded, warnings = load_lock(path)
    assert warnings == ()
    assert loaded == lock


def test_round_trip_minimal_plugin_entry(tmp_path: Path) -> None:
    entry = plugin_entry(
        upstream_filename=None, size=None, checksum=None, detail=None, source_chain=()
    )
    lock = LockData(plugins={"good": entry})
    path = tmp_path / "rocket.lock"

    write_lock(path, lock)

    loaded, warnings = load_lock(path)
    assert warnings == ()
    assert loaded == lock


def test_missing_lock(tmp_path: Path) -> None:
    loaded, warnings = load_lock(tmp_path / "rocket.lock")
    assert loaded is None
    assert warnings == ()


def test_invalid_toml_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "rocket.lock"
    path.write_text("version = = 1", encoding="utf-8")

    loaded, warnings = load_lock(path)

    assert loaded is None
    assert "not valid TOML" in warnings[0]


def test_unsupported_version_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "rocket.lock"
    path.write_text(f"version = {LOCK_VERSION + 1}\n", encoding="utf-8")

    loaded, warnings = load_lock(path)

    assert loaded is None
    assert "unsupported lock version" in warnings[0]


def test_invalid_paper_entry_is_dropped_but_plugins_kept(tmp_path: Path) -> None:
    path = tmp_path / "rocket.lock"
    write_lock(path, LockData(paper=paper_entry(), plugins={"good": plugin_entry()}))
    path.write_text(path.read_text(encoding="utf-8").replace("build = 42\n", ""), encoding="utf-8")

    loaded, warnings = load_lock(path)

    assert loaded is not None
    assert loaded.paper is None
    assert loaded.plugins == {"good": plugin_entry()}
    assert "[paper] lock entry is invalid" in warnings[0]


def test_invalid_plugin_entry_is_dropped(tmp_path: Path) -> None:
    path = tmp_path / "rocket.lock"
    path.write_text(
        f"version = {LOCK_VERSION}\n"
        "\n"
        "[plugins.good]\n"
        'provider = "hangar"\n'
        'project = "Owner/Good"\n'
        "version = 5\n"
        'url = "https://example.invalid/good.jar"\n',
        encoding="utf-8",
    )

    loaded, warnings = load_lock(path)

    assert loaded is not None
    assert loaded.plugins == {}
    assert "[plugins.good] lock entry is invalid" in warnings[0]


def test_write_failure_raises_lock_error(tmp_path: Path) -> None:
    path = tmp_path / "rocket.lock"
    path.mkdir()  # a directory cannot be replaced by the lock file

    try:
        write_lock(path, LockData(paper=paper_entry()))
    except LockError as exc:
        assert "cannot write lock file" in exc.message
    else:  # pragma: no cover
        raise AssertionError("write_lock should fail when the target is a directory")


def test_serialize_sorts_plugin_ids() -> None:
    lock = LockData(
        plugins={
            "zebra": plugin_entry(project="Owner/Zebra"),
            "alpha": plugin_entry(project="Owner/Alpha"),
        }
    )

    text = serialize_lock(lock)

    assert text.index("[plugins.alpha]") < text.index("[plugins.zebra]")
