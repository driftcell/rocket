"""Offline tests for ``rocket upgrade``: interactive version bumps with rollback."""

from __future__ import annotations

from pathlib import Path

from rocket.cli import main
from rocket.recommend import asset_template

from .helpers import (
    FILL_PROJECTS_URL,
    FakeAPI,
    ScriptedPrompter,
    add_paper_routes,
    base_config,
    fill_build_payload,
    fill_builds_url,
    fill_project_payload,
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
    write_config,
)

GOOD_1_1_URL = "https://hangarcdn.papermc.io/plugins/Owner/Good/versions/1.1.0/PAPER/Good-1.1.0.jar"
BAD_2_0_URL = "https://hangarcdn.papermc.io/plugins/Owner/Bad/versions/2.0.0/PAPER/Bad-2.0.0.jar"
COREPROTECT_24_1_URL = (
    "https://cdn.modrinth.com/data/coreprotect/versions/24.1/CoreProtect-CE-24.1.jar"
)

GOOD_PLUGIN = '[plugins.good]\nprovider = "hangar"\nproject = "Owner/Good"\nversion = "1.0.0"\n'
COREPROTECT_PLUGIN = (
    '[plugins.coreprotect]\nprovider = "modrinth"\nproject = "coreprotect"\n'
    'version = "23.2"\nasset = "CoreProtect-CE-23.2.jar"\n'
)


def run_upgrade(
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


def add_good_update_routes(api: FakeAPI, jar: bytes, *, mc: str = "26.1.2") -> None:
    """Routes for checking and downloading hangar Owner/Good 1.1.0."""
    api.add_json(
        hangar_versions_url("Good", platform_version=mc),
        hangar_version_list_payload([hangar_version_list_item(name="1.1.0")]),
    )
    api.add_json(
        hangar_version_url("Good", "1.1.0"),
        hangar_version_payload(
            name="1.1.0",
            downloads=hangar_native_paper(
                download_url=GOOD_1_1_URL,
                name="Good-1.1.0.jar",
                sha256=sha256_hex(jar),
                size=len(jar),
            ),
            platform_dependencies={"PAPER": [mc]},
        ),
    )
    api.add_json(hangar_project_url("Good"), hangar_project_payload(owner="Owner", slug="Good"))
    api.add_bytes(GOOD_1_1_URL, jar)


def add_coreprotect_update_routes(api: FakeAPI, jar: bytes) -> None:
    """Routes for checking and downloading modrinth coreprotect 24.1."""
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
                        filename="CoreProtect-CE-24.1.jar",
                        url=COREPROTECT_24_1_URL,
                        size=len(jar),
                        sha512=sha512_hex(jar),
                    )
                ],
            )
        ],
    )
    api.add_bytes(COREPROTECT_24_1_URL, jar)


