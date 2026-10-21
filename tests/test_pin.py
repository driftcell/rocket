"""Offline tests for ``rocket pin``: interactive exact-version selection (incl. downgrades)."""

from __future__ import annotations

from pathlib import Path

from rocket.cli import main

from .helpers import (
    FILL_PROJECTS_URL,
    FakeAPI,
    ScriptedPrompter,
    add_fill_builds,
    add_paper_routes,
    base_config,
    fill_project_payload,
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
    make_paper_jar_bytes,
    make_plugin_jar_bytes,
    modrinth_file_payload,
    modrinth_project_payload,
    modrinth_project_url,
    modrinth_version_payload,
    modrinth_versions_url,
    sha256_hex,
    sha512_hex,
    spiget_download_url,
    spiget_resource_payload,
    spiget_resource_url,
    spiget_version_payload,
    spiget_versions_url,
    write_config,
)

GOOD_PLUGIN = '[plugins.good]\nprovider = "hangar"\nproject = "Owner/Good"\nversion = "1.0.0"\n'
GOOD_1_0_URL = "https://hangarcdn.papermc.io/plugins/Owner/Good/versions/1.0.0/PAPER/Good-1.0.0.jar"


def run_pin(
    tmp_path: Path,
    api: FakeAPI,
    text: str,
    *,
    argv: list[str],
    prompter: ScriptedPrompter | None = None,
) -> int:
    config_path = write_config(tmp_path, text)
    args = [*argv, "--config", str(config_path)]
    return main(args, transport=api.transport(), sleep=lambda _: None, prompter=prompter)


def add_good_download_routes(api: FakeAPI, version: str, jar: bytes, *, mc: str = "26.1.2") -> None:
    """Routes the download phase needs after pinning hangar Owner/Good to ``version``."""
    url = f"https://hangarcdn.papermc.io/plugins/Owner/Good/versions/{version}/PAPER/Good-{version}.jar"
    api.add_json(hangar_project_url("Good"), hangar_project_payload(owner="Owner", slug="Good"))
    api.add_json(
        hangar_version_url("Good", version),
        hangar_version_payload(
            name=version,
            downloads=hangar_native_paper(
                download_url=url,
                name=f"Good-{version}.jar",
                sha256=sha256_hex(jar),
                size=len(jar),
            ),
            platform_dependencies={"PAPER": [mc]},
        ),
    )
    api.add_bytes(url, jar)


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------


