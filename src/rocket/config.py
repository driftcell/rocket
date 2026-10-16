"""Reading and strict validation of server.toml.

All type/field errors carry full field paths and fail before any network request (exit code 2).
This version does not ignore unknown fields, unknown providers or unknown resolvers.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import USER_AGENT
from .errors import ConfigError
from .models import ResolverChoice

_PLUGIN_ID_RE = re.compile(r"[a-z0-9_-]+")
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")
_PROVIDER_CHOICES = ("hangar", "github", "modrinth", "spiget", "zrips")
_PLATFORM_CHOICES = ("PAPER",)
_GENERIC_USER_AGENT_PREFIXES = ("curl", "wget", "python-requests", "httpx", "python-urllib")


@dataclass(frozen=True)
class PaperConfig:
    """[paper] section."""

    version: str
    channel: str = "STABLE"
    build: int | None = None


@dataclass(frozen=True)
class HttpConfig:
    """[http] section; the whole section and every field in it is optional."""

    user_agent: str = USER_AGENT
    timeout_seconds: float = 30.0
    retries: int = 3


@dataclass(frozen=True)
class DownloadConfig:
    """[download] section (output_dir resolved to an absolute path)."""

    output_dir: Path


@dataclass(frozen=True)
class PluginConfig:
    """[plugins.<id>] item."""

    id: str
    provider: str
    project: str
    version: str
    asset: str | None = None
    resolver: ResolverChoice = ResolverChoice.AUTO
    platform: str = "PAPER"
    follow_external: bool = True


@dataclass(frozen=True)
class PluginDefaultsConfig:
    """[plugin_defaults] section."""

    platform: str = "PAPER"
    follow_external: bool = True


@dataclass(frozen=True)
class Config:
    """Parsed result of the whole server.toml."""

    path: Path
    schema_version: int
    paper: PaperConfig
    http: HttpConfig
    download: DownloadConfig
    plugin_defaults: PluginDefaultsConfig
    plugins: tuple[PluginConfig, ...]
    warnings: tuple[str, ...] = ()


def load_config(path: Path) -> Config:
    """Read and strictly validate the config file; any problem raises ConfigError."""
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(
            f"cannot read config file {path}: {exc}",
            hint="point --config at an existing server.toml",
        ) from exc
    try:
        raw = tomllib.loads(raw_text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"TOML syntax error ({path}): {exc}") from exc
    return parse_config(raw, path=path)


def parse_config(raw: dict[str, Any], *, path: Path) -> Config:
    """Validate a parsed TOML dict and build a Config."""
    _check_unknown_keys(
        raw,
        {"schema_version", "paper", "http", "download", "plugin_defaults", "plugins"},
        "",
    )

    schema_version = _parse_schema_version(raw)

    if "paper" not in raw:
        raise ConfigError(
            "missing [paper] section",
            hint="the [paper] section is required; at least declare paper.version",
        )
    paper = _parse_paper(_require_table(raw["paper"], "paper"))
    http = _parse_http(raw.get("http"))

    download = _parse_download(raw.get("download"), config_path=path)
    plugin_defaults = _parse_plugin_defaults(raw.get("plugin_defaults"))
    plugins = _parse_plugins(
        raw.get("plugins"),
        platform_default=plugin_defaults.platform,
        follow_default=plugin_defaults.follow_external,
    )

    warnings: list[str] = []
    agent_lower = http.user_agent.strip().lower()
    if any(agent_lower.startswith(prefix) for prefix in _GENERIC_USER_AGENT_PREFIXES):
        warnings.append(
            f"http.user_agent looks like a generic default ({http.user_agent!r}); "
            "upstream services require software identification and a real contact "
            "email or project URL"
        )

    return Config(
        path=path,
        schema_version=schema_version,
        paper=paper,
        http=http,
        download=download,
        plugin_defaults=plugin_defaults,
        plugins=plugins,
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# Section parsers
# ---------------------------------------------------------------------------


def is_valid_plugin_id(value: str) -> bool:
    """Whether a plugin id is usable in [plugins.<id>] (lowercase letters, digits, _ and -)."""
    return _PLUGIN_ID_RE.fullmatch(value) is not None


def _parse_schema_version(raw: dict[str, Any]) -> int:
    if "schema_version" not in raw:
        raise ConfigError(
            "missing required field schema_version",
            hint="this version only supports schema_version = 1",
        )
    value = raw["schema_version"]
    if isinstance(value, bool) or not isinstance(value, int):
        raise _type_error("schema_version", value, "an integer")
    if value != 1:
        raise ConfigError(f"unsupported schema_version: {value}", hint="only 1 is accepted")
    return value


def _parse_paper(table: dict[str, Any]) -> PaperConfig:
    _check_unknown_keys(table, {"version", "channel", "build"}, "paper")

    if "version" not in table:
        raise ConfigError("missing required field paper.version")
    version_value = table["version"]
    if not isinstance(version_value, str):
        raise ConfigError(
            f"paper.version must be a quoted string, got {_type_name(version_value)}",
            hint=(
                'write versions as strings such as "26.1.2"; numeric values like 26.1 are rejected'
            ),
        )
    if not version_value.strip():
        raise ConfigError("paper.version must not be empty")

    channel = _optional_string(table, "channel", "paper") or "STABLE"
    if channel != "STABLE":
        raise ConfigError(
            f"paper.channel only supports STABLE, got {channel!r}",
            hint="experimental and other channels are not accepted",
        )

    build: int | None = None
    if "build" in table:
        build_value = table["build"]
        if isinstance(build_value, bool) or not isinstance(build_value, int):
            raise _type_error("paper.build", build_value, "a positive integer")
        if build_value < 1:
            raise ConfigError(f"paper.build must be a positive integer, got {build_value}")
        build = build_value

    return PaperConfig(version=version_value, channel=channel, build=build)


def _parse_http(value: Any) -> HttpConfig:
    if value is None:
        return HttpConfig()
    table = _require_table(value, "http")
    _check_unknown_keys(table, {"user_agent", "timeout_seconds", "retries"}, "http")

    # The default identifies rocket itself (version + project URL), which already
    # satisfies the upstream software-identification policy; users may still
    # override it with their own contact info.
    user_agent = _optional_string(table, "user_agent", "http") or USER_AGENT
    timeout_seconds = _positive_number(table, "timeout_seconds", "http", default=30.0)
    retries = _non_negative_int(table, "retries", "http", default=3)
    return HttpConfig(user_agent=user_agent, timeout_seconds=timeout_seconds, retries=retries)


def _parse_download(value: Any, *, config_path: Path) -> DownloadConfig:
    raw_dir = "./artifacts"
    if value is not None:
        table = _require_table(value, "download")
        _check_unknown_keys(table, {"output_dir"}, "download")
        raw_dir = _optional_string(table, "output_dir", "download") or "./artifacts"
    output_dir = Path(raw_dir).expanduser()
    if not output_dir.is_absolute():
        output_dir = config_path.parent / output_dir
    return DownloadConfig(output_dir=output_dir.resolve())


def _parse_plugin_defaults(value: Any) -> PluginDefaultsConfig:
    if value is None:
        return PluginDefaultsConfig()
    table = _require_table(value, "plugin_defaults")
    _check_unknown_keys(table, {"platform", "follow_external"}, "plugin_defaults")
    platform = _optional_string(table, "platform", "plugin_defaults") or "PAPER"
    if platform not in _PLATFORM_CHOICES:
        raise ConfigError(
            f"plugin_defaults.platform only supports PAPER, got {platform!r}",
        )
    follow_external = _optional_bool(table, "follow_external", "plugin_defaults", default=True)
    return PluginDefaultsConfig(platform=platform, follow_external=follow_external)


def _parse_plugins(
    value: Any,
    *,
    platform_default: str,
    follow_default: bool,
) -> tuple[PluginConfig, ...]:
    if value is None:
        return ()
    table = _require_table(value, "plugins")
    plugins: list[PluginConfig] = []
    for plugin_id, item in table.items():
        if not _PLUGIN_ID_RE.fullmatch(plugin_id):
            raise ConfigError(
                f"invalid plugin id: {plugin_id!r}",
                hint="ids may only contain lowercase letters, digits, underscores and hyphens",
            )
        path = f"plugins.{plugin_id}"
        if not isinstance(item, dict):
            raise ConfigError(
                f"{path} must be a table ([plugins.{plugin_id}]), got {_type_name(item)}",
                hint=(
                    "put shared settings in [plugin_defaults]; "
                    "[plugins] may only contain plugin items"
                ),
            )
        _check_unknown_keys(
            item,
            {"provider", "project", "version", "asset", "resolver", "platform", "follow_external"},
            path,
        )

        provider = _required_string(item, "provider", path)
        if provider not in _PROVIDER_CHOICES:
            raise ConfigError(
                f"unknown provider: {provider!r} (field {path}.provider)",
                hint=f"available providers: {', '.join(_PROVIDER_CHOICES)}",
            )
        project = _required_string(item, "project", path)
        version = _required_string(item, "version", path)

        asset = _optional_string(item, "asset", path)
        if asset is not None and ("/" in asset or "\\" in asset):
            raise ConfigError(
                f"{path}.asset must be an upstream filename and cannot contain path "
                f"separators: {asset!r}",
            )

        resolver_raw = _optional_string(item, "resolver", path) or "auto"
        if resolver_raw not in {choice.value for choice in ResolverChoice}:
            raise ConfigError(
                f"unknown resolver: {resolver_raw!r} (field {path}.resolver)",
                hint="available resolvers: auto, direct, github_release",
            )

        platform = _optional_string(item, "platform", path) or platform_default
        if platform not in _PLATFORM_CHOICES:
            raise ConfigError(f"{path}.platform only supports PAPER, got {platform!r}")

        follow_external = _optional_bool(item, "follow_external", path, default=follow_default)

        plugins.append(
            PluginConfig(
                id=plugin_id,
                provider=provider,
                project=project,
                version=version,
                asset=asset,
                resolver=ResolverChoice(resolver_raw),
                platform=platform,
                follow_external=follow_external,
            )
        )
    return tuple(plugins)


# ---------------------------------------------------------------------------
# Generic validation helpers
# ---------------------------------------------------------------------------


def _type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, int):
        return "an integer"
    if isinstance(value, float):
        return "a float"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, list):
        return "an array"
    if isinstance(value, dict):
        return "a table"
    return type(value).__name__


def _type_error(field_path: str, value: Any, expected: str) -> ConfigError:
    return ConfigError(f"{field_path} must be {expected}, got {_type_name(value)}")


def _check_unknown_keys(table: dict[str, Any], allowed: set[str], prefix: str) -> None:
    for key in table:
        if key not in allowed:
            full = f"{prefix}.{key}" if prefix else str(key)
            raise ConfigError(
                f"unknown field {full}",
                hint=(
                    "unknown fields are not ignored; put shared settings in "
                    "[plugin_defaults] or the relevant section"
                ),
            )


def _require_table(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _type_error(path, value, "a table")
    return value


def _required_string(table: dict[str, Any], key: str, path: str) -> str:
    if key not in table:
        raise ConfigError(f"missing required field {path}.{key}")
    value = table[key]
    if not isinstance(value, str):
        raise _type_error(f"{path}.{key}", value, "a string")
    if not value.strip():
        raise ConfigError(f"{path}.{key} must not be empty")
    return value


def _optional_string(table: dict[str, Any], key: str, path: str) -> str | None:
    if key not in table:
        return None
    value = table[key]
    if not isinstance(value, str):
        raise _type_error(f"{path}.{key}", value, "a string")
    if not value.strip():
        raise ConfigError(f"{path}.{key} must not be empty")
    return value


def _optional_bool(table: dict[str, Any], key: str, path: str, *, default: bool) -> bool:
    if key not in table:
        return default
    value = table[key]
    if not isinstance(value, bool):
        raise _type_error(f"{path}.{key}", value, "a boolean")
    return value


def _positive_number(table: dict[str, Any], key: str, path: str, *, default: float) -> float:
    if key not in table:
        return default
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _type_error(f"{path}.{key}", value, "a positive number")
    if value <= 0:
        raise ConfigError(f"{path}.{key} must be a positive number, got {value}")
    return float(value)


def _non_negative_int(table: dict[str, Any], key: str, path: str, *, default: int) -> int:
    if key not in table:
        return default
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise _type_error(f"{path}.{key}", value, "a non-negative integer")
    if value < 0:
        raise ConfigError(f"{path}.{key} must be a non-negative integer, got {value}")
    return value
