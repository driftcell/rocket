"""Offline tests for config parsing and strict validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from rocket.config import load_config
from rocket.errors import ConfigError
from rocket.models import ResolverChoice

from .helpers import TEST_USER_AGENT, write_config

MINIMAL = f"""
schema_version = 1

[paper]
version = "26.1.2"

[http]
user_agent = "{TEST_USER_AGENT}"
"""

FULL = f"""
schema_version = 1

[paper]
version = "26.1.2"
channel = "STABLE"

[http]
user_agent = "{TEST_USER_AGENT}"
timeout_seconds = 12.5
retries = 0

[download]
output_dir = "./custom-out"

[plugin_defaults]
platform = "PAPER"
follow_external = false

[plugins.viaversion]
provider = "hangar"
project = "ViaVersion/ViaVersion"
version = "5.12.1"

[plugins.essentialsx]
provider = "hangar"
project = "EssentialsX/Essentials"
version = "2.22.0"
asset = "EssentialsX-2.22.0.jar"
resolver = "github_release"
follow_external = true
"""


def test_minimal_config_defaults(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, MINIMAL))
    assert config.schema_version == 1
    assert config.paper.version == "26.1.2"
    assert config.paper.channel == "STABLE"
    assert config.paper.build is None
    assert config.http.user_agent == TEST_USER_AGENT
    assert config.http.timeout_seconds == 30.0
    assert config.http.retries == 3
    assert config.download.output_dir == (tmp_path / "artifacts").resolve()
    assert config.plugins == ()
    assert config.warnings == ()


def test_full_config_parses(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, FULL))
    assert config.http.timeout_seconds == 12.5
    assert config.http.retries == 0
    assert config.download.output_dir == (tmp_path / "custom-out").resolve()
    assert [plugin.id for plugin in config.plugins] == ["viaversion", "essentialsx"]

    viaversion, essentialsx = config.plugins
    assert viaversion.provider == "hangar"
    assert viaversion.project == "ViaVersion/ViaVersion"
    assert viaversion.version == "5.12.1"
    assert viaversion.asset is None
    assert viaversion.resolver is ResolverChoice.AUTO
    assert viaversion.follow_external is False  # from plugin_defaults
    assert essentialsx.resolver is ResolverChoice.GITHUB_RELEASE
    assert essentialsx.follow_external is True  # plugin item overrides the default
    assert essentialsx.asset == "EssentialsX-2.22.0.jar"


def test_output_dir_relative_to_config_file(tmp_path: Path) -> None:
    nested = tmp_path / "configs"
    nested.mkdir()
    path = nested / "server.toml"
    path.write_text(MINIMAL + '\n[download]\noutput_dir = "./deep/out"\n', encoding="utf-8")
    config = load_config(path)
    assert config.download.output_dir == (nested / "deep" / "out").resolve()


@pytest.mark.parametrize(
    "snippet",
    [
        "version = 26.1",
        "version = 26",
        'version = "26.1.2"\nbuild = "42"',
        'version = "26.1.2"\nchannel = "BETA"',
        'version = "26.1.2"\nbuild = 0',
        'version = "26.1.2"\nbuild = true',
        'version = "26.1.2"\nchannel = 1',
        'version = ""',
    ],
)
def test_paper_field_errors(tmp_path: Path, snippet: str) -> None:
    text = MINIMAL.replace('version = "26.1.2"', snippet)
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(tmp_path, text))
    assert "paper." in excinfo.value.message


def test_paper_version_float_names_field_and_type(tmp_path: Path) -> None:
    text = MINIMAL.replace('version = "26.1.2"', "version = 26.1")
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(tmp_path, text))
    assert "paper.version" in excinfo.value.message
    assert "a float" in excinfo.value.message


def test_missing_paper_section(tmp_path: Path) -> None:
    text = MINIMAL.replace('[paper]\nversion = "26.1.2"\n', "")
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(tmp_path, text))
    assert "paper" in excinfo.value.message


def test_missing_http_section(tmp_path: Path) -> None:
    text = MINIMAL.replace(f'[http]\nuser_agent = "{TEST_USER_AGENT}"\n', "")
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(tmp_path, text))
    assert "http" in excinfo.value.message


def test_missing_user_agent(tmp_path: Path) -> None:
    text = MINIMAL.replace(f'user_agent = "{TEST_USER_AGENT}"\n', "")
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(tmp_path, text))
    assert "http.user_agent" in excinfo.value.message


def test_user_agent_must_be_string(tmp_path: Path) -> None:
    text = MINIMAL.replace(f'user_agent = "{TEST_USER_AGENT}"', "user_agent = 123")
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(tmp_path, text))
    assert "http.user_agent" in excinfo.value.message
    assert "a string" in excinfo.value.message


@pytest.mark.parametrize(
    "snippet",
    [
        "timeout_seconds = 0",
        "timeout_seconds = -1",
        "timeout_seconds = true",
        "retries = -1",
        "retries = 1.5",
        "retries = true",
    ],
)
def test_http_field_errors(tmp_path: Path, snippet: str) -> None:
    text = MINIMAL.replace(
        f'user_agent = "{TEST_USER_AGENT}"', f'user_agent = "{TEST_USER_AGENT}"\n{snippet}'
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(tmp_path, text))
    assert "http." in excinfo.value.message


def test_schema_version_required(tmp_path: Path) -> None:
    text = MINIMAL.replace("schema_version = 1\n", "")
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(tmp_path, text))
    assert "schema_version" in excinfo.value.message


def test_schema_version_must_be_one(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(
            write_config(tmp_path, MINIMAL.replace("schema_version = 1", "schema_version = 2"))
        )
    assert "schema_version" in excinfo.value.message


def test_schema_version_type(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(
            write_config(tmp_path, MINIMAL.replace("schema_version = 1", 'schema_version = "1"'))
        )
    assert "schema_version" in excinfo.value.message
    assert "an integer" in excinfo.value.message


def test_unknown_top_level_field(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(tmp_path, MINIMAL + "\n[future]\nx = 1\n"))
    assert "future" in excinfo.value.message


def test_unknown_paper_field(tmp_path: Path) -> None:
    text = MINIMAL.replace('version = "26.1.2"', 'version = "26.1.2"\nfoo = "bar"')
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(tmp_path, text))
    assert "paper.foo" in excinfo.value.message


def test_unknown_plugin_field(tmp_path: Path) -> None:
    text = (
        MINIMAL
        + """
