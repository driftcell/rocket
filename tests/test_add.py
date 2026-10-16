"""Offline tests for rocket add/remove: plugin entries in server.toml without hand-editing."""

from __future__ import annotations

from pathlib import Path

import pytest

from rocket.cli import main
from rocket.config import load_config

from .helpers import (
    FakeAPI,
    add_paper_routes,
    base_config,
    github_asset_payload,
    github_release_api_url,
    github_release_payload,
    github_releases_url,
    hangar_external_paper,
    hangar_native_paper,
    hangar_project_payload,
    hangar_project_url,
    hangar_version_list_item,
    hangar_version_list_payload,
    hangar_version_payload,
    hangar_version_url,
    hangar_versions_url,
    make_plugin_jar_bytes,
    modrinth_file_payload,
    modrinth_project_payload,
    modrinth_project_url,
    modrinth_version_payload,
    modrinth_versions_url,
    sha256_hex,
    sha512_hex,
    spiget_resource_payload,
    spiget_resource_url,
    spiget_version_payload,
    spiget_versions_url,
    write_config,
)

GOOD_SECTION = """
[plugins.good]
provider = "hangar"
project = "Owner/Good"
version = "1.0.0"
"""

GOOD_HANGAR_DOWNLOAD = (
    "https://hangarcdn.papermc.io/plugins/Owner/Good/versions/1.5.0/PAPER/Good-1.5.0.jar"
)


def run_add(api: FakeAPI, tmp_path: Path, args: list[str], config_text: str) -> int:
    config_path = write_config(tmp_path, config_text)
    return main(
        ["add", *args, "--config", str(config_path)],
        transport=api.transport(),
        sleep=lambda _: None,
    )


def run_remove(tmp_path: Path, plugin_id: str, config_text: str) -> int:
    config_path = write_config(tmp_path, config_text)
    return main(["remove", plugin_id, "--config", str(config_path)], sleep=lambda _: None)


def add_hangar_auto_routes(api: FakeAPI, jar: bytes, *, mc: str = "26.1.2") -> None:
    api.add_json(
        hangar_versions_url("Good", platform_version=mc),
        hangar_version_list_payload([hangar_version_list_item(name="1.5.0")]),
    )
    api.add_json(
        hangar_version_url("Good", "1.5.0"),
        hangar_version_payload(
            name="1.5.0",
            downloads=hangar_native_paper(
                download_url=GOOD_HANGAR_DOWNLOAD,
                name="Good-1.5.0.jar",
                sha256=sha256_hex(jar),
                size=len(jar),
            ),
            platform_dependencies={"PAPER": [mc]},
        ),
    )


def add_modrinth_auto_routes(api: FakeAPI, *, mc: str = "26.1.2") -> None:
    jar = make_plugin_jar_bytes(name="Good")
    api.add_json(
        modrinth_versions_url("good"),
        [
            modrinth_version_payload(
                version_number="3.0",
                game_versions=[mc],
                loaders=["paper"],
                files=[
                    modrinth_file_payload(
                        filename="Good-3.0.jar",
                        url="https://cdn.modrinth.com/data/abc/versions/x/Good-3.0.jar",
                        size=len(jar),
                        sha512=sha512_hex(jar),
                    )
                ],
            ),
            modrinth_version_payload(
                version_number="2.0",
                game_versions=[mc],
                loaders=["paper"],
                files=[
                    modrinth_file_payload(
                        filename="Good-2.0.jar",
                        url="https://cdn.modrinth.com/data/abc/versions/y/Good-2.0.jar",
                        size=len(jar),
                        sha512=sha512_hex(jar),
                    )
                ],
            ),
        ],
    )