def test_upgrade_plugins_interactive_bumps_version(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    add_paper_routes(api)
    jar = make_plugin_jar_bytes(name="Good11")
    add_good_update_routes(api, jar)
    prompter = ScriptedPrompter(checkbox=None)  # None accepts every preselected entry

    code = run_upgrade(
        tmp_path, api, base_config(GOOD_PLUGIN), argv=["upgrade", "plugins"], prompter=prompter
    )

    assert code == 0
    config_text = (tmp_path / "server.toml").read_text()
    assert 'version = "1.1.0"' in config_text
    assert (tmp_path / "artifacts" / "plugins" / "good.jar").read_bytes() == jar
    out = capsys.readouterr().out
    assert "Checking updates for Paper 26.1.2 + 1 plugin..." in out
    assert " + good.jar (1.1.0, hangar)" in out
    assert ("checkbox", "Plugins to upgrade") in prompter.calls
    assert not any(kind == "select" for kind, _ in prompter.calls)


def test_upgrade_plugins_modrinth_asset_template(tmp_path: Path, capsys) -> None:
    """The pinned asset is turned into a {version} template before matching new files."""
    api = FakeAPI()
    add_paper_routes(api)
    jar = make_plugin_jar_bytes(name="CoreProtect241")
    add_coreprotect_update_routes(api, jar)

    code = run_upgrade(
        tmp_path,
        api,
        base_config(COREPROTECT_PLUGIN),
        argv=["upgrade", "plugins", "--force"],
    )

    assert code == 0
    config_text = (tmp_path / "server.toml").read_text()
    assert 'version = "24.1"' in config_text
    assert 'asset = "CoreProtect-CE-24.1.jar"' in config_text
    assert (tmp_path / "artifacts" / "plugins" / "coreprotect.jar").read_bytes() == jar


def test_upgrade_nothing_to_do(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    jar = make_plugin_jar_bytes(name="Good11")
    # the pin already matches the recommendation; no download routes are registered at all
    api.add_json(
        hangar_versions_url("Good", platform_version="26.1.2"),
        hangar_version_list_payload([hangar_version_list_item(name="1.1.0")]),
    )
    api.add_json(
        hangar_version_url("Good", "1.1.0"),
        hangar_version_payload(
            name="1.1.0",
            downloads=hangar_native_paper(
                download_url=GOOD_1_1_URL,
                name="Good-1.1.0.jar",
                sha256=sha256_hex(jar),
                size=len(jar),
            ),
            platform_dependencies={"PAPER": ["26.1.2"]},
        ),
    )
    text = base_config(GOOD_PLUGIN.replace('"1.0.0"', '"1.1.0"'))

    code = run_upgrade(tmp_path, api, text, argv=["upgrade", "plugins", "--force"])

    assert code == 0
    assert "everything is up to date" in capsys.readouterr().out
    assert (tmp_path / "server.toml").read_text() == text


def test_upgrade_github_entry_is_noted_and_skipped(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    text = base_config(
        '[plugins.vault]\nprovider = "github"\nproject = "MilkBowl/Vault"\nversion = "1.7.3"\n'
    )

    code = run_upgrade(tmp_path, api, text, argv=["upgrade", "plugins", "--force"])

    assert code == 0
    out = capsys.readouterr().out
    assert "note: vault (github) pins an exact tag" in out
    assert "everything is up to date" in out
    assert api.requests == []


def test_upgrade_rolls_back_failed_pins(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    add_paper_routes(api)
    good_jar = make_plugin_jar_bytes(name="Good11")
    add_good_update_routes(api, good_jar)
    bad_jar = make_plugin_jar_bytes(name="Bad20")
    api.add_json(
        hangar_versions_url("Bad", platform_version="26.1.2"),
        hangar_version_list_payload([hangar_version_list_item(name="2.0.0")]),
    )
    api.add_json(
        hangar_version_url("Bad", "2.0.0"),
        hangar_version_payload(
            name="2.0.0",
            downloads=hangar_native_paper(
                download_url=BAD_2_0_URL,
                name="Bad-2.0.0.jar",
                sha256=sha256_hex(b"different-bytes"),  # hash mismatch on download
                size=len(b"different-bytes"),
            ),
            platform_dependencies={"PAPER": ["26.1.2"]},
        ),
    )
    api.add_json(hangar_project_url("Bad"), hangar_project_payload(owner="Owner", slug="Bad"))
    api.add_bytes(BAD_2_0_URL, bad_jar)
    text = base_config(
        GOOD_PLUGIN
        + '\n[plugins.bad]\nprovider = "hangar"\nproject = "Owner/Bad"\nversion = "1.0.0"\n'
    )

    code = run_upgrade(tmp_path, api, text, argv=["upgrade", "plugins", "--force"])

    assert code == 1
    config_text = (tmp_path / "server.toml").read_text()
    assert 'version = "1.1.0"' in config_text  # the successful bump stays
    assert config_text.count('version = "1.0.0"') == 1  # the failed one was rolled back
    assert "rolled back 1 pin to the previous version" in capsys.readouterr().out
    assert (tmp_path / "artifacts" / "plugins" / "good.jar").read_bytes() == good_jar
    assert not (tmp_path / "artifacts" / "plugins" / "bad.jar").exists()


def test_upgrade_check_failure_exits_1(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    api.add_json(hangar_versions_url("Good", platform_version="26.1.2"), {"error": "x"}, status=500)

    code = run_upgrade(
        tmp_path, api, base_config(GOOD_PLUGIN), argv=["upgrade", "plugins", "--force"]
    )

    assert code == 1
    captured = capsys.readouterr()
    assert "error: good:" in captured.err
    assert "everything is up to date" not in captured.out


def test_upgrade_modrinth_v_prefixed_tag_appears_differently_in_filenames(
    tmp_path: Path,
) -> None:
    """Pinned tag 'v5.5.60' spells '5.5.60' in filenames; the new file is still matched."""
    api = FakeAPI()
    add_paper_routes(api)
    jar = make_plugin_jar_bytes(name="LuckPerms5571")
    url = "https://cdn.modrinth.com/data/luckperms/versions/v5.5.71-bukkit/LuckPerms-Bukkit-5.5.71.jar"
    api.add_json(modrinth_project_url("luckperms"), modrinth_project_payload(slug="luckperms"))
    api.add_json(
        modrinth_versions_url("luckperms"),
        [
            modrinth_version_payload(
                version_number="v5.5.71-bukkit",
                game_versions=["26.1.2"],
                loaders=["paper"],
                files=[
                    modrinth_file_payload(
                        filename="LuckPerms-Bukkit-5.5.71.jar",
                        url=url,
                        size=len(jar),
                        sha512=sha512_hex(jar),
                    )
                ],
            )
        ],
    )
    api.add_bytes(url, jar)
    text = base_config(
        '[plugins.luckperms]\nprovider = "modrinth"\nproject = "luckperms"\n'
        'version = "v5.5.60"\nasset = "LuckPerms-Bukkit-5.5.60.jar"\n'
    )

    code = run_upgrade(tmp_path, api, text, argv=["upgrade", "plugins", "--force"])

    assert code == 0
    config_text = (tmp_path / "server.toml").read_text()
    assert 'version = "v5.5.71-bukkit"' in config_text
    assert 'asset = "LuckPerms-Bukkit-5.5.71.jar"' in config_text


def test_upgrade_modrinth_ambiguous_files_report_a_check_failure(tmp_path: Path, capsys) -> None:
    """A template that matches nothing falls back to a unique JAR — and fails loudly when
    the new version offers several."""
    api = FakeAPI()
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
                        size=10,
                        sha512=sha512_hex(b"a"),
                    ),
                    modrinth_file_payload(
                        filename="CoreProtect-CE-24.1-b.jar",
                        url="https://cdn.modrinth.com/data/coreprotect/versions/24.1/b.jar",
                        size=10,
                        sha512=sha512_hex(b"b"),
                    ),
                ],
            )
        ],
    )
    text = base_config(COREPROTECT_PLUGIN)

    code = run_upgrade(tmp_path, api, text, argv=["upgrade", "plugins", "--force"])

    assert code == 1
    assert "multiple JAR files" in capsys.readouterr().err
    assert (tmp_path / "server.toml").read_text() == text


