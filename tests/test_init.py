"""Offline tests for ``rocket init``: prompts, version recommendations and the generated file."""

from __future__ import annotations

import io
from pathlib import Path

from rocket.catalog import DEFAULT_PLUGINS, CatalogPlugin
from rocket.cli import main
from rocket.config import load_config
from rocket.http_client import HTTPClient
from rocket.recommend import recommend_plugins
from rocket.wizard import build_config_text, run_init

from .helpers import (
    FILL_PROJECTS_URL,
    TEST_USER_AGENT,
    FakeAPI,
    ScriptedPrompter,
    fill_project_payload,
    github_asset_payload,
    github_release_api_url,
    github_release_payload,
    github_releases_url,
    hangar_external_paper,
    hangar_native_paper,
    hangar_version_list_item,
    hangar_version_list_payload,
    hangar_version_payload,
    hangar_version_url,
    hangar_versions_url,
    modrinth_file_payload,
    modrinth_version_payload,
    modrinth_versions_url,
    sha256_hex,
    sha512_hex,
)

VIAVERSION_JAR_URL = (
    "https://hangarcdn.papermc.io/plugins/ViaVersion/ViaVersion/versions/5.12.1"
    "/PAPER/ViaVersion-5.12.1.jar"
)
COREPROTECT_FILE_URL = (
    "https://cdn.modrinth.com/data/coreprotect/versions/24.1/CoreProtect-CE-24.1.jar"
)
VAULT_ASSET_URL = "https://github.com/MilkBowl/Vault/releases/download/1.7.3/Vault.jar"
ESSENTIALS_TAG_URL = "https://github.com/EssentialsX/Essentials/releases/tag/2.22.0"
ESSENTIALS_DOWNLOAD_BASE = "https://github.com/EssentialsX/Essentials/releases/download/2.22.0"

SMALL_CATALOG = (
    CatalogPlugin(
        "viaversion", "ViaVersion", "client compatibility", "hangar", "ViaVersion/ViaVersion"
    ),
    CatalogPlugin("coreprotect", "CoreProtect", "block logging", "modrinth", "coreprotect"),
    CatalogPlugin(
        "vault", "Vault", "economy api", "github", "MilkBowl/Vault", assets=("Vault.jar",)
    ),
)


def make_http(api: FakeAPI) -> HTTPClient:
    return HTTPClient(user_agent=TEST_USER_AGENT, transport=api.transport(), sleep=lambda _: None)


def add_small_catalog_routes(api: FakeAPI, *, paper_version: str = "1.21.11") -> None:
    api.add_json(
        FILL_PROJECTS_URL,
        fill_project_payload({"1.21": ["1.21.11", "1.21.11-rc3", "1.21.10"]}),
    )
    viaversion_jar = b"viaversion-jar"
    api.add_json(
        hangar_versions_url("ViaVersion", platform_version=paper_version),
        hangar_version_list_payload(
            [
                hangar_version_list_item(name="5.12.2-SNAPSHOT+1104", channel="Snapshot"),
                hangar_version_list_item(name="5.12.1"),
            ]
        ),
    )
    api.add_json(
        hangar_version_url("ViaVersion", "5.12.1"),
        hangar_version_payload(
            name="5.12.1",
            downloads=hangar_native_paper(
                download_url=VIAVERSION_JAR_URL,
                name="ViaVersion-5.12.1.jar",
                sha256=sha256_hex(viaversion_jar),
                size=len(viaversion_jar),
            ),
            platform_dependencies={"PAPER": ["1.21.11"]},
        ),
    )
    coreprotect_jar = b"coreprotect-jar"
    api.add_json(
        modrinth_versions_url("coreprotect"),
        [
            modrinth_version_payload(
                version_number="24.1",
                game_versions=["1.21.11"],
                loaders=["paper"],
                files=[
                    modrinth_file_payload(
                        filename="CoreProtect-CE-24.1.jar",
                        url=COREPROTECT_FILE_URL,
                        size=len(coreprotect_jar),
                        sha512=sha512_hex(coreprotect_jar),
                    )
                ],
            )
        ],
    )
    vault_jar = b"vault-jar"
    api.add_json(
        github_releases_url("MilkBowl", "Vault"),
        [
            github_release_payload(
                html_url="https://github.com/MilkBowl/Vault/releases/tag/1.7.3",
                tag="1.7.3",
                assets=[
                    github_asset_payload(name="Vault.jar", url=VAULT_ASSET_URL, size=len(vault_jar))
                ],
            )
        ],
    )


