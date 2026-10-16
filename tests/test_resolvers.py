"""Offline tests for resolvers and external entry resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from rocket.cli import main
from rocket.github import GitHubURLClass, classify_github_url
from rocket.models import GitHubCoordinates

from .helpers import (
    FakeAPI,
    add_paper_routes,
    base_config,
    github_asset_payload,
    github_release_api_url,
    github_release_payload,
    hangar_external_paper,
    hangar_project_payload,
    hangar_project_url,
    hangar_version_payload,
    hangar_version_url,
    make_plugin_jar_bytes,
    sha256_hex,
    write_config,
)

ESSENTIALS_TAG_URL = "https://github.com/EssentialsX/Essentials/releases/tag/2.22.0"


def run_main(tmp_path: Path, api: FakeAPI, text: str) -> int:
    config_path = write_config(tmp_path, text)
    return main(
        ["download", "--config", str(config_path)], transport=api.transport(), sleep=lambda _: None
    )


def external_plugin(
    plugin_id: str, *, external_url: str, asset: str | None = None, extra: str = ""
) -> str:
    asset_line = f'asset = "{asset}"\n' if asset else ""
    return f"""
[plugins.{plugin_id}]
provider = "hangar"
project = "EssentialsX/Essentials"
version = "2.22.0"
{asset_line}{extra}
"""


def add_hangar_external_routes(
    api: FakeAPI, *, slug: str = "Essentials", external_url: str
) -> None:
    api.add_json(hangar_project_url(slug), hangar_project_payload(owner="EssentialsX", slug=slug))
    api.add_json(
        hangar_version_url(slug, "2.22.0"),
        hangar_version_payload(
            name="2.22.0",
            downloads=hangar_external_paper(external_url=external_url),
            platform_dependencies={"PAPER": ["26.1.2"]},
        ),
    )


# ---------------------------------------------------------------------------
# URL classification unit tests
# ---------------------------------------------------------------------------


def test_classify_release_tag_page() -> None:
    url_class, link = classify_github_url(ESSENTIALS_TAG_URL)
    assert url_class is GitHubURLClass.RELEASE_TAG
    assert link == GitHubCoordinates(owner="EssentialsX", repo="Essentials", tag="2.22.0")


def test_classify_percent_encoded_tag() -> None:
    url_class, link = classify_github_url(
        "https://github.com/Owner/Repo/releases/tag/v1.0%2Bbuild.3"
    )
    assert url_class is GitHubURLClass.RELEASE_TAG
    assert link is not None
    assert link.tag == "v1.0+build.3"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/Owner/Repo/releases",
        "https://github.com/Owner/Repo/releases/latest",
    ],
)
def test_classify_not_fixed_entries(url: str) -> None:
    url_class, _ = classify_github_url(url)
    assert url_class is GitHubURLClass.NOT_FIXED


def test_classify_asset_download_link() -> None:
    url_class, _ = classify_github_url(
        "https://github.com/Owner/Repo/releases/download/v1.0/Demo.jar"
    )
    assert url_class is GitHubURLClass.ASSET


def test_classify_other_hosts_and_paths() -> None:
    for url in ["https://example.com/downloads", "https://github.com/Owner/Repo"]:
        url_class, _ = classify_github_url(url)
        assert url_class is GitHubURLClass.OTHER


# ---------------------------------------------------------------------------
# Hangar external entry → GitHub Release
# ---------------------------------------------------------------------------


def test_hangar_external_github_release_two_assets(tmp_path: Path, capsys) -> None:
    core = make_plugin_jar_bytes(name="EssentialsX")
    chat = make_plugin_jar_bytes(name="EssentialsXChat")
    core_url = (
        "https://github.com/EssentialsX/Essentials/releases/download/2.22.0/EssentialsX-2.22.0.jar"
    )
    chat_url = "https://github.com/EssentialsX/Essentials/releases/download/2.22.0/EssentialsXChat-2.22.0.jar"
    api = FakeAPI()
    add_paper_routes(api)
    add_hangar_external_routes(api, external_url=ESSENTIALS_TAG_URL)
    api.add_json(
        github_release_api_url("EssentialsX", "Essentials", "2.22.0"),
        github_release_payload(
            html_url=ESSENTIALS_TAG_URL,
            tag="2.22.0",
            assets=[
                github_asset_payload(
                    name="EssentialsX-2.22.0.jar",
                    url=core_url,
                    size=len(core),
                    digest=f"sha256:{sha256_hex(core)}",
                ),
                github_asset_payload(
                    name="EssentialsXChat-2.22.0.jar",
                    url=chat_url,
                    size=len(chat),
                    digest=f"sha256:{sha256_hex(chat)}",
                ),
                github_asset_payload(
                    name="EssentialsXAntiBuild-2.22.0.jar",
                    url="https://example.invalid/x.jar",
                    size=1,
                ),
            ],
        ),
    )
    api.add_bytes(core_url, core)
    api.add_bytes(chat_url, chat)
    plugin_sections = external_plugin(
        "essentialsx", external_url=ESSENTIALS_TAG_URL, asset="EssentialsX-2.22.0.jar"
    )
    plugin_sections += external_plugin(
        "essentialsx_chat", external_url=ESSENTIALS_TAG_URL, asset="EssentialsXChat-2.22.0.jar"
    )

    code = run_main(tmp_path, api, base_config(plugin_sections))

    assert code == 0
    plugins_dir = tmp_path / "artifacts" / "plugins"
    assert (plugins_dir / "essentialsx.jar").read_bytes() == core
    assert (plugins_dir / "essentialsx_chat.jar").read_bytes() == chat
    # the same Release is queried only once (shared GitHubClient cache)
    assert api.count(github_release_api_url("EssentialsX", "Essentials", "2.22.0")) == 1
    out = capsys.readouterr().out
    assert "hangar → github-release → github-asset" in out


def test_github_multiple_jars_without_asset_ambiguous(tmp_path: Path, capsys) -> None:
    core = make_plugin_jar_bytes(name="EssentialsX")
    chat = make_plugin_jar_bytes(name="EssentialsXChat")
    api = FakeAPI()
    add_paper_routes(api)
    add_hangar_external_routes(api, external_url=ESSENTIALS_TAG_URL)
    api.add_json(
        github_release_api_url("EssentialsX", "Essentials", "2.22.0"),
        github_release_payload(
            html_url=ESSENTIALS_TAG_URL,
            tag="2.22.0",
            assets=[
                github_asset_payload(
                    name="EssentialsX-2.22.0.jar",
                    url="https://github.com/dl/core.jar",
                    size=len(core),
                ),
                github_asset_payload(
                    name="EssentialsXChat-2.22.0.jar",
                    url="https://github.com/dl/chat.jar",
                    size=len(chat),
                ),
            ],
        ),
    )

    code = run_main(
        tmp_path, api, base_config(external_plugin("essentialsx", external_url=ESSENTIALS_TAG_URL))
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "multiple JAR attachments" in err
    assert "EssentialsX-2.22.0.jar" in err
    assert "EssentialsXChat-2.22.0.jar" in err
    assert not (tmp_path / "artifacts" / "plugins" / "essentialsx.jar").exists()


def test_github_asset_not_found_rejected(tmp_path: Path, capsys) -> None:
    core = make_plugin_jar_bytes(name="EssentialsX")
    api = FakeAPI()
    add_paper_routes(api)
    add_hangar_external_routes(api, external_url=ESSENTIALS_TAG_URL)
    api.add_json(
        github_release_api_url("EssentialsX", "Essentials", "2.22.0"),
        github_release_payload(
            html_url=ESSENTIALS_TAG_URL,
            tag="2.22.0",
            assets=[
                github_asset_payload(
                    name="EssentialsX-2.22.0.jar",
                    url="https://github.com/dl/core.jar",
                    size=len(core),
                )
            ],
        ),
    )

    code = run_main(
        tmp_path,
        api,
        base_config(
            external_plugin(
                "essentialsx", external_url=ESSENTIALS_TAG_URL, asset="Missing-2.22.0.jar"
            )
        ),
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "not found in Release" in err
    assert "EssentialsX-2.22.0.jar" in err


def test_github_assets_empty_rejected(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    add_paper_routes(api)
    add_hangar_external_routes(api, external_url=ESSENTIALS_TAG_URL)
    api.add_json(
        github_release_api_url("EssentialsX", "Essentials", "2.22.0"),
        github_release_payload(html_url=ESSENTIALS_TAG_URL, tag="2.22.0", assets=[]),
    )

    code = run_main(
        tmp_path, api, base_config(external_plugin("essentialsx", external_url=ESSENTIALS_TAG_URL))
    )

    assert code == 1
    assert "no usable JAR attachments" in capsys.readouterr().err


def test_github_single_jar_auto_selected(tmp_path: Path) -> None:
    jar = make_plugin_jar_bytes()
    asset_url = "https://github.com/Owner/Repo/releases/download/v1/Demo.jar"
    api = FakeAPI()
    add_paper_routes(api)
    add_hangar_external_routes(api, external_url="https://github.com/Owner/Repo/releases/tag/v1")
    api.add_json(
        github_release_api_url("Owner", "Repo", "v1"),
        github_release_payload(
            html_url="https://github.com/Owner/Repo/releases/tag/v1",
            tag="v1",
            assets=[github_asset_payload(name="Demo.jar", url=asset_url, size=len(jar))],
        ),
    )
    api.add_bytes(asset_url, jar)
    plugin = external_plugin(
        "essentialsx", external_url="https://github.com/Owner/Repo/releases/tag/v1"
    )

    code = run_main(tmp_path, api, base_config(plugin))

    assert code == 0
    assert (tmp_path / "artifacts" / "plugins" / "essentialsx.jar").read_bytes() == jar


def test_external_github_asset_direct_link(tmp_path: Path) -> None:
    jar = make_plugin_jar_bytes()
    asset_url = "https://github.com/Owner/Repo/releases/download/v1/Demo-1.0.jar"
    api = FakeAPI()
    add_paper_routes(api)
    add_hangar_external_routes(api, external_url=asset_url)
    api.add_bytes(asset_url, jar)
    plugin = external_plugin("essentialsx", external_url=asset_url)

    code = run_main(tmp_path, api, base_config(plugin))

    assert code == 0
    assert (tmp_path / "artifacts" / "plugins" / "essentialsx.jar").read_bytes() == jar


def test_latest_entry_rejected_without_network(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    add_paper_routes(api)
    latest = "https://github.com/Owner/Repo/releases/latest"
    add_hangar_external_routes(api, external_url=latest)

    code = run_main(tmp_path, api, base_config(external_plugin("essentialsx", external_url=latest)))

    assert code == 1
    assert "not pinned" in capsys.readouterr().err
    assert api.count(github_release_api_url("Owner", "Repo", "latest")) == 0


def test_patreon_like_html_external_rejected(tmp_path: Path, capsys) -> None:
    patreon = "https://www.patreon.com/CoreProtect/posts/download"
    api = FakeAPI()
    add_paper_routes(api)
    add_hangar_external_routes(api, external_url=patreon)
    api.add_html(patreon)

    code = run_main(
        tmp_path, api, base_config(external_plugin("coreprotect", external_url=patreon))
    )

    assert code == 1
    assert "not a valid ZIP/JAR" in capsys.readouterr().err
    assert not (tmp_path / "artifacts" / "plugins" / "coreprotect.jar").exists()


def test_follow_external_false_rejects_external(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    add_paper_routes(api)
    add_hangar_external_routes(api, external_url=ESSENTIALS_TAG_URL)
    plugin = external_plugin(
        "essentialsx", external_url=ESSENTIALS_TAG_URL, extra="follow_external = false\n"
    )

    code = run_main(tmp_path, api, base_config(plugin))

    assert code == 1
    err = capsys.readouterr().err
    assert "follow_external" in err
    assert api.count(github_release_api_url("EssentialsX", "Essentials", "2.22.0")) == 0


def test_explicit_direct_on_github_release_entry_rejected(tmp_path: Path, capsys) -> None:
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
    plugin = """
[plugins.demo]
provider = "github"
project = "Owner/Demo"
version = "v1.2.3"
resolver = "direct"
"""

    code = run_main(tmp_path, api, base_config(plugin))

    assert code == 1
    assert "does not match" in capsys.readouterr().err
    assert api.count(asset_url) == 0


def test_explicit_github_release_on_direct_entry_rejected(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    add_paper_routes(api)
    api.add_json(hangar_project_url("Demo"), hangar_project_payload(owner="Owner", slug="Demo"))
    api.add_json(
        hangar_version_url("Demo", "1.0.0"),
        hangar_version_payload(
            name="1.0.0",
            downloads=hangar_external_paper(external_url="https://example.com/not-github"),
            platform_dependencies={"PAPER": ["26.1.2"]},
        ),
    )
    plugin = """
[plugins.demo]
provider = "hangar"
project = "Owner/Demo"
version = "1.0.0"
resolver = "github_release"
"""

    code = run_main(tmp_path, api, base_config(plugin))

    assert code == 1
    assert "does not match" in capsys.readouterr().err