[plugins.demo]
provider = "hangar"
project = "Owner/Demo"
version = "1.0.0"
enabled = true
"""
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(tmp_path, text))
    assert "plugins.demo.enabled" in excinfo.value.message


def test_unknown_provider(tmp_path: Path) -> None:
    text = (
        MINIMAL
        + """
[plugins.demo]
provider = "spigot"
project = "Owner/Demo"
version = "1.0.0"
"""
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(tmp_path, text))
    assert "provider" in excinfo.value.message
    assert "spigot" in excinfo.value.message


def test_unknown_resolver(tmp_path: Path) -> None:
    text = (
        MINIMAL
        + """
[plugins.demo]
provider = "hangar"
project = "Owner/Demo"
version = "1.0.0"
resolver = "guess"
"""
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(tmp_path, text))
    assert "resolver" in excinfo.value.message
    assert "guess" in excinfo.value.message


def test_plugin_id_rejects_uppercase(tmp_path: Path) -> None:
    text = (
        MINIMAL
        + """
[plugins.Bad_Id]
provider = "hangar"
project = "Owner/Demo"
version = "1.0.0"
"""
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(tmp_path, text))
    assert "id" in excinfo.value.message


@pytest.mark.parametrize(
    "body",
    [
        'project = "Owner/Demo"\nversion = "1.0.0"',
        'provider = "hangar"\nversion = "1.0.0"',
        'provider = "hangar"\nproject = "Owner/Demo"',
        'provider = "hangar"\nproject = "Owner/Demo"\nversion = 1.0',
    ],
)
def test_plugin_missing_or_bad_fields(tmp_path: Path, body: str) -> None:
    text = f"{MINIMAL}\n[plugins.demo]\n{body}\n"
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(tmp_path, text))
    assert "plugins.demo" in excinfo.value.message


def test_global_options_under_plugins_table_rejected(tmp_path: Path) -> None:
    text = f'{MINIMAL}\n[plugins]\nplatform = "PAPER"\n'
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(tmp_path, text))
    assert "plugins.platform" in excinfo.value.message


def test_plugin_platform_only_paper(tmp_path: Path) -> None:
    text = (
        MINIMAL
        + """
[plugin_defaults]
platform = "VELOCITY"
"""
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(tmp_path, text))
    assert "platform" in excinfo.value.message


def test_asset_with_path_separator_rejected(tmp_path: Path) -> None:
    text = (
        MINIMAL
        + """
[plugins.demo]
provider = "hangar"
project = "Owner/Demo"
version = "1.0.0"
asset = "sub/dir.jar"
"""
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(tmp_path, text))
    assert "asset" in excinfo.value.message


def test_generic_user_agent_warns(tmp_path: Path) -> None:
    text = MINIMAL.replace(f'user_agent = "{TEST_USER_AGENT}"', 'user_agent = "curl/8.4.0"')
    config = load_config(write_config(tmp_path, text))
    assert config.warnings
    assert "user_agent" in config.warnings[0]


def test_modrinth_provider_accepted(tmp_path: Path) -> None:
    text = (
        MINIMAL
        + """
[plugins.coreprotect]
provider = "modrinth"
project = "coreprotect"
version = "24.1"
"""
    )
    config = load_config(write_config(tmp_path, text))
    assert config.plugins[0].provider == "modrinth"
    assert config.plugins[0].project == "coreprotect"


def test_server_section_is_rejected(tmp_path: Path) -> None:
    # server-init was removed; [server] is no longer part of the config schema.
    text = MINIMAL + '\n[server]\ndir = "./server"\n'
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(tmp_path, text))
    assert "server" in excinfo.value.message


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(tmp_path / "missing.toml")
    assert "cannot read" in excinfo.value.message


def test_toml_syntax_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(tmp_path, "schema_version = = 1"))
    assert "TOML" in excinfo.value.message
