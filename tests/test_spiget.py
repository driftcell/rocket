"""Offline tests for SpigetProvider."""

from __future__ import annotations

from pathlib import Path

from rocket.cli import main

from .helpers import (
    FakeAPI,
    add_paper_routes,
    base_config,
    make_plugin_jar_bytes,
    spiget_download_url,
    spiget_resource_payload,
    spiget_resource_url,
    spiget_version_payload,
    spiget_versions_url,
    write_config,
)

RESOURCE_ID = "28140"
VERSION_ID = 648014

PLUGIN = f"""
[plugins.luckperms]
provider = "spiget"
project = "{RESOURCE_ID}"
version = "5.5.71"
"""


def run_main(tmp_path: Path, api: FakeAPI, text: str) -> int:
    config_path = write_config(tmp_path, text)
    return main(
        ["download", "--config", str(config_path)],
        transport=api.transport(),
        sleep=lambda _: None,
    )


def add_spiget_routes(
    api: FakeAPI,
    jar: bytes,
    *,
    versions: list[dict[str, object]] | None = None,
    tested_versions: list[str] | None = None,
    premium: bool = False,
    external: bool = False,
) -> None:
    """Register the Spiget chain for luckperms 5.5.71 (resource is tested on the 26.1 line)."""
    if tested_versions is None:
        tested_versions = ["1.21", "26.1"]
    api.add_json(
        spiget_resource_url(RESOURCE_ID),
        spiget_resource_payload(
            resource_id=int(RESOURCE_ID),
            name="LuckPerms",
            tested_versions=tested_versions,
            premium=premium,
            external=external,
        ),
    )
    if versions is None:
        versions = [
            spiget_version_payload(version_id=VERSION_ID, name="5.5.71", release_date=1786045114),
            spiget_version_payload(version_id=590885, name="5.5.0", release_date=1748247660),
        ]
    api.add_json(spiget_versions_url(RESOURCE_ID), versions)
    api.add_bytes(spiget_download_url(RESOURCE_ID, VERSION_ID), jar)


def test_spiget_download_success(tmp_path: Path, capsys) -> None:
    jar = make_plugin_jar_bytes(name="LuckPerms")
    api = FakeAPI()
    add_paper_routes(api)
    add_spiget_routes(api, jar)

    assert run_main(tmp_path, api, base_config(PLUGIN)) == 0
    target = tmp_path / "artifacts" / "plugins" / "luckperms.jar"
    assert target.read_bytes() == jar
    out = capsys.readouterr().out
    assert " + luckperms.jar (5.5.71, spiget)" in out
    assert "Downloaded 2 items" in out


def test_spiget_redownloads_without_upstream_hash(tmp_path: Path) -> None:
    """Spiget provides no checksum: the lock is reused, but the file is re-downloaded."""
    jar = make_plugin_jar_bytes(name="LuckPerms")
    api = FakeAPI()
    add_paper_routes(api)
    add_spiget_routes(api, jar)
    text = base_config(PLUGIN)

    assert run_main(tmp_path, api, text) == 0
    assert run_main(tmp_path, api, text) == 0

    assert api.count(spiget_resource_url(RESOURCE_ID)) == 1  # locked after the first run
    assert api.count(spiget_versions_url(RESOURCE_ID)) == 1
    assert api.count(spiget_download_url(RESOURCE_ID, VERSION_ID)) == 2


def test_spiget_exact_version_required(tmp_path: Path, capsys) -> None:
    jar = make_plugin_jar_bytes()
    api = FakeAPI()
    add_paper_routes(api)
    add_spiget_routes(api, jar)
    plugin = PLUGIN.replace('version = "5.5.71"', 'version = "9.9.9"')

    assert run_main(tmp_path, api, base_config(plugin)) == 1
    err = capsys.readouterr().err
    assert "has no version '9.9.9'" in err
    assert "5.5.71" in err  # the hint lists recent available versions
    assert api.count(spiget_download_url(RESOURCE_ID, VERSION_ID)) == 0


def test_spiget_duplicate_version_names_cannot_decide(tmp_path: Path, capsys) -> None:
    jar = make_plugin_jar_bytes()
    api = FakeAPI()
    add_paper_routes(api)
    add_spiget_routes(
        api,
        jar,
        versions=[
            spiget_version_payload(version_id=VERSION_ID, name="5.5.71"),
            spiget_version_payload(version_id=648000, name="5.5.71"),
        ],
    )

    assert run_main(tmp_path, api, base_config(PLUGIN)) == 1
    err = capsys.readouterr().err
    assert "multiple versions named '5.5.71'" in err
    assert "cannot decide" in err


def test_spiget_premium_resource_rejected_before_download(tmp_path: Path, capsys) -> None:
    jar = make_plugin_jar_bytes()
    api = FakeAPI()
    add_paper_routes(api)
    add_spiget_routes(api, jar, premium=True)

    assert run_main(tmp_path, api, base_config(PLUGIN)) == 1
    assert "is premium" in capsys.readouterr().err
    assert api.count(spiget_versions_url(RESOURCE_ID)) == 0
    assert api.count(spiget_download_url(RESOURCE_ID, VERSION_ID)) == 0


