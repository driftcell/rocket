"""Offline tests for ModrinthProvider."""

from __future__ import annotations

from pathlib import Path

from rocket.cli import main

from .helpers import (
    FakeAPI,
    add_paper_routes,
    base_config,
    make_plugin_jar_bytes,
    modrinth_file_payload,
    modrinth_project_payload,
    modrinth_project_url,
    modrinth_version_payload,
    modrinth_versions_url,
    sha512_hex,
    write_config,
)

SLUG = "coreprotect"
CDN_URL = "https://cdn.modrinth.com/data/abc123/versions/xyz987/CoreProtect-CE-24.1.jar"

PLUGIN = f"""
[plugins.coreprotect]
provider = "modrinth"
project = "{SLUG}"
version = "24.1"
asset = "CoreProtect-CE-24.1.jar"
"""


def run_main(tmp_path: Path, api: FakeAPI, text: str) -> int:
    config_path = write_config(tmp_path, text)
    return main(
        ["download", "--config", str(config_path)],
        transport=api.transport(),
        sleep=lambda _: None,
    )


def plugin_config(plugin_sections: str) -> str:
    """Modrinth tests build every config against the real target version 1.21.11."""
    return base_config(plugin_sections, paper_version="1.21.11")


def add_modrinth_routes(
    api: FakeAPI,
    jar: bytes,
    *,
    versions: list[dict[str, object]],
    cdn_url: str = CDN_URL,
) -> None:
    api.add_json(modrinth_project_url(SLUG), modrinth_project_payload(slug=SLUG))
    api.add_json(modrinth_versions_url(SLUG), versions)
    api.add_bytes(cdn_url, jar)