def test_asset_template_recovers_every_filename_form() -> None:
    assert asset_template("Good-1.0.0.jar", "1.0.0") == "Good-{version}.jar"
    assert asset_template("LuckPerms-Bukkit-5.5.60.jar", "v5.5.60") == (
        "LuckPerms-Bukkit-{version}.jar"
    )
    assert asset_template("LuckPerms-Bukkit-5.5.60.jar", "v5.5.60-bukkit") == (
        "LuckPerms-Bukkit-{version}.jar"
    )
    assert asset_template("plugin.jar", "1.0.0") == "plugin.jar"


def test_upgrade_non_tty_without_force_fails(tmp_path: Path, capsys) -> None:
    api = FakeAPI()

    code = run_upgrade(tmp_path, api, base_config(GOOD_PLUGIN), argv=["upgrade", "plugins"])

    assert code == 1
    assert "interactive" in capsys.readouterr().err
    assert api.requests == []


# ---------------------------------------------------------------------------
# Core upgrades
# ---------------------------------------------------------------------------


def add_fill_builds(api: FakeAPI, version: str, builds: list[tuple[int, bytes]]) -> None:
    api.add_json(
        fill_builds_url(version),
        [
            fill_build_payload(
                build_id=build_id,
                channel="STABLE",
                url=f"https://fill-data.papermc.io/v1/objects/{sha256_hex(jar)}/paper-{version}-{build_id}.jar",
                name=f"paper-{version}-{build_id}.jar",
                sha256=sha256_hex(jar),
                size=len(jar),
            )
            for build_id, jar in builds
        ],
    )
    for build_id, jar in builds:
        api.add_bytes(
            f"https://fill-data.papermc.io/v1/objects/{sha256_hex(jar)}/paper-{version}-{build_id}.jar",
            jar,
        )