def test_init_wizard_writes_expected_config(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    add_small_catalog_routes(api)
    prompter = ScriptedPrompter(select=["paper", "1.21.11"])
    path = tmp_path / "server.toml"

    code = run_init(
        path,
        transport=api.transport(),
        sleep=lambda _: None,
        prompter=prompter,
        catalog=SMALL_CATALOG,
    )

    assert code == 0
    expected = """schema_version = 1

[paper]
version = "1.21.11"
channel = "STABLE"

[download]
output_dir = "./artifacts"

[plugin_defaults]
platform = "PAPER"
follow_external = true

[plugins.viaversion]
provider = "hangar"
project = "ViaVersion/ViaVersion"
version = "5.12.1"

[plugins.coreprotect]
provider = "modrinth"
project = "coreprotect"
version = "24.1"
asset = "CoreProtect-CE-24.1.jar"

[plugins.vault]
provider = "github"
project = "MilkBowl/Vault"
version = "1.7.3"
asset = "Vault.jar"
"""
    assert path.read_text(encoding="utf-8") == expected

    config = load_config(path)  # round-trips through the real strict validator
    assert config.paper.version == "1.21.11"
    assert [plugin.id for plugin in config.plugins] == ["viaversion", "coreprotect", "vault"]
    assert config.plugins[1].asset == "CoreProtect-CE-24.1.jar"

    assert prompter.choices["Paper version (newest first)"] == ["1.21.11", "1.21.10"]
    assert set(prompter.choices["Default plugins"]) == {"viaversion", "coreprotect", "vault"}
    assert [kind for kind, _ in prompter.calls] == ["select", "select", "confirm", "checkbox"]

    out = capsys.readouterr().out
    assert "Resolved 3 of 3 plugins for Paper 1.21.11" in out
    assert "  + viaversion 5.12.1 (hangar)" in out
    assert f"wrote {path}" in out


def test_init_reports_skipped_plugins_and_still_writes(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    api.add_json(
        FILL_PROJECTS_URL,
        fill_project_payload({"1.21": ["1.21.11"]}),
    )
    api.add_json(
        hangar_versions_url("ViaVersion", platform_version="1.21.11"),
        hangar_version_list_payload([hangar_version_list_item(name="5.12.1")]),
    )
    viaversion_jar = b"viaversion-jar"
    api.add_json(
        hangar_version_url("ViaVersion", "5.12.1"),
        hangar_version_payload(
            name="5.12.1",
            downloads=hangar_native_paper(
                download_url=VIAVERSION_JAR_URL,
                name="ViaVersion-5.12.1.jar",
                sha256=sha256_hex(viaversion_jar),
                size=len(viaversion_jar),
            ),
            platform_dependencies={"PAPER": ["1.21.11"]},
        ),
    )
    api.add_json(
        modrinth_versions_url("coreprotect"),
        [
            modrinth_version_payload(
                version_number="23.2",
                game_versions=["1.21.10"],
                loaders=["paper"],
                files=[
                    modrinth_file_payload(
                        filename="CoreProtect-CE-23.2.jar",
                        url=COREPROTECT_FILE_URL,
                        size=10,
                        sha512=sha512_hex(b"coreprotect-jar"),
                    )
                ],
            )
        ],
    )
    prompter = ScriptedPrompter(select=["paper", "1.21.11"])
    path = tmp_path / "server.toml"

    code = run_init(
        path,
        transport=api.transport(),
        sleep=lambda _: None,
        prompter=prompter,
        catalog=SMALL_CATALOG[:2],
    )

    assert code == 0
    config = load_config(path)
    assert [plugin.id for plugin in config.plugins] == ["viaversion"]
    err = capsys.readouterr().err
    assert "[coreprotect] skipped:" in err
    assert "no PAPER version for Minecraft 1.21.11" in err


def test_init_refuses_to_overwrite_without_force(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    path = tmp_path / "server.toml"
    path.write_text("keep me\n", encoding="utf-8")
    prompter = ScriptedPrompter(select=["paper", "1.21.11"])

    code = run_init(
        path,
        transport=api.transport(),
        sleep=lambda _: None,
        prompter=prompter,
        catalog=(),
    )

    assert code == 1
    assert path.read_text(encoding="utf-8") == "keep me\n"
    err = capsys.readouterr().err
    assert "already exists" in err
    assert "--force" in err
    assert prompter.calls == []
    assert api.requests == []


def test_init_force_overwrites_existing_file(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_json(FILL_PROJECTS_URL, fill_project_payload({"1.21": ["1.21.11"]}))
    path = tmp_path / "server.toml"
    path.write_text("old content\n", encoding="utf-8")

    code = run_init(
        path,
        force=True,
        transport=api.transport(),
        sleep=lambda _: None,
        prompter=ScriptedPrompter(select=["paper", "1.21.11"]),
        catalog=(),
    )

    assert code == 0
    text = path.read_text(encoding="utf-8")
    assert text == build_config_text(paper_version="1.21.11", plugins=())
    assert "[plugins." not in text


def test_init_falls_back_to_manual_paper_version(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    api.add_json(FILL_PROJECTS_URL, {"message": "unavailable"}, status=503)
    path = tmp_path / "server.toml"

    code = run_init(
        path,
        transport=api.transport(),
        sleep=lambda _: None,
        prompter=ScriptedPrompter(select=["paper"], text="1.21.10"),
        catalog=(),
    )

    assert code == 0
    assert load_config(path).paper.version == "1.21.10"
    assert "cannot list Paper versions" in capsys.readouterr().err


def test_init_abort_writes_nothing(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    api.add_json(FILL_PROJECTS_URL, fill_project_payload({"1.21": ["1.21.11"]}))
    path = tmp_path / "server.toml"

    code = run_init(
        path,
        transport=api.transport(),
        sleep=lambda _: None,
        prompter=ScriptedPrompter(abort_on="select"),
        catalog=(),
    )

    assert code == 1
    assert not path.exists()
    assert "aborted" in capsys.readouterr().err


def test_init_requires_a_terminal_without_injected_prompter(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO())

    code = run_init(tmp_path / "server.toml")

    assert code == 1
    assert "interactive" in capsys.readouterr().err
    assert not (tmp_path / "server.toml").exists()


def test_cli_init_command(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_json(FILL_PROJECTS_URL, fill_project_payload({"1.21": ["1.21.11"]}))
    path = tmp_path / "server.toml"

    code = main(
        ["init", "--config", str(path)],
        transport=api.transport(),
        sleep=lambda _: None,
        prompter=ScriptedPrompter(select=["paper", "1.21.11"], confirm=False),
    )

    assert code == 0
    config = load_config(path)
    assert config.paper.version == "1.21.11"
    assert config.plugins == ()


def test_hangar_external_group_resolves_once_per_project() -> None:
    api = FakeAPI()
    api.add_json(
        hangar_versions_url("Essentials", platform_version="1.21.11"),
        hangar_version_list_payload([hangar_version_list_item(name="2.22.0")]),
    )
    api.add_json(
        hangar_version_url("Essentials", "2.22.0"),
        hangar_version_payload(
            name="2.22.0",
            downloads=hangar_external_paper(external_url=ESSENTIALS_TAG_URL),
            platform_dependencies={"PAPER": ["1.21.11"]},
        ),
    )
    api.add_json(
        github_release_api_url("EssentialsX", "Essentials", "2.22.0"),
        github_release_payload(
            html_url=ESSENTIALS_TAG_URL,
            tag="2.22.0",
            assets=[
                github_asset_payload(
                    name=name,
                    url=f"{ESSENTIALS_DOWNLOAD_BASE}/{name}",
                    size=10,
                )
                for name in (
                    "EssentialsX-2.22.0.jar",
                    "EssentialsXAntiBuild-2.22.0.jar",
                    "EssentialsXChat-2.22.0.jar",
                )
            ],
        ),
    )
    catalog = (
        CatalogPlugin(
            "essentialsx",
            "EssentialsX",
            "core commands",
            "hangar",
            "EssentialsX/Essentials",
            assets=("EssentialsX-{version}.jar",),
        ),
        CatalogPlugin(
            "essentialsx_chat",
            "EssentialsX Chat",
            "chat module",
            "hangar",
            "EssentialsX/Essentials",
            assets=("EssentialsXChat-{version}.jar",),
        ),
        CatalogPlugin(
            "essentialsx_spawn",
            "EssentialsX Spawn",
            "spawn module",
            "hangar",
            "EssentialsX/Essentials",
            assets=("EssentialsXSpawn-{version}.jar",),
        ),
    )

    with make_http(api) as http:
        result = recommend_plugins(catalog, minecraft_version="1.21.11", http=http)

    assert [plugin.id for plugin in result.plugins] == ["essentialsx", "essentialsx_chat"]
    assert [plugin.asset for plugin in result.plugins] == [
        "EssentialsX-2.22.0.jar",
        "EssentialsXChat-2.22.0.jar",
    ]
    assert [skipped.id for skipped in result.skipped] == ["essentialsx_spawn"]
    assert "expected attachments" in result.skipped[0].reason
    assert api.count(hangar_versions_url("Essentials", platform_version="1.21.11")) == 1
    assert api.count(hangar_version_url("Essentials", "2.22.0")) == 1
    assert api.count(github_release_api_url("EssentialsX", "Essentials", "2.22.0")) == 1


def test_github_recommendation_walks_back_to_a_usable_release() -> None:
    api = FakeAPI()
    api.add_json(
        github_releases_url("Zrips", "Residence"),
        [
            github_release_payload(
                html_url="https://github.com/Zrips/Residence/releases/tag/6.0.2.3",
                tag="6.0.2.3",
                assets=[],
                draft=True,
            ),
            github_release_payload(
                html_url="https://github.com/Zrips/Residence/releases/tag/6.0.1.0",
                tag="6.0.1.0",
                assets=[],
                prerelease=True,
            ),
            github_release_payload(
                html_url="https://github.com/Zrips/Residence/releases/tag/6.0.0.1",
                tag="6.0.0.1",
                assets=[
                    github_asset_payload(
                        name="Residence6.0.0.1.jar",
                        url="https://github.com/Zrips/Residence/releases/download/6.0.0.1/Residence6.0.0.1.jar",
                        size=10,
                    )
                ],
            ),
        ],
    )
    catalog = (
        CatalogPlugin(
            "residence",
            "Residence",
            "land claims",
            "github",
            "Zrips/Residence",
            assets=("Residence{version}.jar",),
        ),
    )

    with make_http(api) as http:
        result = recommend_plugins(catalog, minecraft_version="1.21.11", http=http)

    assert result.skipped == ()
    plugin = result.plugins[0]
    assert plugin.version == "6.0.0.1"
    assert plugin.asset == "Residence6.0.0.1.jar"
    assert "no Minecraft compatibility info" in plugin.note


def test_modrinth_prefers_a_release_over_a_newer_beta() -> None:
    api = FakeAPI()
    api.add_json(
        modrinth_versions_url("luckperms"),
        [
            modrinth_version_payload(
                version_number="v6.0.0-bukkit",
                game_versions=["1.21.11"],
                loaders=["bukkit"],
                files=[
                    modrinth_file_payload(
                        filename="LuckPerms-Bukkit-6.0.0.jar",
                        url="https://cdn.modrinth.com/data/luckperms/versions/x/LuckPerms-Bukkit-6.0.0.jar",
                        size=10,
                        sha512=sha512_hex(b"a"),
                    )
                ],
                version_type="beta",
            ),
            modrinth_version_payload(
                version_number="v5.5.71-bukkit",
                game_versions=["1.21.11"],
                loaders=["paper"],
                files=[
                    modrinth_file_payload(
                        filename="LuckPerms-Bukkit-5.5.71.jar",
                        url="https://cdn.modrinth.com/data/luckperms/versions/y/LuckPerms-Bukkit-5.5.71.jar",
                        size=10,
                        sha512=sha512_hex(b"b"),
                    )
                ],
            ),
        ],
    )
    catalog = (CatalogPlugin("luckperms", "LuckPerms", "permissions", "modrinth", "luckperms"),)

    with make_http(api) as http:
        result = recommend_plugins(catalog, minecraft_version="1.21.11", http=http)

    assert result.skipped == ()
    assert result.plugins[0].version == "v5.5.71-bukkit"
    assert result.plugins[0].asset == "LuckPerms-Bukkit-5.5.71.jar"
    assert result.plugins[0].note == ""


def test_modrinth_beta_fallback_notes_and_incompatible_versions_skip() -> None:
    api = FakeAPI()
    api.add_json(
        modrinth_versions_url("betaonly"),
        [
            modrinth_version_payload(
                version_number="1.0.0",
                game_versions=["1.21.11"],
                loaders=["paper"],
                files=[
                    modrinth_file_payload(
                        filename="BetaOnly-1.0.0.jar",
                        url="https://cdn.modrinth.com/data/betaonly/versions/1.0.0/BetaOnly-1.0.0.jar",
                        size=10,
                        sha512=sha512_hex(b"a"),
                    )
                ],
                version_type="beta",
            )
        ],
    )
    api.add_json(
        modrinth_versions_url("oldonly"),
        [
            modrinth_version_payload(
                version_number="9.9.9",
                game_versions=["1.20.6"],
                loaders=["paper"],
                files=[
                    modrinth_file_payload(
                        filename="OldOnly-9.9.9.jar",
                        url="https://cdn.modrinth.com/data/oldonly/versions/9.9.9/OldOnly-9.9.9.jar",
                        size=10,
                        sha512=sha512_hex(b"b"),
                    )
                ],
            )
        ],
    )
    catalog = (
        CatalogPlugin("betaonly", "BetaOnly", "", "modrinth", "betaonly"),
        CatalogPlugin("oldonly", "OldOnly", "", "modrinth", "oldonly"),
    )

    with make_http(api) as http:
        result = recommend_plugins(catalog, minecraft_version="1.21.11", http=http)

    assert [plugin.id for plugin in result.plugins] == ["betaonly"]
    assert "using beta" in result.plugins[0].note
    assert [skipped.id for skipped in result.skipped] == ["oldonly"]
    assert "no PAPER version for Minecraft 1.21.11" in result.skipped[0].reason


def test_default_catalog_matches_the_reference_config() -> None:
    assert [plugin.id for plugin in DEFAULT_PLUGINS] == [
        "placeholderapi",
        "viaversion",
        "viabackwards",
        "essentialsx",
        "essentialsx_chat",
        "essentialsx_spawn",
        "coreprotect",
        "residence",
        "cmilib",
        "luckperms",
        "tab",
        "vault",
    ]
    by_id = {plugin.id: plugin for plugin in DEFAULT_PLUGINS}
    assert by_id["placeholderapi"].provider == "hangar"
    assert by_id["placeholderapi"].assets == ()
    assert by_id["essentialsx"].project == "EssentialsX/Essentials"
    assert by_id["essentialsx_chat"].assets == ("EssentialsXChat-{version}.jar",)
    assert by_id["coreprotect"].provider == "modrinth"
    assert by_id["residence"].provider == "zrips"
    assert by_id["residence"].project == "Residence"
    assert by_id["residence"].assets == ()
    assert by_id["cmilib"].provider == "zrips"
    assert by_id["cmilib"].project == "CMILib"
    assert by_id["cmilib"].assets == ()
    assert by_id["tab"].assets == ("TAB.v{version}.jar",)
    assert by_id["vault"].assets == ("Vault.jar",)