def coreprotect_version(
    jar: bytes,
    *,
    version_number: str = "24.1",
    games: list[str] | None = None,
    loaders: list[str] | None = None,
    files: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    if files is None:
        files = [
            modrinth_file_payload(
                filename="CoreProtect-CE-24.1.jar",
                url=CDN_URL,
                size=len(jar),
                sha512=sha512_hex(jar),
                sha1="a" * 40,
            )
        ]
    return modrinth_version_payload(
        version_number=version_number,
        game_versions=games if games is not None else ["1.21.10", "1.21.11", "26.1"],
        loaders=loaders if loaders is not None else ["bukkit", "paper", "spigot"],
        files=files,
    )


def test_modrinth_download_success_and_skip(tmp_path: Path, capsys) -> None:
    jar = make_plugin_jar_bytes(name="CoreProtect")
    api = FakeAPI()
    add_paper_routes(api, version="1.21.11")
    add_modrinth_routes(api, jar, versions=[coreprotect_version(jar)])
    text = plugin_config(PLUGIN)

    assert run_main(tmp_path, api, text) == 0
    target = tmp_path / "artifacts" / "plugins" / "coreprotect.jar"
    assert target.read_bytes() == jar
    out = capsys.readouterr().out
    assert " + coreprotect.jar (24.1, modrinth)" in out
    assert "Downloaded 2 items" in out

    assert run_main(tmp_path, api, text) == 0
    out = capsys.readouterr().out
    assert "Audited 2 items" in out
    assert api.count(CDN_URL) == 1


def test_modrinth_exact_version_required(tmp_path: Path, capsys) -> None:
    jar = make_plugin_jar_bytes()
    api = FakeAPI()
    add_paper_routes(api, version="1.21.11")
    add_modrinth_routes(api, jar, versions=[coreprotect_version(jar, version_number="24.0")])
    plugin = PLUGIN.replace('version = "24.1"', 'version = "24.2"')

    assert run_main(tmp_path, api, plugin_config(plugin)) == 1
    err = capsys.readouterr().err
    assert "does not exist" in err
    assert api.count(CDN_URL) == 0


def test_modrinth_incompatible_game_version(tmp_path: Path, capsys) -> None:
    jar = make_plugin_jar_bytes()
    api = FakeAPI()
    add_paper_routes(api, version="1.21.11")
    add_modrinth_routes(api, jar, versions=[coreprotect_version(jar, games=["1.20.4", "1.20.6"])])

    assert run_main(tmp_path, api, plugin_config(PLUGIN)) == 1
    assert "does not declare support for Minecraft" in capsys.readouterr().err


def test_modrinth_loader_mismatch(tmp_path: Path, capsys) -> None:
    jar = make_plugin_jar_bytes()
    api = FakeAPI()
    add_paper_routes(api, version="1.21.11")
    add_modrinth_routes(api, jar, versions=[coreprotect_version(jar, loaders=["fabric", "forge"])])

    assert run_main(tmp_path, api, plugin_config(PLUGIN)) == 1
    assert "is not usable with PAPER" in capsys.readouterr().err


def test_modrinth_multiple_jars_require_asset(tmp_path: Path, capsys) -> None:
    jar = make_plugin_jar_bytes()
    other = make_plugin_jar_bytes(name="Other")
    files = [
        modrinth_file_payload(
            filename="A-1.0.jar",
            url="https://cdn.modrinth.com/data/abc/a.jar",
            size=len(jar),
            sha512=sha512_hex(jar),
        ),
        modrinth_file_payload(
            filename="B-1.0.jar",
            url="https://cdn.modrinth.com/data/abc/b.jar",
            size=len(other),
            sha512=sha512_hex(other),
        ),
    ]
    api = FakeAPI()
    add_paper_routes(api, version="1.21.11")
    add_modrinth_routes(api, jar, versions=[coreprotect_version(jar, files=files)])
    plugin = """
[plugins.coreprotect]
provider = "modrinth"
project = "coreprotect"
version = "24.1"
"""

    assert run_main(tmp_path, api, plugin_config(plugin)) == 1
    err = capsys.readouterr().err
    assert "multiple JAR files" in err
    assert "A-1.0.jar" in err
    assert "B-1.0.jar" in err


def test_modrinth_asset_exact_match(tmp_path: Path) -> None:
    jar = make_plugin_jar_bytes(name="Picked")
    other = make_plugin_jar_bytes(name="NotPicked")
    picked_url = "https://cdn.modrinth.com/data/abc/versions/xyz/B-24.1.jar"
    files = [
        modrinth_file_payload(
            filename="A-24.1.jar",
            url="https://cdn.modrinth.com/data/abc/versions/xyz/A-24.1.jar",
            size=len(other),
            sha512=sha512_hex(other),
        ),
        modrinth_file_payload(
            filename="B-24.1.jar", url=picked_url, size=len(jar), sha512=sha512_hex(jar)
        ),
    ]
    api = FakeAPI()
    add_paper_routes(api, version="1.21.11")
    add_modrinth_routes(
        api, jar, versions=[coreprotect_version(jar, files=files)], cdn_url=picked_url
    )
    plugin = PLUGIN.replace('asset = "CoreProtect-CE-24.1.jar"', 'asset = "B-24.1.jar"')

    assert run_main(tmp_path, api, plugin_config(plugin)) == 0
    assert (tmp_path / "artifacts" / "plugins" / "coreprotect.jar").read_bytes() == jar


def test_modrinth_asset_not_found(tmp_path: Path, capsys) -> None:
    jar = make_plugin_jar_bytes()
    api = FakeAPI()
    add_paper_routes(api, version="1.21.11")
    add_modrinth_routes(api, jar, versions=[coreprotect_version(jar)])
    plugin = PLUGIN.replace("CoreProtect-CE-24.1.jar", "Missing-24.1.jar")

    assert run_main(tmp_path, api, plugin_config(plugin)) == 1
    err = capsys.readouterr().err
    assert "not found in Modrinth version" in err
    assert "CoreProtect-CE-24.1.jar" in err


def test_modrinth_ambiguous_same_version_number(tmp_path: Path, capsys) -> None:
    jar = make_plugin_jar_bytes()
    api = FakeAPI()
    add_paper_routes(api, version="1.21.11")
    add_modrinth_routes(
        api,
        jar,
        versions=[
            coreprotect_version(jar, loaders=["paper"]),
            coreprotect_version(jar, loaders=["bukkit"]),
        ],
    )

    assert run_main(tmp_path, api, plugin_config(PLUGIN)) == 1
    assert "cannot decide" in capsys.readouterr().err


def test_modrinth_picks_compatible_entry(tmp_path: Path) -> None:
    jar = make_plugin_jar_bytes()
    wrong_url = "https://cdn.modrinth.com/data/abc/versions/zzz/Wrong-26.jar"
    incompatible = modrinth_version_payload(
        version_number="24.1",
        game_versions=["26.1"],
        loaders=["paper"],
        files=[
            modrinth_file_payload(filename="Wrong-26.jar", url=wrong_url, size=1, sha512="0" * 128)
        ],
    )
    api = FakeAPI()
    add_paper_routes(api, version="1.21.11")
    add_modrinth_routes(api, jar, versions=[incompatible, coreprotect_version(jar)])

    assert run_main(tmp_path, api, plugin_config(PLUGIN)) == 0
    assert api.count(wrong_url) == 0
    assert (tmp_path / "artifacts" / "plugins" / "coreprotect.jar").read_bytes() == jar


def test_modrinth_project_not_found(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    add_paper_routes(api, version="1.21.11")
    api.add_json(modrinth_project_url(SLUG), {"error": "not_found"}, status=404)

    assert run_main(tmp_path, api, plugin_config(PLUGIN)) == 1
    assert "Modrinth project not found" in capsys.readouterr().err


def test_modrinth_shared_cache_between_entries(tmp_path: Path) -> None:
    jar = make_plugin_jar_bytes()
    api = FakeAPI()
    add_paper_routes(api, version="1.21.11")
    add_modrinth_routes(api, jar, versions=[coreprotect_version(jar)])
    second = f"""
[plugins.coreprotect_backup]
provider = "modrinth"
project = "{SLUG}"
version = "24.1"
asset = "CoreProtect-CE-24.1.jar"
"""

    assert run_main(tmp_path, api, plugin_config(PLUGIN + second)) == 0
    assert api.count(modrinth_project_url(SLUG)) == 1
    assert api.count(modrinth_versions_url(SLUG)) == 1
    assert api.count(CDN_URL) == 2
    plugins_dir = tmp_path / "artifacts" / "plugins"
    assert (plugins_dir / "coreprotect.jar").read_bytes() == jar
    assert (plugins_dir / "coreprotect_backup.jar").read_bytes() == jar
