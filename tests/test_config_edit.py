"""Unit tests for config_edit: textual [plugins.<id>] section edits in server.toml."""

from __future__ import annotations

from pathlib import Path

import pytest

from rocket.config import load_config
from rocket.config_edit import (
    PaperPin,
    PluginPin,
    add_plugin_section,
    remove_plugin_section,
    update_version_pins,
)
from rocket.errors import RocketError

from .helpers import base_config, write_config

GOOD_SECTION = """
[plugins.good]
provider = "hangar"
project = "Owner/Good"
version = "1.0.0"
"""


def add_good(path: Path, *, version: str = "1.0.0", asset: str | None = None) -> None:
    add_plugin_section(
        path,
        plugin_id="good",
        provider="hangar",
        project="Owner/Good",
        version=version,
        asset=asset,
    )


def test_add_appends_a_parseable_section(tmp_path: Path) -> None:
    path = write_config(tmp_path, base_config())

    add_good(path, asset="Good-1.0.0.jar")

    text = path.read_text(encoding="utf-8")
    assert text.endswith(
        '\n[plugins.good]\nprovider = "hangar"\nproject = "Owner/Good"\n'
        'version = "1.0.0"\nasset = "Good-1.0.0.jar"\n'
    )
    config = load_config(path)
    assert [plugin.id for plugin in config.plugins] == ["good"]
    assert config.plugins[0].asset == "Good-1.0.0.jar"


def test_add_preserves_handwritten_content(tmp_path: Path) -> None:
    original = base_config(GOOD_SECTION).replace(
        "[plugins.good]", "# my plugins\n[plugins.good]  # hosted on hangar"
    )
    path = write_config(tmp_path, original)

    add_plugin_section(
        path,
        plugin_id="other",
        provider="modrinth",
        project="other",
        version="2.0",
        asset=None,
    )

    text = path.read_text(encoding="utf-8")
    assert text.startswith(original)
    assert "[plugins.other]" in text
    assert [plugin.id for plugin in load_config(path).plugins] == ["good", "other"]


def test_add_to_file_without_trailing_newline(tmp_path: Path) -> None:
    path = write_config(tmp_path, base_config().rstrip("\n"))

    add_good(path)

    assert load_config(path).plugins[0].id == "good"


def test_add_escapes_values(tmp_path: Path) -> None:
    path = write_config(tmp_path, base_config())

    add_good(path, version='1.0 "beta"')

    assert load_config(path).plugins[0].version == '1.0 "beta"'


def test_remove_only_plugin_leaves_valid_config(tmp_path: Path) -> None:
    path = write_config(tmp_path, base_config(GOOD_SECTION))
    before = path.read_text(encoding="utf-8")

    remove_plugin_section(path, "good")

    text = path.read_text(encoding="utf-8")
    assert text == before.replace(GOOD_SECTION.lstrip("\n"), "").rstrip("\n") + "\n"
    assert load_config(path).plugins == ()


def test_remove_middle_plugin_keeps_neighbors(tmp_path: Path) -> None:
    sections = (
        GOOD_SECTION
        + """
[plugins.other]
provider = "modrinth"
project = "other"
version = "2.0"

[plugins.third]
provider = "github"
project = "Owner/Third"
version = "3.0"
"""
    )
    path = write_config(tmp_path, base_config(sections))

    remove_plugin_section(path, "other")

    text = path.read_text(encoding="utf-8")
    assert "other" not in text
    assert "[plugins.good]" in text
    assert "[plugins.third]" in text
    assert [plugin.id for plugin in load_config(path).plugins] == ["good", "third"]


def test_remove_first_plugin_keeps_separator(tmp_path: Path) -> None:
    path = write_config(tmp_path, base_config(GOOD_SECTION + GOOD_SECTION.replace("good", "other")))

    remove_plugin_section(path, "good")

    text = path.read_text(encoding="utf-8")
    assert "\n\n[plugins.other]\n" in text
    assert [plugin.id for plugin in load_config(path).plugins] == ["other"]


def test_remove_quoted_key_form(tmp_path: Path) -> None:
    path = write_config(
        tmp_path, base_config(GOOD_SECTION.replace("[plugins.good]", '[plugins."good"]'))
    )

    remove_plugin_section(path, "good")

    assert load_config(path).plugins == ()


def test_remove_dotted_key_form_refuses_with_hint(tmp_path: Path) -> None:
    dotted = """
[plugins]
good.provider = "hangar"
good.project = "Owner/Good"
good.version = "1.0.0"
"""
    path = write_config(tmp_path, base_config(dotted))
    before = path.read_text(encoding="utf-8")

    with pytest.raises(RocketError, match="cannot locate"):
        remove_plugin_section(path, "good")

    assert path.read_text(encoding="utf-8") == before


