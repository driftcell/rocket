"""Offline tests for the Paper / Fill API v3."""

from __future__ import annotations

from pathlib import Path

from rocket.cli import main

from .helpers import (
    FILL_PROJECTS_URL,
    FakeAPI,
    fill_build_payload,
    fill_builds_url,
    fill_project_payload,
    make_paper_jar_bytes,
    sha256_hex,
    write_config,
)

VERSION = "26.1.2"
BUILDS_URL = fill_builds_url(VERSION)
URL_41 = "https://fill-data.papermc.io/v1/objects/aaa/paper-26.1.2-41.jar"
URL_42 = "https://fill-data.papermc.io/v1/objects/bbb/paper-26.1.2-42.jar"
URL_99 = "https://fill-data.papermc.io/v1/objects/ccc/paper-26.1.2-99.jar"


def config_text(*, version: str = VERSION, build: int | None = None) -> str:
    build_line = f"build = {build}\n" if build is not None else ""
    return f"""
schema_version = 1

[paper]
version = "{version}"
{build_line}

[http]
user_agent = "rocket-test/0.1 (test@example.invalid)"
"""


def run_main(tmp_path: Path, api: FakeAPI, text: str) -> int:
    config_path = write_config(tmp_path, text)
    return main(
        ["download", "--config", str(config_path)], transport=api.transport(), sleep=lambda _: None
    )


def make_api(
    *, builds: list[dict[str, object]], groups: dict[str, list[str]] | None = None
) -> FakeAPI:
    api = FakeAPI()
    api.add_json(FILL_PROJECTS_URL, fill_project_payload(groups or {"26.1": ["26.1.1", "26.1.2"]}))
    api.add_json(BUILDS_URL, builds)
    return api


def stable_builds(jar: bytes) -> list[dict[str, object]]:
    sha = sha256_hex(jar)
    return [
        fill_build_payload(
            build_id=41,
            channel="STABLE",
            url=URL_41,
            name="paper-26.1.2-41.jar",
            sha256=sha,
            size=len(jar),
        ),
        fill_build_payload(
            build_id=42,
            channel="STABLE",
            url=URL_42,
            name="paper-26.1.2-42.jar",
            sha256=sha,
            size=len(jar),
        ),
    ]


def test_paper_only_success(tmp_path: Path, capsys) -> None:
    jar = make_paper_jar_bytes()
    api = make_api(builds=stable_builds(jar))
    api.add_bytes(URL_42, jar)

    code = run_main(tmp_path, api, config_text())

    assert code == 0
    target = tmp_path / "artifacts" / "paper.jar"
    assert target.read_bytes() == jar
    assert api.count(URL_42) == 1
    assert api.count(URL_41) == 0
    out = capsys.readouterr().out
    assert " + paper.jar (26.1.2 build 42, fill)" in out
    assert "Downloaded 1 item" in out


