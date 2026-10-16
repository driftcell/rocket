"""Offline tests for ZripsProvider (zrips.net download pages)."""

from __future__ import annotations

from pathlib import Path

from rocket.cli import main
from rocket.config import load_config
from rocket.zrips import parse_listing

from .helpers import (
    FakeAPI,
    ScriptedPrompter,
    add_paper_routes,
    base_config,
    make_plugin_jar_bytes,
    write_config,
    zrips_direct_download_url,
    zrips_file_manager_html,
    zrips_listing_url,
    zrips_manager_download_url,
    zrips_wordpress_html,
)

PROJECT = "DemoPlugin"
VERSIONS = ["3.1.0", "3.0.0", "2.9.0"]

PLUGIN = f"""
[plugins.demoplugin]
provider = "zrips"
project = "{PROJECT}"
version = "3.0.0"
"""


def run_main(tmp_path: Path, api: FakeAPI, text: str) -> int:
    config_path = write_config(tmp_path, text)
    return main(
        ["download", "--config", str(config_path)],
        transport=api.transport(),
        sleep=lambda _: None,
    )


def add_zrips_routes(
    api: FakeAPI,
    jar: bytes,
    *,
    layout: str = "manager",
    versions: list[str] | None = None,
) -> None:
    """Register the zrips.net listing page plus the pinned version's JAR bytes."""
    if versions is None:
        versions = VERSIONS
    if layout == "manager":
        api.add_html(zrips_listing_url(PROJECT), zrips_file_manager_html(PROJECT, versions))
        api.add_bytes(zrips_manager_download_url(PROJECT, "3.0.0"), jar)
    else:
        api.add_html(zrips_listing_url(PROJECT), zrips_wordpress_html(PROJECT, versions))
        api.add_bytes(zrips_direct_download_url(PROJECT, "3.0.0"), jar)


def test_zrips_download_success_file_manager(tmp_path: Path, capsys) -> None:
    jar = make_plugin_jar_bytes(name="DemoPlugin")
    api = FakeAPI()
    add_paper_routes(api)
    add_zrips_routes(api, jar)

    assert run_main(tmp_path, api, base_config(PLUGIN)) == 0
    target = tmp_path / "artifacts" / "plugins" / "demoplugin.jar"
    assert target.read_bytes() == jar
    captured = capsys.readouterr()
    assert " + demoplugin.jar (3.0.0, zrips)" in captured.out
    assert "Downloaded 2 items" in captured.out
    assert "no Minecraft compatibility info" in captured.err


def test_zrips_download_success_wordpress_layout(tmp_path: Path) -> None:
    jar = make_plugin_jar_bytes(name="DemoPlugin")
    api = FakeAPI()
    add_paper_routes(api)
    add_zrips_routes(api, jar, layout="wordpress")

    assert run_main(tmp_path, api, base_config(PLUGIN)) == 0
    assert (tmp_path / "artifacts" / "plugins" / "demoplugin.jar").read_bytes() == jar


def test_zrips_redownloads_without_upstream_hash(tmp_path: Path) -> None:
    """zrips.net provides no checksum: the lock is reused, but the file is re-downloaded."""
    jar = make_plugin_jar_bytes(name="DemoPlugin")
    api = FakeAPI()
    add_paper_routes(api)
    add_zrips_routes(api, jar)
    text = base_config(PLUGIN)

    assert run_main(tmp_path, api, text) == 0
    assert run_main(tmp_path, api, text) == 0

    assert api.count(zrips_listing_url(PROJECT)) == 1  # locked after the first run
    assert api.count(zrips_manager_download_url(PROJECT, "3.0.0")) == 2


def test_zrips_exact_version_required(tmp_path: Path, capsys) -> None:
    jar = make_plugin_jar_bytes()
    api = FakeAPI()
    add_paper_routes(api)
    add_zrips_routes(api, jar)
    plugin = PLUGIN.replace('version = "3.0.0"', 'version = "9.9.9"')

    assert run_main(tmp_path, api, base_config(plugin)) == 1
    err = capsys.readouterr().err
    assert "has no version '9.9.9'" in err
    assert "3.1.0" in err  # the hint lists recent available versions
    assert api.count(zrips_manager_download_url(PROJECT, "3.0.0")) == 0


