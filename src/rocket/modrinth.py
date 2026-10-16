"""Modrinth API v2 client and version/file selection logic.

Used by ModrinthProvider; each project is queried only once per run. Modrinth version files
usually provide sha512/sha1 hashes (no sha256), so sha512 is preferred as the upstream checksum.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .errors import HTTPStatusError, ProviderError
from .http_client import HTTPClient
from .models import Checksum

_API_BASE = "https://api.modrinth.com/v2"
_SLUG_RE = re.compile(r"[A-Za-z0-9_.-]+")
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")
_SHA512_RE = re.compile(r"[0-9a-fA-F]{128}")

# Modrinth loaders accepted for the PAPER platform (Bukkit-family plugins can all run on Paper)
PAPER_LOADERS = ("paper", "bukkit", "spigot", "purpur", "folia")


@dataclass(frozen=True)
class ModrinthFile:
    """A single file of a version."""

    filename: str
    url: str
    size: int | None
    checksum: Checksum | None


@dataclass(frozen=True)
class ModrinthVersion:
    """Minimal required structure of a version."""

    version_number: str
    game_versions: tuple[str, ...]
    loaders: tuple[str, ...]
    version_type: str
    files: tuple[ModrinthFile, ...]


def parse_slug(project: str, *, field_path: str) -> str:
    """Validate and normalize a Modrinth project slug."""
    candidate = project.strip()
    if not _SLUG_RE.fullmatch(candidate):
        raise ProviderError(
            f"{field_path} is not a valid Modrinth slug: {project!r}",
            hint="use the project slug from the Modrinth page, e.g. coreprotect",
        )
    return candidate.lower()


class ModrinthClient:
    """Modrinth query client with in-run caching."""

    def __init__(self, http: HTTPClient) -> None:
        self._http = http
        self._projects: dict[str, dict[str, Any]] = {}
        self._versions: dict[str, tuple[ModrinthVersion, ...]] = {}

    def get_project(self, slug: str) -> dict[str, Any]:
        key = slug.lower()
        cached = self._projects.get(key)
        if cached is not None:
            return cached
        try:
            payload = self._http.get_json(f"{_API_BASE}/project/{key}")
        except HTTPStatusError as exc:
            if exc.status_code == 404:
                raise ProviderError(
                    f"Modrinth project not found: {slug!r}",
                    hint="project must be a Modrinth project slug (e.g. coreprotect)",
                ) from exc
            raise
        if not isinstance(payload, dict):
            raise ProviderError(f"Modrinth project response has invalid structure: {slug!r}")
        actual = payload.get("slug")
        if isinstance(actual, str) and actual.lower() != key:
            raise ProviderError(
                f"Modrinth project coordinates mismatch: requested {slug!r}, got {actual!r}",
                hint="use the project slug shown on the Modrinth page",
            )
        self._projects[key] = payload
        return payload

    def get_versions(self, slug: str) -> tuple[ModrinthVersion, ...]:
        key = slug.lower()
        cached = self._versions.get(key)
        if cached is not None:
            return cached
        try:
            payload = self._http.get_json(f"{_API_BASE}/project/{key}/version")
        except HTTPStatusError as exc:
            if exc.status_code == 404:
                raise ProviderError(f"Modrinth project not found: {slug!r}") from exc
            raise
        if not isinstance(payload, list):
            raise ProviderError(
                f"Modrinth version list response has invalid structure: {slug!r}",
                hint="the upstream response contract may have changed; "
                "update the adapter instead of guessing",
            )
        versions = tuple(parsed for item in payload if (parsed := _parse_version(item)) is not None)
        if not versions:
            raise ProviderError(f"Modrinth project has no usable versions: {slug!r}")
        self._versions[key] = versions
        return versions


def select_version(
    versions: tuple[ModrinthVersion, ...],
    *,
    version_number: str,
    minecraft_version: str,
) -> ModrinthVersion:
    """Exact version_number match; the target Minecraft version must be included exactly."""
    matches = [version for version in versions if version.version_number == version_number]
    if not matches:
        available = ", ".join(version.version_number for version in versions[:8]) or "(none)"
        raise ProviderError(
            f"Modrinth version number {version_number!r} does not exist",
            hint=f"recent available versions: {available}; "
            f"latest and version ranges are not supported",
        )
    compatible = [version for version in matches if minecraft_version in version.game_versions]
    if not compatible:
        declared = sorted({game for version in matches for game in version.game_versions})
        raise ProviderError(
            f"Modrinth version {version_number!r} does not declare support "
            f"for Minecraft {minecraft_version}",
            hint=f"declared game versions: {_summarize(declared)}; "
            f"nearby versions are not assumed equivalent",
        )
    if len(compatible) > 1:
        detail = "; ".join(
            f"loaders={','.join(version.loaders) or 'none'}" for version in compatible
        )
        raise ProviderError(
            f"Modrinth version {version_number!r} has multiple entries compatible "
            f"with {minecraft_version}; cannot decide",
            hint=f"entries: {detail}",
        )
    return compatible[0]


def check_loaders(version: ModrinthVersion, *, platform: str) -> None:
    """Platform and loader compatibility check."""
    if platform != "PAPER":
        raise ProviderError(f"Modrinth provider does not support platform {platform} yet")
    if not set(version.loaders) & set(PAPER_LOADERS):
        declared = ", ".join(version.loaders) or "none"
        raise ProviderError(
            f"Modrinth version {version.version_number!r} is not usable with PAPER "
            f"(loaders: {declared})",
        )


def select_file(version: ModrinthVersion, *, selector: str | None) -> ModrinthFile:
    """File selection: exact match when asset is configured.

    Otherwise only a unique JAR candidate is allowed.
    """
    names = [file.filename for file in version.files]
    if selector is not None:
        matches = [file for file in version.files if file.filename == selector]
        if not matches:
            raise ProviderError(
                f"file {selector!r} not found in Modrinth version {version.version_number!r}",
                hint=f"available files: {_summarize(names)}",
            )
        if len(matches) > 1:
            raise ProviderError(
                f"Modrinth version has multiple files named {selector!r}; cannot decide"
            )
        chosen = matches[0]
        if not chosen.filename.lower().endswith(".jar"):
            raise ProviderError(
                f"the selected file {selector!r} is not a JAR file",
                hint="source archives and other attachments are not accepted as plugin JARs",
            )
        return chosen
    jars = [file for file in version.files if file.filename.lower().endswith(".jar")]
    if not jars:
        raise ProviderError(
            f"Modrinth version {version.version_number!r} has no usable JAR files",
            hint=f"available files: {_summarize(names)}",
        )
    if len(jars) > 1:
        raise ProviderError(
            f"Modrinth version {version.version_number!r} has multiple JAR files; "
            f"cannot choose automatically",
            hint=f"use asset to select one: {_summarize([file.filename for file in jars])}",
        )
    return jars[0]


def _parse_version(payload: Any) -> ModrinthVersion | None:
    if not isinstance(payload, dict):
        return None
    version_number = payload.get("version_number")
    if not isinstance(version_number, str) or not version_number:
        return None
    games_raw = payload.get("game_versions")
    game_versions = tuple(str(game) for game in games_raw) if isinstance(games_raw, list) else ()
    loaders_raw = payload.get("loaders")
    loaders = (
        tuple(str(loader).lower() for loader in loaders_raw)
        if isinstance(loaders_raw, list)
        else ()
    )
    version_type = payload.get("version_type")
    if not isinstance(version_type, str):
        version_type = "unknown"
    files_raw = payload.get("files")
    files = (
        [file for item in files_raw if (file := _parse_file(item)) is not None]
        if isinstance(files_raw, list)
        else []
    )
    return ModrinthVersion(
        version_number=version_number,
        game_versions=game_versions,
        loaders=loaders,
        version_type=version_type,
        files=tuple(files),
    )


def _parse_file(payload: Any) -> ModrinthFile | None:
    if not isinstance(payload, dict):
        return None
    filename = payload.get("filename")
    url = payload.get("url")
    if not isinstance(filename, str) or not filename or not isinstance(url, str) or not url:
        return None
    size = payload.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        size = None
    return ModrinthFile(
        filename=filename,
        url=url,
        size=size,
        checksum=_parse_hashes(payload.get("hashes")),
    )


def _parse_hashes(raw: Any) -> Checksum | None:
    """Prefer sha512, then sha256; sha1-only is treated as no strong checksum."""
    if not isinstance(raw, dict):
        return None
    sha512 = raw.get("sha512")
    if isinstance(sha512, str) and _SHA512_RE.fullmatch(sha512.strip()):
        return Checksum(algorithm="sha512", value=sha512.strip().lower())
    sha256 = raw.get("sha256")
    if isinstance(sha256, str) and _SHA256_RE.fullmatch(sha256.strip()):
        return Checksum(algorithm="sha256", value=sha256.strip().lower())
    return None


def _summarize(items: list[str]) -> str:
    if not items:
        return "(none)"
    return ", ".join(items)