def test_remove_missing_section_refuses(tmp_path: Path) -> None:
    path = write_config(tmp_path, base_config())
    before = path.read_text(encoding="utf-8")

    with pytest.raises(RocketError, match="cannot locate"):
        remove_plugin_section(path, "good")

    assert path.read_text(encoding="utf-8") == before


def test_remove_does_not_touch_other_sections_or_comments(tmp_path: Path) -> None:
    original = base_config(GOOD_SECTION).replace(
        'user_agent = "rocket-test/0.1 (test@example.invalid)"',
        'user_agent = "rocket-test/0.1 (test@example.invalid)"  # replace me',
    )
    path = write_config(tmp_path, original)

    remove_plugin_section(path, "good")

    text = path.read_text(encoding="utf-8")
    assert "# replace me" in text
    assert 'version = "26.1.2"' in text


# ---------------------------------------------------------------------------
# update_version_pins
# ---------------------------------------------------------------------------


def test_update_pins_rewrites_version_and_asset_in_place(tmp_path: Path) -> None:
    original = base_config(
        "\n[plugins.good]\n"
        'provider = "hangar"   # hosted\n'
        'project = "Owner/Good"\n'
        'version = "1.0.0"   # pinned for the event\n'
        'asset = "Good-1.0.0.jar"\n'
    )
    path = write_config(tmp_path, original)

    update_version_pins(
        path, paper=None, plugins={"good": PluginPin(version="1.1.0", asset="Good-1.1.0.jar")}
    )

    text = path.read_text(encoding="utf-8")
    assert 'version = "1.1.0"   # pinned for the event\n' in text
    assert 'asset = "Good-1.1.0.jar"\n' in text
    assert 'provider = "hangar"   # hosted\n' in text
    plugin = load_config(path).plugins[0]
    assert plugin.version == "1.1.0"
    assert plugin.asset == "Good-1.1.0.jar"


def test_update_pins_removes_and_adds_the_asset_line(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        base_config(
            "\n[plugins.good]\n"
            'provider = "hangar"\n'
            'project = "Owner/Good"\n'
            'version = "1.0.0"\n'
            'asset = "Good-1.0.0.jar"\n'
            "\n[plugins.other]\n"
            'provider = "modrinth"\n'
            'project = "other"\n'
            'version = "2.0"\n'
        ),
    )

    update_version_pins(
        path,
        paper=None,
        plugins={
            "good": PluginPin(version="1.1.0", asset=None),
            "other": PluginPin(version="2.1", asset="other-2.1.jar"),
        },
    )

    text = path.read_text(encoding="utf-8")
    assert "asset" not in text.split("[plugins.other]")[0].split("[plugins.good]")[1]
    assert 'version = "2.1"\nasset = "other-2.1.jar"\n' in text
    config = load_config(path)
    assert config.plugins[0].asset is None
    assert config.plugins[1].asset == "other-2.1.jar"


def test_update_pins_paper_version_and_build(tmp_path: Path) -> None:
    original = base_config().replace(
        'version = "26.1.2"', 'version = "26.1.1"   # current\nbuild = 5'
    )
    path = write_config(tmp_path, original)

    update_version_pins(path, paper=PaperPin(version="26.1.2", build=None), plugins={})

    text = path.read_text(encoding="utf-8")
    assert 'version = "26.1.2"   # current\n' in text
    assert "build" not in text
    config = load_config(path)
    assert config.paper.version == "26.1.2"
    assert config.paper.build is None


def test_update_pins_can_set_a_build_where_none_existed(tmp_path: Path) -> None:
    path = write_config(tmp_path, base_config())

    update_version_pins(path, paper=PaperPin(version="26.1.2", build=57), plugins={})

    text = path.read_text(encoding="utf-8")
    assert 'version = "26.1.2"\nbuild = 57\n' in text
    assert load_config(path).paper.build == 57


def test_update_pins_refuses_dotted_key_entries(tmp_path: Path) -> None:
    original = (
        base_config().replace("[plugins]", "")
        + '\n[plugins]\ngood = {provider = "hangar", project = "Owner/Good", version = "1.0.0"}\n'
    )
    path = write_config(tmp_path, original)

    with pytest.raises(RocketError):
        update_version_pins(
            path, paper=None, plugins={"good": PluginPin(version="1.1.0", asset=None)}
        )
    assert path.read_text(encoding="utf-8") == original


def test_update_pins_unknown_plugin_id_is_an_error(tmp_path: Path) -> None:
    path = write_config(tmp_path, base_config(GOOD_SECTION))

    with pytest.raises(RocketError):
        update_version_pins(
            path, paper=None, plugins={"missing": PluginPin(version="1.1.0", asset=None)}
        )