def test_upgrade_core_patch_is_the_default_choice(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    api.add_json(
        FILL_PROJECTS_URL,
        fill_project_payload({"26.2": ["26.2.0"], "26.1": ["26.1.2", "26.1.1"]}),
    )
    old_build_jar = make_paper_jar_bytes()
    add_fill_builds(api, "26.1.1", [(5, old_build_jar)])
    new_jar = make_paper_jar_bytes(main_class="io.papermc.paper.NewMain")
    add_fill_builds(api, "26.1.2", [(9, new_jar)])
    text = base_config("", paper_version="26.1.1").replace(
        'version = "26.1.1"', 'version = "26.1.1"\nbuild = 5'
    )
    prompter = ScriptedPrompter(select=["0"])  # default: the patch, not the major jump

    code = run_upgrade(tmp_path, api, text, argv=["upgrade", "core"], prompter=prompter)

    assert code == 0
    config_text = (tmp_path / "server.toml").read_text()
    assert 'version = "26.1.2"' in config_text
    assert "build" not in config_text  # a build pin never carries over to a new release
    assert (tmp_path / "artifacts" / "paper.jar").read_bytes() == new_jar
    out = capsys.readouterr().out
    assert "next: rocket upgrade plugins" in out
    choices = prompter.choices["Paper core (current: 26.1.1)"]
    assert choices == ["0", "1", "keep"]


def test_upgrade_core_force_takes_the_major(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_json(
        FILL_PROJECTS_URL,
        fill_project_payload({"26.2": ["26.2.0"], "26.1": ["26.1.2", "26.1.1"]}),
    )
    new_jar = make_paper_jar_bytes(main_class="io.papermc.paper.NewMain")
    add_fill_builds(api, "26.2.0", [(3, new_jar)])
    text = base_config("", paper_version="26.1.1")

    code = run_upgrade(tmp_path, api, text, argv=["upgrade", "core", "--force"])

    assert code == 0
    assert 'version = "26.2.0"' in (tmp_path / "server.toml").read_text()
    assert (tmp_path / "artifacts" / "paper.jar").read_bytes() == new_jar


def test_upgrade_core_keep_leaves_everything(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    api.add_json(
        FILL_PROJECTS_URL,
        fill_project_payload({"26.2": ["26.2.0"], "26.1": ["26.1.2", "26.1.1"]}),
    )
    text = base_config("", paper_version="26.1.1")
    prompter = ScriptedPrompter(select=["keep"])

    code = run_upgrade(tmp_path, api, text, argv=["upgrade", "core"], prompter=prompter)

    assert code == 0
    assert "everything is up to date" in capsys.readouterr().out
    assert (tmp_path / "server.toml").read_text() == text


def test_upgrade_core_build_bump_unpinned_only_refreshes_the_lock(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    old_jar = make_paper_jar_bytes()
    api.add_json(FILL_PROJECTS_URL, fill_project_payload({"26.1": ["26.1.2"]}))
    add_fill_builds(api, "26.1.2", [(42, old_jar)])
    text = base_config("", paper_version="26.1.2")
    assert (
        main(
            ["download", "--config", str(write_config(tmp_path, text))],
            transport=api.transport(),
            sleep=lambda _: None,
        )
        == 0
    )
    new_jar = make_paper_jar_bytes(main_class="io.papermc.paper.NewMain")
    add_fill_builds(api, "26.1.2", [(42, old_jar), (57, new_jar)])
    prompter = ScriptedPrompter(select=["0"])

    code = run_upgrade(tmp_path, api, text, argv=["upgrade", "core"], prompter=prompter)

    assert code == 0
    assert (tmp_path / "server.toml").read_text() == text  # unpinned: no config rewrite at all
    assert (tmp_path / "artifacts" / "paper.jar").read_bytes() == new_jar
    assert " + paper.jar (26.1.2 build 57, fill)" in capsys.readouterr().out


def test_upgrade_core_build_bump_pinned_rewrites_the_pin(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_json(FILL_PROJECTS_URL, fill_project_payload({"26.1": ["26.1.2"]}))
    old_jar = make_paper_jar_bytes()
    new_jar = make_paper_jar_bytes(main_class="io.papermc.paper.NewMain")
    add_fill_builds(api, "26.1.2", [(42, old_jar), (57, new_jar)])
    text = base_config("", paper_version="26.1.2").replace(
        'version = "26.1.2"', 'version = "26.1.2"\nbuild = 42'
    )

    code = run_upgrade(tmp_path, api, text, argv=["upgrade", "core", "--force"])

    assert code == 0
    config_text = (tmp_path / "server.toml").read_text()
    assert "build = 57" in config_text
    assert (tmp_path / "artifacts" / "paper.jar").read_bytes() == new_jar


def test_upgrade_core_already_up_to_date(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    api.add_json(FILL_PROJECTS_URL, fill_project_payload({"26.2": ["26.2.0"]}))
    text = base_config("", paper_version="26.2.0")

    code = run_upgrade(tmp_path, api, text, argv=["upgrade", "core", "--force"])

    assert code == 0
    assert "everything is up to date" in capsys.readouterr().out


def test_upgrade_all_cascades_plugin_checks_to_the_new_core(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    api.add_json(
        FILL_PROJECTS_URL,
        fill_project_payload({"26.2": ["26.2.0"], "26.1": ["26.1.2", "26.1.1"]}),
    )
    new_paper_jar = make_paper_jar_bytes(main_class="io.papermc.paper.NewMain")
    add_fill_builds(api, "26.1.2", [(9, new_paper_jar)])
    good_jar = make_plugin_jar_bytes(name="Good11")
    add_good_update_routes(api, good_jar, mc="26.1.2")  # only the NEW core version is registered
    prompter = ScriptedPrompter(select=["0"], checkbox=None)

    code = run_upgrade(
        tmp_path,
        api,
        base_config(GOOD_PLUGIN, paper_version="26.1.1"),
        argv=["upgrade"],
        prompter=prompter,
    )

    assert code == 0
    config_text = (tmp_path / "server.toml").read_text()
    assert 'version = "26.1.2"' in config_text
    assert 'version = "1.1.0"' in config_text
    assert "next: rocket upgrade plugins" not in capsys.readouterr().out