def test_zrips_page_without_jar_links_fails(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    add_paper_routes(api)
    api.add_html(
        zrips_listing_url(PROJECT),
        "<html><body><h1>DemoPlugin documentation</h1></body></html>",
    )

    assert run_main(tmp_path, api, base_config(PLUGIN)) == 1
    assert "no downloadable versions" in capsys.readouterr().err


def test_zrips_ignores_other_plugins_and_offsite_links(tmp_path: Path) -> None:
    jar = make_plugin_jar_bytes(name="DemoPlugin")
    api = FakeAPI()
    add_paper_routes(api)
    html = (
        "<html><body>"
        '<a href="/Other/Other1.0.0.jar">Other plugin</a>'
        f'<a href="download.php?file={PROJECT}3.0.0.jar">[01/02/2026 10:00:00] {PROJECT} 3.0.0</a>'
        f'<a href="https://evil.example.com/{PROJECT}9.9.9.jar">lookalike</a>'
        "</body></html>"
    )
    api.add_html(zrips_listing_url(PROJECT), html)
    api.add_bytes(zrips_manager_download_url(PROJECT, "3.0.0"), jar)

    assert run_main(tmp_path, api, base_config(PLUGIN)) == 0
    assert (tmp_path / "artifacts" / "plugins" / "demoplugin.jar").read_bytes() == jar


def test_zrips_project_not_found(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    add_paper_routes(api)
    api.add_html(zrips_listing_url(PROJECT), "not found", status=404)

    assert run_main(tmp_path, api, base_config(PLUGIN)) == 1
    assert "zrips.net project not found" in capsys.readouterr().err


def test_zrips_invalid_project_coordinate_fails(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    add_paper_routes(api)
    plugin = PLUGIN.replace(f'project = "{PROJECT}"', 'project = "Zrips/DemoPlugin"')

    assert run_main(tmp_path, api, base_config(plugin)) == 1
    assert "not a valid zrips.net project" in capsys.readouterr().err
    assert all("zrips.net" not in str(request.url) for request in api.requests)


def test_zrips_asset_mismatch_rejected(tmp_path: Path, capsys) -> None:
    """The listing publishes the upstream filename, so a declared asset is verified."""
    jar = make_plugin_jar_bytes()
    api = FakeAPI()
    add_paper_routes(api)
    add_zrips_routes(api, jar)
    plugin = PLUGIN + 'asset = "SomethingElse-3.0.0.jar"\n'

    assert run_main(tmp_path, api, base_config(plugin)) == 1
    assert "does not match the upstream filename" in capsys.readouterr().err
    assert api.count(zrips_manager_download_url(PROJECT, "3.0.0")) == 0


def test_zrips_add_auto_pins_the_newest_listing_entry(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_html(zrips_listing_url(PROJECT), zrips_file_manager_html(PROJECT, VERSIONS))

    config_path = write_config(tmp_path, base_config())
    code = main(
        ["add", f"zrips:{PROJECT}", "--config", str(config_path)],
        transport=api.transport(),
        sleep=lambda _: None,
    )

    assert code == 0
    config = load_config(config_path)
    plugin = config.plugins[0]
    assert plugin.provider == "zrips"
    assert plugin.project == PROJECT
    assert plugin.version == "3.1.0"
    assert plugin.asset is None


def test_zrips_recommend_rejects_a_mismatched_asset_template(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    api.add_html(zrips_listing_url(PROJECT), zrips_file_manager_html(PROJECT, VERSIONS))

    config_path = write_config(tmp_path, base_config())
    code = main(
        [
            "add",
            f"zrips:{PROJECT}",
            "--asset",
            "OtherName-{version}.jar",
            "--config",
            str(config_path),
        ],
        transport=api.transport(),
        sleep=lambda _: None,
    )

    assert code == 1
    assert "does not match the zrips.net filename" in capsys.readouterr().err
    assert "[plugins.demoplugin]" not in config_path.read_text(encoding="utf-8")


def test_zrips_parse_listing_decodes_dedupes_and_extracts_details() -> None:
    html = (
        "<html><body>"
        # file-manager entry: the upload date is the detail
        f'<a href="download.php?file={PROJECT}3.1.0.jar">'
        f'[02/01/2026 10:11:12] {PROJECT} 3.1.0 <span class="download-count"> 5</span></a>'
        # a duplicate version keeps the first occurrence
        f'<a href="download.php?file={PROJECT}3.1.0.jar">[01/01/2026 09:00:00] dup</a>'
        # URL-encoded characters in the filename belong to the version
        f'<a href="download.php?file={PROJECT}3.0.0Beta%281.12%29.jar">'
        f"[12/01/2025 08:00:00] {PROJECT} 3.0.0Beta(1.12)</a>"
        # WordPress entry: the trailing note is the detail
        f'<a href="/{PROJECT}/{PROJECT}2.9.0.jar"> {PROJECT}2.9.0.jar 26.3 support</a>'
        "</body></html>"
    )

    files = parse_listing(html, project=PROJECT, page_url=zrips_listing_url(PROJECT))

    assert [file.version for file in files] == ["3.1.0", "3.0.0Beta(1.12)", "2.9.0"]
    assert files[0].detail == "02/01/2026"
    assert files[0].url == zrips_manager_download_url(PROJECT, "3.1.0")
    assert files[1].filename == f"{PROJECT}3.0.0Beta(1.12).jar"
    assert files[1].url.endswith("download.php?file=DemoPlugin3.0.0Beta%281.12%29.jar")
    assert files[2].detail == "26.3 support"
    assert files[2].url == zrips_direct_download_url(PROJECT, "2.9.0")


def test_zrips_pin_downgrade_pins_no_asset(tmp_path: Path) -> None:
    api = FakeAPI()
    add_paper_routes(api)
    jar = make_plugin_jar_bytes(name="DemoPlugin290")
    api.add_html(zrips_listing_url(PROJECT), zrips_file_manager_html(PROJECT, VERSIONS))
    api.add_bytes(zrips_manager_download_url(PROJECT, "2.9.0"), jar)
    plugin = PLUGIN.replace('version = "3.0.0"', 'version = "3.1.0"')
    config_path = write_config(tmp_path, base_config(plugin))
    prompter = ScriptedPrompter(select=["2.9.0"])

    code = main(
        ["pin", "demoplugin", "--config", str(config_path)],
        transport=api.transport(),
        sleep=lambda _: None,
        prompter=prompter,
    )

    assert code == 0
    config_text = config_path.read_text(encoding="utf-8")
    assert 'version = "2.9.0"' in config_text
    assert "asset" not in config_text
    assert (tmp_path / "artifacts" / "plugins" / "demoplugin.jar").read_bytes() == jar
    # one file per version: no file prompt follows the version pick
    assert [kind for kind, _ in prompter.calls] == ["select"]
    choices = prompter.choices["demoplugin version (current: 3.1.0)"]
    assert choices == ["3.1.0", "3.0.0", "2.9.0"]
