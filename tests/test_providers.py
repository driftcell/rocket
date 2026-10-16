"""Offline tests for HangarProvider and GitHubProvider."""

from __future__ import annotations

from pathlib import Path

from rocket.cli import main

from .helpers import (
    FakeAPI,
    add_paper_routes,
    base_config,
    github_asset_payload,
    github_release_api_url,
    github_release_payload,
    hangar_native_paper,
    hangar_project_payload,
    hangar_project_url,
    hangar_version_payload,
    hangar_version_url,
    make_plugin_jar_bytes,
    sha256_hex,
    write_config,
)

NATIVE_DOWNLOAD_URL = (
    "https://hangarcdn.papermc.io/plugins/Owner/Demo/versions/1.0.0/PAPER/Demo-1.0.0.jar"
)

HANGAR_PLUGIN = """
[plugins.demo]
provider = "hangar"
project = "Owner/Demo"
version = "1.0.0"
"""


def run_main(tmp_path: Path, api: FakeAPI, text: str) -> int:
    config_path = write_config(tmp_path, text)
    return main(
        ["download", "--config", str(config_path)], transport=api.transport(), sleep=lambda _: None
    )


def add_hangar_native_routes(
    api: FakeAPI,
    jar: bytes,
    *,
    version: str = "1.0.0",
    owner: str = "Owner",
    platform_dependencies: dict[str, list[str]] | None = None,
    plugin_dependencies: dict[str, object] | None = None,
) -> None:
    sha = sha256_hex(jar)
    api.add_json(hangar_project_url("Demo"), hangar_project_payload(owner=owner, slug="Demo"))
    api.add_json(
        hangar_version_url("Demo", version),
        hangar_version_payload(
            name=version,
            downloads=hangar_native_paper(
                download_url=NATIVE_DOWNLOAD_URL,
                name="Demo-1.0.0.jar",
                sha256=sha,
                size=len(jar),
            ),
            platform_dependencies=platform_dependencies,
            plugin_dependencies=plugin_dependencies,
        ),
    )
    api.add_bytes(NATIVE_DOWNLOAD_URL, jar)


def test_hangar_native_paper_download(tmp_path: Path, capsys) -> None:
    jar = make_plugin_jar_bytes()
    api = FakeAPI()
    add_paper_routes(api)
    add_hangar_native_routes(api, jar, platform_dependencies={"PAPER": ["26.1.2"]})

    code = run_main(tmp_path, api, base_config(HANGAR_PLUGIN))

    assert code == 0
    target = tmp_path / "artifacts" / "plugins" / "demo.jar"
    assert target.read_bytes() == jar
    out = capsys.readouterr().out
    assert " + demo.jar (1.0.0, hangar)" in out
    assert "Downloaded 2 items" in out