def test_pin_core_two_level_select_with_auto_build(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_json(
        FILL_PROJECTS_URL,
        fill_project_payload({"26.2": ["26.2.0"], "26.1": ["26.1.2", "26.1.1"]}),
    )
    new_jar = make_paper_jar_bytes(main_class="io.papermc.paper.NewMain")
    add_fill_builds(api, "26.1.2", [(9, new_jar)])
    text = base_config("", paper_version="26.1.1")
    prompter = ScriptedPrompter(select=["26.1", "26.1.2", "__auto__"])

    code = run_pin(tmp_path, api, text, argv=["pin", "core", "-i"], prompter=prompter)

    assert code == 0
    config_text = (tmp_path / "server.toml").read_text()
    assert 'version = "26.1.2"' in config_text
    assert "build" not in config_text
    assert (tmp_path / "artifacts" / "paper.jar").read_bytes() == new_jar
    messages = [message for kind, message in prompter.calls if kind == "select"]
    assert messages == [
        "Paper release line (current: 26.1.1)",
        "Paper version (line 26.1)",
        "Paper build for 26.1.2",
    ]


def test_pin_core_specific_build_pinned(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_json(FILL_PROJECTS_URL, fill_project_payload({"26.1": ["26.1.2"]}))
    new_jar = make_paper_jar_bytes(main_class="io.papermc.paper.NewMain")
    add_fill_builds(api, "26.1.2", [(57, new_jar), (42, make_paper_jar_bytes())])
    text = base_config("", paper_version="26.1.2")
    prompter = ScriptedPrompter(select=["26.1", "26.1.2", "57"])

    code = run_pin(tmp_path, api, text, argv=["pin", "core", "-i"], prompter=prompter)

    assert code == 0
    config_text = (tmp_path / "server.toml").read_text()
    assert 'version = "26.1.2"' in config_text
    assert "build = 57" in config_text
    assert (tmp_path / "artifacts" / "paper.jar").read_bytes() == new_jar


def test_pin_core_no_change_skips_download(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    api.add_json(FILL_PROJECTS_URL, fill_project_payload({"26.1": ["26.1.2"]}))
    add_fill_builds(api, "26.1.2", [(42, make_paper_jar_bytes())])
    text = base_config("", paper_version="26.1.2")
    prompter = ScriptedPrompter(select=["26.1", "26.1.2", "__auto__"])

    code = run_pin(tmp_path, api, text, argv=["pin", "core", "-i"], prompter=prompter)

    assert code == 0
    assert "nothing to change" in capsys.readouterr().out
    assert (tmp_path / "server.toml").read_text() == text
    assert not (tmp_path / "artifacts").exists()


# ---------------------------------------------------------------------------
# Plugins
# ---------------------------------------------------------------------------


def test_pin_plugin_hangar_downgrade(tmp_path: Path) -> None:
    api = FakeAPI()
    add_paper_routes(api)
    old_jar = make_plugin_jar_bytes(name="Good10")
    api.add_json(
        hangar_versions_url("Good", platform_version="26.1.2", channel=None, limit=30),
        hangar_version_list_payload(
            [
                hangar_version_list_item(name="1.1.0"),
                hangar_version_list_item(name="1.0.0"),
            ]
        ),
    )
    add_good_download_routes(api, "1.0.0", old_jar)
    text = base_config(GOOD_PLUGIN.replace('"1.0.0"', '"1.1.0"'))
    prompter = ScriptedPrompter(select=["1.0.0"])

    code = run_pin(tmp_path, api, text, argv=["pin", "good", "--interactive"], prompter=prompter)

    assert code == 0
    assert 'version = "1.0.0"' in (tmp_path / "server.toml").read_text()
    assert (tmp_path / "artifacts" / "plugins" / "good.jar").read_bytes() == old_jar
    choices = prompter.choices["good version (current: 1.1.0)"]
    assert choices == ["1.1.0", "1.0.0"]


def test_pin_plugin_hangar_lists_snapshot_channel(tmp_path: Path) -> None:
    api = FakeAPI()
    add_paper_routes(api)
    snapshot_jar = make_plugin_jar_bytes(name="GoodSnap")
    api.add_json(
        hangar_versions_url("Good", platform_version="26.1.2", channel=None, limit=30),
        hangar_version_list_payload(
            [
                hangar_version_list_item(name="1.2.0-SNAPSHOT", channel="Snapshot"),
                hangar_version_list_item(name="1.1.0"),
            ]
        ),
    )
    add_good_download_routes(api, "1.2.0-SNAPSHOT", snapshot_jar)
    prompter = ScriptedPrompter(select=["1.2.0-SNAPSHOT"])

    code = run_pin(
        tmp_path,
        api,
        base_config(GOOD_PLUGIN),
        argv=["pin", "good", "--interactive"],
        prompter=prompter,
    )

    assert code == 0
    assert 'version = "1.2.0-SNAPSHOT"' in (tmp_path / "server.toml").read_text()


def test_pin_plugin_spiget_downgrade_pins_no_asset(tmp_path: Path) -> None:
    api = FakeAPI()
    add_paper_routes(api)
    jar = make_plugin_jar_bytes(name="LuckPerms550")
    api.add_json(
        spiget_resource_url("28140"),
        spiget_resource_payload(resource_id=28140, name="LuckPerms", tested_versions=["26.1"]),
    )
    api.add_json(
        spiget_versions_url("28140"),
        [
            spiget_version_payload(version_id=648014, name="5.5.71", release_date=1786045114),
            spiget_version_payload(version_id=590885, name="5.5.0", release_date=1748247660),
        ],
    )
    api.add_bytes(spiget_download_url("28140", 590885), jar)
    text = base_config(
        '[plugins.luckperms]\nprovider = "spiget"\nproject = "28140"\nversion = "5.5.71"\n'
    )
    prompter = ScriptedPrompter(select=["5.5.0"])

    code = run_pin(
        tmp_path, api, text, argv=["pin", "luckperms", "--interactive"], prompter=prompter
    )

    assert code == 0
    config_text = (tmp_path / "server.toml").read_text()
    assert 'version = "5.5.0"' in config_text
    assert "asset" not in config_text
    assert (tmp_path / "artifacts" / "plugins" / "luckperms.jar").read_bytes() == jar
    # spiget serves one unnamed file per version: no file prompt follows the version pick
    assert [kind for kind, _ in prompter.calls] == ["select"]
    choices = prompter.choices["luckperms version (current: 5.5.71)"]
    assert choices == ["5.5.71", "5.5.0"]


def test_pin_plugin_spiget_incompatible_declared_versions_fail(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    api.add_json(
        spiget_resource_url("28140"),
        spiget_resource_payload(resource_id=28140, name="LuckPerms", tested_versions=["1.12"]),
    )
    text = base_config(
        '[plugins.luckperms]\nprovider = "spiget"\nproject = "28140"\nversion = "5.5.71"\n'
    )
    prompter = ScriptedPrompter(select=["5.5.71"])

    code = run_pin(
        tmp_path, api, text, argv=["pin", "luckperms", "--interactive"], prompter=prompter
    )

    assert code == 1
    assert "declared tested versions do not include Minecraft 26.1.2" in capsys.readouterr().err
    assert prompter.calls == []


def test_pin_plugin_modrinth_multi_jar_selects_the_file(tmp_path: Path) -> None:
    api = FakeAPI()
    add_paper_routes(api)
    jar_a = make_plugin_jar_bytes(name="CpA")
    jar_b = make_plugin_jar_bytes(name="CpB")
    url_b = "https://cdn.modrinth.com/data/coreprotect/versions/24.1/CoreProtect-CE-24.1-b.jar"
    api.add_json(modrinth_project_url("coreprotect"), modrinth_project_payload(slug="coreprotect"))
    api.add_json(
        modrinth_versions_url("coreprotect"),
        [
            modrinth_version_payload(
                version_number="24.1",
                game_versions=["26.1.2"],
                loaders=["paper"],
                files=[
                    modrinth_file_payload(
                        filename="CoreProtect-CE-24.1-a.jar",
                        url="https://cdn.modrinth.com/data/coreprotect/versions/24.1/a.jar",
                        size=len(jar_a),
                        sha512=sha512_hex(jar_a),
                    ),
                    modrinth_file_payload(
                        filename="CoreProtect-CE-24.1-b.jar",
                        url=url_b,
                        size=len(jar_b),
                        sha512=sha512_hex(jar_b),
                    ),
                ],
            )
        ],
    )
    api.add_bytes(url_b, jar_b)
    text = base_config(
        '[plugins.coreprotect]\nprovider = "modrinth"\nproject = "coreprotect"\n'
        'version = "23.2"\nasset = "CoreProtect-CE-23.2.jar"\n'
    )
    prompter = ScriptedPrompter(select=["24.1", "CoreProtect-CE-24.1-b.jar"])

    code = run_pin(
        tmp_path, api, text, argv=["pin", "coreprotect", "--interactive"], prompter=prompter
    )

    assert code == 0
    config_text = (tmp_path / "server.toml").read_text()
    assert 'version = "24.1"' in config_text
    assert 'asset = "CoreProtect-CE-24.1-b.jar"' in config_text
    assert (tmp_path / "artifacts" / "plugins" / "coreprotect.jar").read_bytes() == jar_b
    messages = [message for kind, message in prompter.calls if kind == "select"]
    assert messages == ["coreprotect version (current: 23.2)", "coreprotect 24.1 file"]


def test_pin_plugin_github_release(tmp_path: Path) -> None:
    api = FakeAPI()
    add_paper_routes(api)
    jar = make_plugin_jar_bytes(name="Vault172")
    jar_url = "https://github.com/MilkBowl/Vault/releases/download/1.7.2/Vault.jar"
    api.add_json(
        github_releases_url("MilkBowl", "Vault"),
        [
            github_release_payload(
                html_url="https://github.com/MilkBowl/Vault/releases/tag/1.7.3",
                tag="1.7.3",
                assets=[],
            ),
            github_release_payload(
                html_url="https://github.com/MilkBowl/Vault/releases/tag/1.7.2",
                tag="1.7.2",
                assets=[],
            ),
        ],
    )
    api.add_json(
        github_release_api_url("MilkBowl", "Vault", "1.7.2"),
        github_release_payload(
            html_url="https://github.com/MilkBowl/Vault/releases/tag/1.7.2",
            tag="1.7.2",
            assets=[
                github_asset_payload(
                    name="Vault.jar", url=jar_url, size=len(jar), digest=f"sha256:{sha256_hex(jar)}"
                )
            ],
        ),
    )
    api.add_bytes(jar_url, jar)
    text = base_config(
        '[plugins.vault]\nprovider = "github"\nproject = "MilkBowl/Vault"\nversion = "1.7.3"\n'
    )
    prompter = ScriptedPrompter(select=["1.7.2"])

    code = run_pin(tmp_path, api, text, argv=["pin", "vault", "--interactive"], prompter=prompter)

    assert code == 0
    assert 'version = "1.7.2"' in (tmp_path / "server.toml").read_text()
    assert (tmp_path / "artifacts" / "plugins" / "vault.jar").read_bytes() == jar


def test_pin_plugin_hangar_external_release(tmp_path: Path) -> None:
    api = FakeAPI()
    add_paper_routes(api)
    jar = make_plugin_jar_bytes(name="Ess222")
    tag_url = "https://github.com/EssentialsX/Essentials/releases/tag/2.22.0"
    jar_url = (
        "https://github.com/EssentialsX/Essentials/releases/download/2.22.0/EssentialsX-2.22.0.jar"
    )
    api.add_json(
        hangar_versions_url("Essentials", platform_version="26.1.2", channel=None, limit=30),
        hangar_version_list_payload([hangar_version_list_item(name="2.22.0")]),
    )
    api.add_json(
        hangar_version_url("Essentials", "2.22.0"),
        hangar_version_payload(
            name="2.22.0", downloads=hangar_external_paper(external_url=tag_url)
        ),
    )
    api.add_json(
        hangar_project_url("Essentials"),
        hangar_project_payload(owner="EssentialsX", slug="Essentials"),
    )
    api.add_json(
        github_release_api_url("EssentialsX", "Essentials", "2.22.0"),
        github_release_payload(
            html_url=tag_url,
            tag="2.22.0",
            assets=[
                github_asset_payload(
                    name="EssentialsX-2.22.0.jar",
                    url=jar_url,
                    size=len(jar),
                    digest=f"sha256:{sha256_hex(jar)}",
                )
            ],
        ),
    )
    api.add_bytes(jar_url, jar)
    text = base_config(
        '[plugins.essentialsx]\nprovider = "hangar"\nproject = "EssentialsX/Essentials"\n'
        'version = "2.21.0"\nasset = "EssentialsX-2.21.0.jar"\n'
    )
    prompter = ScriptedPrompter(select=["2.22.0"])

    code = run_pin(
        tmp_path, api, text, argv=["pin", "essentialsx", "--interactive"], prompter=prompter
    )

    assert code == 0
    config_text = (tmp_path / "server.toml").read_text()
    assert 'version = "2.22.0"' in config_text
    assert 'asset = "EssentialsX-2.22.0.jar"' in config_text


def test_pin_rolls_back_failed_download(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    add_paper_routes(api)
    bad_jar = make_plugin_jar_bytes(name="Good11")
    api.add_json(
        hangar_versions_url("Good", platform_version="26.1.2", channel=None, limit=30),
        hangar_version_list_payload([hangar_version_list_item(name="1.1.0")]),
    )
    api.add_json(hangar_project_url("Good"), hangar_project_payload(owner="Owner", slug="Good"))
    api.add_json(
        hangar_version_url("Good", "1.1.0"),
        hangar_version_payload(
            name="1.1.0",
            downloads=hangar_native_paper(
                download_url=GOOD_1_0_URL,
                name="Good-1.1.0.jar",
                sha256=sha256_hex(b"different-bytes"),  # hash mismatch on download
                size=len(b"different-bytes"),
            ),
            platform_dependencies={"PAPER": ["26.1.2"]},
        ),
    )
    api.add_bytes(GOOD_1_0_URL, bad_jar)
    text = base_config(GOOD_PLUGIN)
    prompter = ScriptedPrompter(select=["1.1.0"])

    code = run_pin(tmp_path, api, text, argv=["pin", "good", "--interactive"], prompter=prompter)

    assert code == 1
    assert 'version = "1.0.0"' in (tmp_path / "server.toml").read_text()
    assert "rolled back 1 pin to the previous version" in capsys.readouterr().out


def test_pin_bare_target_asks_what_to_pin(tmp_path: Path) -> None:
    api = FakeAPI()
    add_paper_routes(api)
    jar = make_plugin_jar_bytes(name="Good11")
    api.add_json(
        hangar_versions_url("Good", platform_version="26.1.2", channel=None, limit=30),
        hangar_version_list_payload([hangar_version_list_item(name="1.1.0")]),
    )
    add_good_download_routes(api, "1.1.0", jar)
    prompter = ScriptedPrompter(select=["good", "1.1.0"])

    code = run_pin(
        tmp_path, api, base_config(GOOD_PLUGIN), argv=["pin", "--interactive"], prompter=prompter
    )

    assert code == 0
    first_kind, first_message = prompter.calls[0]
    assert (first_kind, first_message) == ("select", "What to pin")
    assert prompter.choices["What to pin"] == ["core", "good"]


def test_pin_core_non_interactive_with_version_and_build(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_json(FILL_PROJECTS_URL, fill_project_payload({"26.1": ["26.1.2"]}))
    jar = make_paper_jar_bytes(main_class="io.papermc.paper.NewMain")
    add_fill_builds(api, "26.1.2", [(57, jar)])

    code = run_pin(
        tmp_path,
        api,
        base_config("", paper_version="26.1.1"),
        argv=["pin", "core", "--version", "26.1.2", "--build", "57"],
    )

    assert code == 0
    config_text = (tmp_path / "server.toml").read_text()
    assert 'version = "26.1.2"' in config_text
    assert "build = 57" in config_text
    assert (tmp_path / "artifacts" / "paper.jar").read_bytes() == jar


def test_pin_plugin_non_interactive_with_version(tmp_path: Path) -> None:
    api = FakeAPI()
    add_paper_routes(api)
    jar = make_plugin_jar_bytes(name="Good11")
    add_good_download_routes(api, "1.1.0", jar)

    code = run_pin(
        tmp_path,
        api,
        base_config(GOOD_PLUGIN),
        argv=["pin", "good", "--version", "1.1.0"],
    )

    assert code == 0
    assert 'version = "1.1.0"' in (tmp_path / "server.toml").read_text()
    assert (tmp_path / "artifacts" / "plugins" / "good.jar").read_bytes() == jar


def test_pin_non_interactive_requires_target_and_version(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    text = base_config(GOOD_PLUGIN)

    assert run_pin(tmp_path, api, text, argv=["pin"]) == 1
    assert "needs a target" in capsys.readouterr().err
    assert run_pin(tmp_path, api, text, argv=["pin", "good"]) == 1
    assert "needs --version" in capsys.readouterr().err
    assert api.requests == []


def test_pin_interactive_rejects_exact_value_flags(tmp_path: Path, capsys) -> None:
    api = FakeAPI()

    code = run_pin(
        tmp_path,
        api,
        base_config(GOOD_PLUGIN),
        argv=["pin", "good", "--interactive", "--version", "1.1.0"],
        prompter=ScriptedPrompter(),
    )

    assert code == 1
    assert "cannot combine --interactive" in capsys.readouterr().err
    assert api.requests == []


def test_pin_non_interactive_rejects_cross_target_flags(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    text = base_config(GOOD_PLUGIN)

    assert (
        run_pin(
            tmp_path, api, text, argv=["pin", "core", "--version", "26.1.2", "--asset", "x.jar"]
        )
        == 1
    )
    assert "--asset only applies to plugins" in capsys.readouterr().err
    assert (
        run_pin(tmp_path, api, text, argv=["pin", "good", "--version", "1.1.0", "--build", "2"])
        == 1
    )
    assert "--build only applies to core" in capsys.readouterr().err
    assert api.requests == []


def test_pin_unknown_target_id(tmp_path: Path, capsys) -> None:
    api = FakeAPI()

    code = run_pin(tmp_path, api, base_config(GOOD_PLUGIN), argv=["pin", "nope"])

    assert code == 1
    assert "no plugin with id 'nope'" in capsys.readouterr().err
    assert api.requests == []


def test_pin_non_tty_refused(tmp_path: Path, capsys) -> None:
    api = FakeAPI()

    code = run_pin(tmp_path, api, base_config(GOOD_PLUGIN), argv=["pin", "good", "--interactive"])

    assert code == 1
    assert "interactive" in capsys.readouterr().err
    assert api.requests == []


def test_pin_plugin_no_compatible_versions(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    api.add_json(
        hangar_versions_url("Good", platform_version="26.1.2", channel=None, limit=30),
        hangar_version_list_payload([]),
    )
    prompter = ScriptedPrompter()

    code = run_pin(
        tmp_path,
        api,
        base_config(GOOD_PLUGIN),
        argv=["pin", "good", "--interactive"],
        prompter=prompter,
    )

    assert code == 1
    assert "no version for Minecraft 26.1.2" in capsys.readouterr().err