def test_spiget_external_resource_rejected_before_download(tmp_path: Path, capsys) -> None:
    jar = make_plugin_jar_bytes()
    api = FakeAPI()
    add_paper_routes(api)
    add_spiget_routes(api, jar, external=True)

    assert run_main(tmp_path, api, base_config(PLUGIN)) == 1
    assert "hosted externally" in capsys.readouterr().err
    assert api.count(spiget_download_url(RESOURCE_ID, VERSION_ID)) == 0


def test_spiget_resource_not_found(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    add_paper_routes(api)
    api.add_json(spiget_resource_url(RESOURCE_ID), {"error": "not found"}, status=404)

    assert run_main(tmp_path, api, base_config(PLUGIN)) == 1
    assert "Spigot resource not found" in capsys.readouterr().err


def test_spiget_resource_id_mismatch_rejected(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    add_paper_routes(api)
    api.add_json(
        spiget_resource_url(RESOURCE_ID),
        spiget_resource_payload(resource_id=99999, name="Other", tested_versions=["26.1"]),
    )

    assert run_main(tmp_path, api, base_config(PLUGIN)) == 1
    assert "resource mismatch" in capsys.readouterr().err
    assert api.count(spiget_download_url(RESOURCE_ID, VERSION_ID)) == 0


def test_spiget_tested_versions_mismatch_rejected(tmp_path: Path, capsys) -> None:
    jar = make_plugin_jar_bytes()
    api = FakeAPI()
    add_paper_routes(api)
    add_spiget_routes(api, jar, tested_versions=["1.12", "1.13"])

    assert run_main(tmp_path, api, base_config(PLUGIN)) == 1
    err = capsys.readouterr().err
    assert "declared tested versions do not include Minecraft 26.1.2" in err
    assert "1.12" in err
    assert api.count(spiget_download_url(RESOURCE_ID, VERSION_ID)) == 0


def test_spiget_missing_tested_versions_warns_and_downloads(tmp_path: Path, capsys) -> None:
    jar = make_plugin_jar_bytes(name="LuckPerms")
    api = FakeAPI()
    add_paper_routes(api)
    add_spiget_routes(api, jar, tested_versions=[])

    assert run_main(tmp_path, api, base_config(PLUGIN)) == 0
    assert (tmp_path / "artifacts" / "plugins" / "luckperms.jar").read_bytes() == jar
    assert "no tested-version info" in capsys.readouterr().err


def test_spiget_asset_cannot_be_verified(tmp_path: Path, capsys) -> None:
    jar = make_plugin_jar_bytes()
    api = FakeAPI()
    add_paper_routes(api)
    add_spiget_routes(api, jar)
    plugin = PLUGIN + 'asset = "LuckPerms-5.5.71.jar"\n'

    assert run_main(tmp_path, api, base_config(plugin)) == 1
    assert "provides no upstream filename" in capsys.readouterr().err
    assert api.count(spiget_download_url(RESOURCE_ID, VERSION_ID)) == 0


def test_spiget_invalid_resource_id_fails(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    add_paper_routes(api)
    plugin = PLUGIN.replace(f'project = "{RESOURCE_ID}"', 'project = "luckperms"')

    assert run_main(tmp_path, api, base_config(plugin)) == 1
    assert "not a valid Spigot resource id" in capsys.readouterr().err


def test_spiget_version_found_on_a_later_page(tmp_path: Path) -> None:
    jar = make_plugin_jar_bytes(name="LuckPerms")
    api = FakeAPI()
    add_paper_routes(api)
    api.add_json(
        spiget_resource_url(RESOURCE_ID),
        spiget_resource_payload(
            resource_id=int(RESOURCE_ID), name="LuckPerms", tested_versions=["26.1"]
        ),
    )
    full_page = [
        spiget_version_payload(version_id=900000 + index, name=f"99.0.{index}")
        for index in range(100)
    ]
    api.add_json(spiget_versions_url(RESOURCE_ID, page=1), full_page)
    api.add_json(
        spiget_versions_url(RESOURCE_ID, page=2),
        [spiget_version_payload(version_id=VERSION_ID, name="5.5.71")],
    )
    api.add_bytes(spiget_download_url(RESOURCE_ID, VERSION_ID), jar)

    assert run_main(tmp_path, api, base_config(PLUGIN)) == 0
    assert api.count(spiget_versions_url(RESOURCE_ID, page=1)) == 1
    assert api.count(spiget_versions_url(RESOURCE_ID, page=2)) == 1
    assert (tmp_path / "artifacts" / "plugins" / "luckperms.jar").read_bytes() == jar


def test_spiget_shared_cache_between_entries(tmp_path: Path) -> None:
    jar = make_plugin_jar_bytes()
    api = FakeAPI()
    add_paper_routes(api)
    add_spiget_routes(api, jar)
    second = f"""
[plugins.luckperms_backup]
provider = "spiget"
project = "{RESOURCE_ID}"
version = "5.5.71"
"""

    assert run_main(tmp_path, api, base_config(PLUGIN + second)) == 0
    assert api.count(spiget_resource_url(RESOURCE_ID)) == 1
    assert api.count(spiget_versions_url(RESOURCE_ID)) == 1
    assert api.count(spiget_download_url(RESOURCE_ID, VERSION_ID)) == 2
    plugins_dir = tmp_path / "artifacts" / "plugins"
    assert (plugins_dir / "luckperms.jar").read_bytes() == jar
    assert (plugins_dir / "luckperms_backup.jar").read_bytes() == jar