def test_version_group_rejected(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    api.add_json(FILL_PROJECTS_URL, fill_project_payload({"26.1": ["26.1.1", "26.1.2"]}))

    code = run_main(tmp_path, api, config_text(version="26.1"))

    assert code == 1
    err = capsys.readouterr().err
    assert "version group" in err
    assert api.count(BUILDS_URL) == 0
    assert not (tmp_path / "artifacts" / "paper.jar").exists()


def test_version_not_found(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    api.add_json(FILL_PROJECTS_URL, fill_project_payload({"26.1": ["26.1.1", "26.1.2"]}))

    code = run_main(tmp_path, api, config_text(version="99.9"))

    assert code == 1
    assert "does not exist" in capsys.readouterr().err


def test_stable_max_build_selected(tmp_path: Path) -> None:
    jar = make_paper_jar_bytes()
    sha = sha256_hex(jar)
    experimental = fill_build_payload(
        build_id=99,
        channel="EXPERIMENTAL",
        url=URL_99,
        name="paper-26.1.2-99.jar",
        sha256=sha,
        size=len(jar),
    )
    api = make_api(builds=[*stable_builds(jar), experimental])
    api.add_bytes(URL_42, jar)

    code = run_main(tmp_path, api, config_text())

    assert code == 0
    assert api.count(URL_42) == 1
    assert api.count(URL_41) == 0
    assert api.count(URL_99) == 0


def test_specified_stable_build(tmp_path: Path) -> None:
    jar = make_paper_jar_bytes()
    api = make_api(builds=stable_builds(jar))
    api.add_bytes(URL_41, jar)

    code = run_main(tmp_path, api, config_text(build=41))

    assert code == 0
    assert api.count(URL_41) == 1
    assert api.count(URL_42) == 0
    assert (tmp_path / "artifacts" / "paper.jar").read_bytes() == jar


def test_specified_experimental_build_rejected(tmp_path: Path, capsys) -> None:
    jar = make_paper_jar_bytes()
    sha = sha256_hex(jar)
    experimental = fill_build_payload(
        build_id=99,
        channel="EXPERIMENTAL",
        url=URL_99,
        name="paper-26.1.2-99.jar",
        sha256=sha,
        size=len(jar),
    )
    api = make_api(builds=[*stable_builds(jar), experimental])

    code = run_main(tmp_path, api, config_text(build=99))

    assert code == 1
    assert "not on the STABLE channel" in capsys.readouterr().err
    assert api.count(URL_99) == 0
    assert api.count(URL_41) == 0


def test_specified_missing_build_rejected(tmp_path: Path, capsys) -> None:
    jar = make_paper_jar_bytes()
    api = make_api(builds=stable_builds(jar))

    code = run_main(tmp_path, api, config_text(build=7))

    assert code == 1
    assert "no stable build 7" in capsys.readouterr().err
    assert api.count(URL_41) == 0
    assert api.count(URL_42) == 0


def test_no_stable_builds_rejected(tmp_path: Path, capsys) -> None:
    jar = make_paper_jar_bytes()
    sha = sha256_hex(jar)
    experimental = fill_build_payload(
        build_id=5,
        channel="EXPERIMENTAL",
        url=URL_99,
        name="paper-26.1.2-5.jar",
        sha256=sha,
        size=len(jar),
    )
    api = make_api(builds=[experimental])

    code = run_main(tmp_path, api, config_text())

    assert code == 1
    assert "no STABLE builds" in capsys.readouterr().err


def test_missing_server_default_rejected(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    api.add_json(FILL_PROJECTS_URL, fill_project_payload({"26.1": ["26.1.1", "26.1.2"]}))
    api.add_json(BUILDS_URL, [{"id": 42, "channel": "STABLE", "downloads": {}}])

    code = run_main(tmp_path, api, config_text())

    assert code == 1
    assert "server:default" in capsys.readouterr().err


def test_missing_sha256_rejected(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    api.add_json(FILL_PROJECTS_URL, fill_project_payload({"26.1": ["26.1.1", "26.1.2"]}))
    api.add_json(
        BUILDS_URL,
        [
            {
                "id": 42,
                "channel": "STABLE",
                "downloads": {"server:default": {"name": "paper.jar", "url": URL_42}},
            }
        ],
    )

    code = run_main(tmp_path, api, config_text())

    assert code == 1
    assert "SHA-256" in capsys.readouterr().err


def test_fill_error_object_rejected(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    api.add_json(FILL_PROJECTS_URL, fill_project_payload({"26.1": ["26.1.1", "26.1.2"]}))
    api.add_json(
        BUILDS_URL,
        {"ok": False, "error": "version_not_found", "message": "No version was found."},
    )

    code = run_main(tmp_path, api, config_text())

    assert code == 1
    assert "Fill API returned an error" in capsys.readouterr().err


def test_second_run_skips_and_keeps_single_download(tmp_path: Path, capsys) -> None:
    jar = make_paper_jar_bytes()
    api = make_api(builds=stable_builds(jar))
    api.add_bytes(URL_42, jar)
    text = config_text()

    assert run_main(tmp_path, api, text) == 0
    capsys.readouterr()

    assert run_main(tmp_path, api, text) == 0
    out = capsys.readouterr().out
    assert "Audited 1 item" in out
    assert api.count(URL_42) == 1
    assert (tmp_path / "artifacts" / "paper.jar").read_bytes() == jar