def test_add_modrinth_auto_pins_newest_compatible(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    add_modrinth_auto_routes(api)
    config_path = tmp_path / "server.toml"

    assert run_add(api, tmp_path, ["modrinth:good"], base_config()) == 0

    text = config_path.read_text(encoding="utf-8")
    assert '[plugins.good]\nprovider = "modrinth"\nproject = "good"\n' in text
    assert 'version = "3.0"' in text
    assert 'asset = "Good-3.0.jar"' in text
    config = load_config(config_path)
    assert config.plugins[0].version == "3.0"
    out = capsys.readouterr().out
    assert "added good 3.0 (modrinth)" in out
    assert f"next: rocket download --config {config_path}" in out
    assert not (tmp_path / "rocket.lock").exists()


def test_add_then_download_works_end_to_end(tmp_path: Path) -> None:
    jar = make_plugin_jar_bytes(name="Good")
    cdn_url = "https://cdn.modrinth.com/data/abc/versions/x/Good-3.0.jar"
    api = FakeAPI()
    add_modrinth_auto_routes(api)
    config_path = write_config(tmp_path, base_config())

    assert (
        main(
            ["add", "modrinth:good", "--config", str(config_path)],
            transport=api.transport(),
            sleep=lambda _: None,
        )
        == 0
    )

    add_paper_routes(api)
    api.add_json(modrinth_project_url("good"), modrinth_project_payload(slug="good"))
    api.add_bytes(cdn_url, jar)
    code = main(
        ["download", "--config", str(config_path)], transport=api.transport(), sleep=lambda _: None
    )

    assert code == 0
    assert (tmp_path / "artifacts" / "plugins" / "good.jar").read_bytes() == jar


def test_add_hangar_native_auto_writes_no_asset(tmp_path: Path) -> None:
    jar = make_plugin_jar_bytes(name="Good")
    api = FakeAPI()
    add_hangar_auto_routes(api, jar)

    assert run_add(api, tmp_path, ["hangar:Owner/Good"], base_config()) == 0

    text = (tmp_path / "server.toml").read_text(encoding="utf-8")
    assert 'version = "1.5.0"' in text
    assert "asset" not in text


def test_add_github_auto_picks_newest_usable_release(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    api.add_json(
        github_releases_url("Owner", "Good"),
        [
            github_release_payload(
                html_url="https://github.com/Owner/Good/releases/tag/2.0.0",
                tag="2.0.0",
                assets=[],
                prerelease=True,
            ),
            github_release_payload(
                html_url="https://github.com/Owner/Good/releases/tag/1.0.0",
                tag="1.0.0",
                assets=[
                    github_asset_payload(
                        name="Good-1.0.0.jar",
                        url="https://github.com/Owner/Good/releases/download/1.0.0/Good-1.0.0.jar",
                        size=10,
                    )
                ],
            ),
        ],
    )

    assert run_add(api, tmp_path, ["github:Owner/Good"], base_config()) == 0

    text = (tmp_path / "server.toml").read_text(encoding="utf-8")
    assert 'version = "1.0.0"' in text
    assert 'asset = "Good-1.0.0.jar"' in text
    assert "no Minecraft compatibility info" in capsys.readouterr().err


def test_add_hangar_external_auto_follows_fixed_tag_release(tmp_path: Path) -> None:
    tag_url = "https://github.com/Owner/Good/releases/tag/2.0.0"
    api = FakeAPI()
    api.add_json(
        hangar_versions_url("Good", platform_version="26.1.2"),
        hangar_version_list_payload([hangar_version_list_item(name="2.0.0")]),
    )
    api.add_json(
        hangar_version_url("Good", "2.0.0"),
        hangar_version_payload(
            name="2.0.0",
            downloads=hangar_external_paper(external_url=tag_url),
            platform_dependencies={"PAPER": ["26.1.2"]},
        ),
    )
    api.add_json(
        github_release_api_url("Owner", "Good", "2.0.0"),
        github_release_payload(
            html_url=tag_url,
            tag="2.0.0",
            assets=[
                github_asset_payload(
                    name="Good-2.0.0.jar",
                    url="https://github.com/Owner/Good/releases/download/2.0.0/Good-2.0.0.jar",
                    size=10,
                )
            ],
        ),
    )

    assert run_add(api, tmp_path, ["hangar:Owner/Good"], base_config()) == 0

    text = (tmp_path / "server.toml").read_text(encoding="utf-8")
    assert 'version = "2.0.0"' in text
    assert 'asset = "Good-2.0.0.jar"' in text


def add_spiget_auto_routes(api: FakeAPI, *, mc: str = "26.1") -> None:
    api.add_json(
        spiget_resource_url("28140"),
        spiget_resource_payload(resource_id=28140, name="LuckPerms", tested_versions=[mc]),
    )
    api.add_json(
        spiget_versions_url("28140"),
        [
            spiget_version_payload(version_id=648014, name="5.5.71", release_date=1786045114),
            spiget_version_payload(version_id=590885, name="5.5.0", release_date=1748247660),
        ],
    )


def test_add_spiget_auto_pins_newest_version(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    add_spiget_auto_routes(api)

    assert run_add(api, tmp_path, ["spiget:28140"], base_config()) == 0

    text = (tmp_path / "server.toml").read_text(encoding="utf-8")
    assert '[plugins.28140]\nprovider = "spiget"\nproject = "28140"\n' in text
    assert 'version = "5.5.71"' in text
    assert "asset" not in text
    assert "added 28140 5.5.71 (spiget)" in capsys.readouterr().out


def test_add_spiget_explicit_version_is_verified(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    add_spiget_auto_routes(api)

    assert (
        run_add(
            api,
            tmp_path,
            ["spiget:28140", "--version", "5.5.0", "--id", "luckperms"],
            base_config(),
        )
        == 0
    )

    text = (tmp_path / "server.toml").read_text(encoding="utf-8")
    assert "[plugins.luckperms]" in text
    assert 'version = "5.5.0"' in text

    api2 = FakeAPI()
    api2.add_json(spiget_resource_url("28141"), {"error": "not found"}, status=404)
    assert run_add(api2, tmp_path, ["spiget:28141", "--version", "9.9.9"], base_config()) == 1
    assert "not found" in capsys.readouterr().err


def test_add_spiget_rejects_asset_before_network(tmp_path: Path, capsys) -> None:
    api = FakeAPI()

    assert run_add(api, tmp_path, ["spiget:28140", "--asset", "LuckPerms.jar"], base_config()) == 1

    assert "asset cannot be verified" in capsys.readouterr().err
    assert api.requests == []
    assert "plugins" not in (tmp_path / "server.toml").read_text(encoding="utf-8")


def test_add_spiget_invalid_resource_id_fails_before_network(tmp_path: Path, capsys) -> None:
    api = FakeAPI()

    assert run_add(api, tmp_path, ["spiget:luckperms"], base_config()) == 1

    assert "not a valid Spigot resource id" in capsys.readouterr().err
    assert api.requests == []


def test_add_auto_with_asset_selects_among_multiple_jars(tmp_path: Path) -> None:
    tag_url = "https://github.com/Owner/Good/releases/tag/1.0.0"
    api = FakeAPI()
    api.add_json(
        github_releases_url("Owner", "Good"),
        [
            github_release_payload(
                html_url=tag_url,
                tag="1.0.0",
                assets=[
                    github_asset_payload(
                        name="Good-1.0.0.jar",
                        url="https://github.com/Owner/Good/releases/download/1.0.0/Good-1.0.0.jar",
                        size=10,
                    ),
                    github_asset_payload(
                        name="GoodExtra-1.0.0.jar",
                        url="https://github.com/Owner/Good/releases/download/1.0.0/GoodExtra-1.0.0.jar",
                        size=10,
                    ),
                ],
            )
        ],
    )

    assert (
        run_add(
            api, tmp_path, ["github:Owner/Good", "--asset", "GoodExtra-1.0.0.jar"], base_config()
        )
        == 0
    )

    text = (tmp_path / "server.toml").read_text(encoding="utf-8")
    assert 'asset = "GoodExtra-1.0.0.jar"' in text


def test_add_auto_without_any_compatible_version_fails(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    api.add_json(
        modrinth_versions_url("good"),
        [
            modrinth_version_payload(
                version_number="3.0",
                game_versions=["1.20.4"],
                loaders=["paper"],
                files=[],
            )
        ],
    )
    config_path = write_config(tmp_path, base_config())
    before = config_path.read_text(encoding="utf-8")

    code = main(
        ["add", "modrinth:good", "--config", str(config_path)],
        transport=api.transport(),
        sleep=lambda _: None,
    )

    assert code == 1
    assert "no PAPER version for Minecraft 26.1.2" in capsys.readouterr().err
    assert config_path.read_text(encoding="utf-8") == before


def test_add_explicit_hangar_version(tmp_path: Path) -> None:
    jar = make_plugin_jar_bytes(name="Good")
    api = FakeAPI()
    api.add_json(hangar_project_url("Good"), hangar_project_payload(owner="Owner", slug="Good"))
    api.add_json(
        hangar_version_url("Good", "1.5.0"),
        hangar_version_payload(
            name="1.5.0",
            downloads=hangar_native_paper(
                download_url=GOOD_HANGAR_DOWNLOAD,
                name="Good-1.5.0.jar",
                sha256=sha256_hex(jar),
                size=len(jar),
            ),
            platform_dependencies={"PAPER": ["26.1.2"]},
        ),
    )

    assert run_add(api, tmp_path, ["hangar:Owner/Good", "--version", "1.5.0"], base_config()) == 0

    text = (tmp_path / "server.toml").read_text(encoding="utf-8")
    assert 'version = "1.5.0"' in text
    assert "asset" not in text


def test_add_explicit_version_is_verified(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    api.add_json(hangar_project_url("Good"), hangar_project_payload(owner="Owner", slug="Good"))
    api.add_json(hangar_version_url("Good", "9.9.9"), {"ok": False}, status=404)
    config_path = write_config(tmp_path, base_config())
    before = config_path.read_text(encoding="utf-8")

    code = main(
        ["add", "hangar:Owner/Good", "--version", "9.9.9", "--config", str(config_path)],
        transport=api.transport(),
        sleep=lambda _: None,
    )

    assert code == 1
    assert "Hangar version not found" in capsys.readouterr().err
    assert config_path.read_text(encoding="utf-8") == before


def test_add_explicit_modrinth_multiple_jars_requires_asset(tmp_path: Path, capsys) -> None:
    jar = make_plugin_jar_bytes()
    api = FakeAPI()
    api.add_json(modrinth_project_url("good"), modrinth_project_payload(slug="good"))
    api.add_json(
        modrinth_versions_url("good"),
        [
            modrinth_version_payload(
                version_number="3.0",
                game_versions=["26.1.2"],
                loaders=["paper"],
                files=[
                    modrinth_file_payload(
                        filename="A-3.0.jar",
                        url="https://cdn.modrinth.com/data/abc/a.jar",
                        size=len(jar),
                        sha512=sha512_hex(jar),
                    ),
                    modrinth_file_payload(
                        filename="B-3.0.jar",
                        url="https://cdn.modrinth.com/data/abc/b.jar",
                        size=len(jar),
                        sha512=sha512_hex(jar),
                    ),
                ],
            )
        ],
    )

    code = run_add(api, tmp_path, ["modrinth:good", "--version", "3.0"], base_config())

    assert code == 1
    err = capsys.readouterr().err
    assert "multiple JAR files" in err
    assert "[plugins.good]" not in (tmp_path / "server.toml").read_text(encoding="utf-8")


def test_add_with_id_override(tmp_path: Path) -> None:
    api = FakeAPI()
    add_modrinth_auto_routes(api)

    assert run_add(api, tmp_path, ["modrinth:good", "--id", "perms"], base_config()) == 0

    text = (tmp_path / "server.toml").read_text(encoding="utf-8")
    assert "[plugins.perms]" in text


def test_add_derives_id_by_sanitizing_dots(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_json(
        github_releases_url("Owner", "Foo.Bar"),
        [
            github_release_payload(
                html_url="https://github.com/Owner/Foo.Bar/releases/tag/1.0",
                tag="1.0",
                assets=[
                    github_asset_payload(
                        name="FooBar-1.0.jar",
                        url="https://github.com/Owner/Foo.Bar/releases/download/1.0/FooBar-1.0.jar",
                        size=10,
                    )
                ],
            )
        ],
    )

    assert run_add(api, tmp_path, ["github:Owner/Foo.Bar"], base_config()) == 0

    assert "[plugins.foo-bar]" in (tmp_path / "server.toml").read_text(encoding="utf-8")


def test_add_duplicate_id_fails_before_network(tmp_path: Path, capsys) -> None:
    api = FakeAPI()

    assert run_add(api, tmp_path, ["hangar:Owner/Good"], base_config(GOOD_SECTION)) == 1

    assert "already exists" in capsys.readouterr().err
    assert api.requests == []


@pytest.mark.parametrize(
    "spec,message",
    [
        ("good", "invalid plugin spec"),
        ("foo:bar", "unknown provider"),
        ("hangar:NoSlash", "owner/name form"),
        ("modrinth:bad slug", "not a valid Modrinth slug"),
    ],
)
def test_add_invalid_specs_fail_before_network(tmp_path: Path, capsys, spec, message) -> None:
    api = FakeAPI()

    assert run_add(api, tmp_path, [spec], base_config()) == 1

    assert message in capsys.readouterr().err
    assert api.requests == []


def test_add_invalid_id_fails_before_network(tmp_path: Path, capsys) -> None:
    api = FakeAPI()

    assert run_add(api, tmp_path, ["modrinth:good", "--id", "BAD ID"], base_config()) == 1

    assert "invalid plugin id" in capsys.readouterr().err
    assert api.requests == []


def test_add_empty_version_or_asset_fails(tmp_path: Path, capsys) -> None:
    api = FakeAPI()

    assert run_add(api, tmp_path, ["modrinth:good", "--version", " "], base_config()) == 1
    assert "--version must not be empty" in capsys.readouterr().err
    assert run_add(api, tmp_path, ["modrinth:good", "--asset", ""], base_config()) == 1
    assert "--asset must not be empty" in capsys.readouterr().err
    assert api.requests == []


def test_add_missing_config_exits_2(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    missing = tmp_path / "none.toml"

    code = main(
        ["add", "modrinth:good", "--config", str(missing)],
        transport=api.transport(),
        sleep=lambda _: None,
    )

    assert code == 2
    assert "cannot read config file" in capsys.readouterr().err
    assert api.requests == []


def test_add_invalid_config_exits_2_before_network(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    text = base_config().replace('version = "26.1.2"', "version = 26.1")
    config_path = write_config(tmp_path, text)
    before = config_path.read_text(encoding="utf-8")

    code = main(
        ["add", "modrinth:good", "--config", str(config_path)],
        transport=api.transport(),
        sleep=lambda _: None,
    )

    assert code == 2
    assert "paper.version" in capsys.readouterr().err
    assert config_path.read_text(encoding="utf-8") == before
    assert api.requests == []


def test_remove_existing_plugin(tmp_path: Path, capsys) -> None:
    config_path = write_config(tmp_path, base_config(GOOD_SECTION))

    assert run_remove(tmp_path, "good", base_config(GOOD_SECTION)) == 0

    text = config_path.read_text(encoding="utf-8")
    assert "good" not in text
    assert load_config(config_path).plugins == ()
    assert capsys.readouterr().out == "removed good\n"


def test_remove_reports_kept_jar(tmp_path: Path, capsys) -> None:
    jar_dir = tmp_path / "artifacts" / "plugins"
    jar_dir.mkdir(parents=True)
    (jar_dir / "good.jar").write_bytes(b"jar")

    assert run_remove(tmp_path, "good", base_config(GOOD_SECTION)) == 0

    out = capsys.readouterr().out
    assert "removed good" in out
    assert "good.jar was kept on disk" in out


def test_remove_unknown_plugin(tmp_path: Path, capsys) -> None:
    config_path = write_config(tmp_path, base_config(GOOD_SECTION))
    before = config_path.read_text(encoding="utf-8")

    assert run_remove(tmp_path, "other", base_config(GOOD_SECTION)) == 1

    err = capsys.readouterr().err
    assert "not defined" in err
    assert "good" in err  # the hint lists known ids
    assert config_path.read_text(encoding="utf-8") == before


def test_remove_invalid_id(tmp_path: Path, capsys) -> None:
    assert run_remove(tmp_path, "BAD ID", base_config()) == 1
    assert "invalid plugin id" in capsys.readouterr().err


def test_remove_missing_config_exits_2(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "none.toml"

    assert main(["remove", "good", "--config", str(missing)]) == 2

    assert "cannot read config file" in capsys.readouterr().err