def test_hangar_owner_mismatch_rejected(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    add_paper_routes(api)
    api.add_json(
        hangar_project_url("Demo"), hangar_project_payload(owner="SomeoneElse", slug="Demo")
    )

    code = run_main(tmp_path, api, base_config(HANGAR_PLUGIN))

    assert code == 1
    assert "project mismatch" in capsys.readouterr().err
    assert api.count(hangar_version_url("Demo", "1.0.0")) == 0
    assert not (tmp_path / "artifacts" / "plugins" / "demo.jar").exists()


def test_hangar_version_plus_sign_encoded(tmp_path: Path) -> None:
    version = "1.0.0+build.5"
    jar = make_plugin_jar_bytes()
    api = FakeAPI()
    add_paper_routes(api)
    add_hangar_native_routes(api, jar, version=version)
    plugin = f"""
[plugins.demo]
provider = "hangar"
project = "Owner/Demo"
version = "{version}"
"""

    code = run_main(tmp_path, api, base_config(plugin))

    assert code == 0
    urls = api.urls()
    assert any("%2B" in url for url in urls)
    assert not any(" " in url for url in urls)
    assert api.count(hangar_version_url("Demo", version)) == 1


def test_hangar_project_not_found(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    add_paper_routes(api)
    api.add_json(hangar_project_url("Demo"), {"ok": False}, status=404)

    code = run_main(tmp_path, api, base_config(HANGAR_PLUGIN))

    assert code == 1
    assert "Hangar project not found" in capsys.readouterr().err


def test_hangar_version_not_found(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    add_paper_routes(api)
    api.add_json(hangar_project_url("Demo"), hangar_project_payload(owner="Owner", slug="Demo"))
    api.add_json(hangar_version_url("Demo", "1.0.0"), {"ok": False}, status=404)

    code = run_main(tmp_path, api, base_config(HANGAR_PLUGIN))

    assert code == 1
    assert "Hangar version not found" in capsys.readouterr().err


def test_hangar_missing_download_entries_rejected(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    add_paper_routes(api)
    api.add_json(hangar_project_url("Demo"), hangar_project_payload(owner="Owner", slug="Demo"))
    api.add_json(
        hangar_version_url("Demo", "1.0.0"),
        hangar_version_payload(
            name="1.0.0",
            downloads={"PAPER": {"downloadUrl": None, "externalUrl": None, "fileInfo": None}},
            platform_dependencies={"PAPER": ["26.1.2"]},
        ),
    )

    code = run_main(tmp_path, api, base_config(HANGAR_PLUGIN))

    assert code == 1
    assert "no download entry" in capsys.readouterr().err


def test_hangar_asset_must_match_fileinfo_name(tmp_path: Path, capsys) -> None:
    jar = make_plugin_jar_bytes()
    api = FakeAPI()
    add_paper_routes(api)
    add_hangar_native_routes(api, jar)
    plugin = HANGAR_PLUGIN + 'asset = "Other-1.0.0.jar"\n'

    code = run_main(tmp_path, api, base_config(plugin))

    assert code == 1
    assert "does not match" in capsys.readouterr().err
    assert api.count(NATIVE_DOWNLOAD_URL) == 0


def test_hangar_asset_exact_match_ok(tmp_path: Path) -> None:
    jar = make_plugin_jar_bytes()
    api = FakeAPI()
    add_paper_routes(api)
    add_hangar_native_routes(api, jar)
    plugin = HANGAR_PLUGIN + 'asset = "Demo-1.0.0.jar"\n'

    code = run_main(tmp_path, api, base_config(plugin))

    assert code == 0
    assert (tmp_path / "artifacts" / "plugins" / "demo.jar").read_bytes() == jar


def test_hangar_incompatible_platform_rejected(tmp_path: Path, capsys) -> None:
    jar = make_plugin_jar_bytes()
    api = FakeAPI()
    add_paper_routes(api)
    add_hangar_native_routes(api, jar, platform_dependencies={"PAPER": ["1.21.4", "1.21.5"]})

    code = run_main(tmp_path, api, base_config(HANGAR_PLUGIN))

    assert code == 1
    err = capsys.readouterr().err
    assert "compatibility does not include" in err
    assert api.count(NATIVE_DOWNLOAD_URL) == 0


def test_hangar_unknown_compatibility_warns_and_proceeds(tmp_path: Path, capsys) -> None:
    jar = make_plugin_jar_bytes()
    api = FakeAPI()
    add_paper_routes(api)
    add_hangar_native_routes(api, jar, platform_dependencies=None)

    code = run_main(tmp_path, api, base_config(HANGAR_PLUGIN))

    assert code == 0
    err = capsys.readouterr().err
    assert "cannot be confirmed" in err


def test_hangar_required_dependency_hint(tmp_path: Path, capsys) -> None:
    jar = make_plugin_jar_bytes()
    api = FakeAPI()
    add_paper_routes(api)
    add_hangar_native_routes(
        api,
        jar,
        platform_dependencies={"PAPER": ["26.1.2"]},
        plugin_dependencies={
            "PAPER": [
                {"name": "Vault", "required": True},
                {"name": "OptionalDep", "required": False},
            ]
        },
    )

    code = run_main(tmp_path, api, base_config(HANGAR_PLUGIN))

    assert code == 0
    err = capsys.readouterr().err
    assert "Vault" in err
    assert "not auto-installed" in err


GITHUB_PLUGIN = """
[plugins.demo]
provider = "github"
project = "Owner/Demo"
version = "v1.2.3"
"""


def test_github_provider_native_chain(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "super-secret-token")
    jar = make_plugin_jar_bytes()
    sha = sha256_hex(jar)
    asset_url = "https://github.com/Owner/Demo/releases/download/v1.2.3/Demo-1.2.3.jar"
    api = FakeAPI()
    add_paper_routes(api)
    api.add_json(
        github_release_api_url("Owner", "Demo", "v1.2.3"),
        github_release_payload(
            html_url="https://github.com/Owner/Demo/releases/tag/v1.2.3",
            tag="v1.2.3",
            assets=[
                github_asset_payload(
                    name="Demo-1.2.3.jar", url=asset_url, size=len(jar), digest=f"sha256:{sha}"
                )
            ],
        ),
    )
    api.add_bytes(asset_url, jar)

    code = run_main(tmp_path, api, base_config(GITHUB_PLUGIN))

    assert code == 0
    captured = capsys.readouterr()
    assert "github → github-release → github-asset" in captured.out
    assert "super-secret-token" not in captured.out
    assert "super-secret-token" not in captured.err
    assert (tmp_path / "artifacts" / "plugins" / "demo.jar").read_bytes() == jar
    # the token is only sent to api.github.com
    github_request = next(r for r in api.requests if r.url.host == "api.github.com")
    asset_request = next(r for r in api.requests if r.url.host == "github.com")
    assert github_request.headers.get("authorization") == "Bearer super-secret-token"
    assert "authorization" not in asset_request.headers


def test_github_tag_not_found(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    add_paper_routes(api)
    api.add_json(
        github_release_api_url("Owner", "Demo", "v1.2.3"), {"message": "Not Found"}, status=404
    )

    code = run_main(tmp_path, api, base_config(GITHUB_PLUGIN))

    assert code == 1
    assert "GitHub Release not found" in capsys.readouterr().err


def test_github_provider_ignores_follow_external_false(tmp_path: Path) -> None:
    """follow_external=false only blocks provider-marked external entries.

    It does not affect the native GitHubProvider.
    """
    jar = make_plugin_jar_bytes()
    sha = sha256_hex(jar)
    asset_url = "https://github.com/Owner/Demo/releases/download/v1.2.3/Demo-1.2.3.jar"
    api = FakeAPI()
    add_paper_routes(api)
    api.add_json(
        github_release_api_url("Owner", "Demo", "v1.2.3"),
        github_release_payload(
            html_url="https://github.com/Owner/Demo/releases/tag/v1.2.3",
            tag="v1.2.3",
            assets=[
                github_asset_payload(
                    name="Demo-1.2.3.jar", url=asset_url, size=len(jar), digest=f"sha256:{sha}"
                )
            ],
        ),
    )
    api.add_bytes(asset_url, jar)
    plugin = GITHUB_PLUGIN + "follow_external = false\n"

    code = run_main(tmp_path, api, base_config(plugin))

    assert code == 0
    assert (tmp_path / "artifacts" / "plugins" / "demo.jar").read_bytes() == jar
