"""Spiget API v2 client (SpigotMC resources) and version selection logic.

Used by SpigetProvider; each resource and its version list is queried only once per run.
Spiget mirrors SpigotMC: resources are addressed by their numeric id, a version carries a
display name plus the numeric id the download endpoint needs, and the pinned download URL
is the documented ``/resources/<id>/versions/<version id>/download`` endpoint. Spiget
provides no file hashes and no per-version compatibility info (``testedVersions`` is
resource-level and declares major lines like ``1.21``), so downloads are verified
structurally by the downloader and compatibility uses prefix semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlencode

from .errors import HTTPStatusError, ProviderError
from .http_client import HTTPClient
from .models import Compatibility, SearchResult

_API_BASE = "https://api.spiget.org/v2"
_VERSIONS_PAGE_SIZE = 100
_MAX_VERSION_PAGES = 50  # defensive bound against a broken pagination contract


@dataclass(frozen=True)
class SpigetResource:
    """Resource metadata relevant to downloads and compatibility."""

    resource_id: int
    name: str
    tested_versions: tuple[str, ...]
    premium: bool
    external: bool


@dataclass(frozen=True)
class SpigetVersion:
    """One resource version: display name plus the numeric id the download endpoint needs."""

    version_id: int
    name: str
    release_date: int | None  # unix seconds


def parse_resource_id(project: str, *, field_path: str) -> str:
    """Validate a Spigot resource id (the numeric part of the resource URL)."""
    candidate = project.strip()
    if not candidate.isdigit():
        raise ProviderError(
            f"{field_path} is not a valid Spigot resource id: {project!r}",
            hint="use the numeric id from the resource URL, e.g. spiget:28140",
        )
    return candidate


def version_download_url(resource_id: str, version_id: int) -> str:
    """The documented download endpoint of an exact version."""
    return f"{_API_BASE}/resources/{resource_id}/versions/{version_id}/download"


def select_version(
    versions: tuple[SpigetVersion, ...],
    *,
    name: str,
    resource_id: str,
) -> SpigetVersion:
    """Exact version-name match; several entries with the same name cannot be decided."""
    matches = [version for version in versions if version.name == name]
    if not matches:
        available = ", ".join(version.name for version in versions[:8]) or "(none)"
        raise ProviderError(
            f"Spigot resource {resource_id} has no version {name!r}",
            hint=f"recent available versions: {available}; "
            f"latest and version ranges are not supported",
        )
    if len(matches) > 1:
        ids = ", ".join(str(version.version_id) for version in matches)
        raise ProviderError(
            f"Spigot resource {resource_id} has multiple versions named {name!r}; cannot decide",
            hint=f"matching version ids: {ids}",
        )
    return matches[0]


def check_tested_versions(
    resource: SpigetResource, *, minecraft_version: str
) -> tuple[Compatibility, str]:
    """Map the resource-level ``testedVersions`` onto the target Minecraft version.

    Spiget declares tested versions per resource as major lines ("1.21", "26.1"), so a
    declared entry covers the exact version and every patch release of that line.
    """
    tested = resource.tested_versions
    if not tested:
        return (
            Compatibility.UNKNOWN,
            "Spiget provides no tested-version info for this resource; compatibility with "
            f"Minecraft {minecraft_version} cannot be confirmed (treated as unknown)",
        )
    if any(_covers(declared, minecraft_version) for declared in tested):
        return Compatibility.COMPATIBLE, ""
    raise ProviderError(
        f"the resource's declared tested versions do not include Minecraft {minecraft_version}",
        hint=f"declared tested versions: {', '.join(tested)}; "
        f"nearby major lines are not assumed equivalent",
    )


def _covers(declared: str, minecraft_version: str) -> bool:
    """A declared line covers its exact spelling and every patch release of the line."""
    return (
        minecraft_version == declared
        or minecraft_version.startswith(declared + ".")
        or declared.startswith(minecraft_version + ".")
    )


def version_detail(version: SpigetVersion) -> str:
    """Pin-catalog detail for one version: its release date."""
    if version.release_date is None:
        return "unknown date"
    return datetime.fromtimestamp(version.release_date, UTC).strftime("%Y-%m-%d")


class SpigetClient:
    """Spiget query client with in-run caching."""

    def __init__(self, http: HTTPClient) -> None:
        self._http = http
        self._resources: dict[str, SpigetResource] = {}
        self._versions: dict[str, tuple[SpigetVersion, ...]] = {}

    def get_resource(self, resource_id: str) -> SpigetResource:
        cached = self._resources.get(resource_id)
        if cached is not None:
            return cached
        params = urlencode({"fields": "id,name,testedVersions,premium,external"})
        try:
            payload = self._http.get_json(f"{_API_BASE}/resources/{resource_id}?{params}")
        except HTTPStatusError as exc:
            if exc.status_code == 404:
                raise ProviderError(
                    f"Spigot resource not found: {resource_id}",
                    hint="check the numeric resource id; no guessing by name",
                ) from exc
            raise
        resource = _parse_resource(payload, resource_id=resource_id)
        self._resources[resource_id] = resource
        return resource

    def get_versions(self, resource_id: str) -> tuple[SpigetVersion, ...]:
        """All versions of a resource, newest first (paged, with a defensive page cap)."""
        cached = self._versions.get(resource_id)
        if cached is not None:
            return cached
        versions: list[SpigetVersion] = []
        for page in range(1, _MAX_VERSION_PAGES + 1):
            params = urlencode(
                {
                    "size": str(_VERSIONS_PAGE_SIZE),
                    "page": str(page),
                    "sort": "-releaseDate",
                    "fields": "id,name,releaseDate",
                }
            )
            payload = self._http.get_json(f"{_API_BASE}/resources/{resource_id}/versions?{params}")
            if not isinstance(payload, list):
                raise ProviderError(
                    f"Spiget version list response has invalid structure: {resource_id}",
                    hint=(
                        "the upstream response contract may have changed; "
                        "update the adapter instead of guessing"
                    ),
                )
            versions.extend(
                parsed for item in payload if (parsed := _parse_version(item)) is not None
            )
            if len(payload) < _VERSIONS_PAGE_SIZE:
                break
        else:
            raise ProviderError(
                f"Spigot resource {resource_id} has more than "
                f"{_MAX_VERSION_PAGES * _VERSIONS_PAGE_SIZE} versions",
                hint="the version list is paged; the upstream response contract may have changed",
            )
        if not versions:
            raise ProviderError(f"Spigot resource {resource_id} has no versions")
        result = tuple(versions)
        self._versions[resource_id] = result
        return result

    def search_resources(self, query: str, *, limit: int) -> tuple[SearchResult, ...]:
        """Search resources by name, most downloaded first."""
        params = urlencode(
            {
                "field": "name",
                "size": str(limit),
                "sort": "-downloads",
                "fields": "id,name,tag,downloads",
            }
        )
        payload = self._http.get_json(
            f"{_API_BASE}/search/resources/{quote(query, safe='')}?{params}"
        )
        if not isinstance(payload, list):
            raise ProviderError(
                "Spiget search response has invalid structure (expected array)",
                hint=(
                    "the upstream response contract may have changed; "
                    "update the adapter instead of guessing"
                ),
            )
        results: list[SearchResult] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            resource_id = item.get("id")
            name = item.get("name")
            if (
                isinstance(resource_id, bool)
                or not isinstance(resource_id, int)
                or not isinstance(name, str)
                or not name
            ):
                continue
            downloads = item.get("downloads")
            results.append(
                SearchResult(
                    provider="spiget",
                    project=str(resource_id),
                    name=name,
                    description=_one_line(item.get("tag")),
                    downloads=downloads if isinstance(downloads, int) else None,
                )
            )
        return tuple(results)


def _parse_resource(payload: Any, *, resource_id: str) -> SpigetResource:
    if not isinstance(payload, dict):
        raise ProviderError(
            f"Spiget resource response has invalid structure: {resource_id}",
            hint=(
                "the upstream response contract may have changed; "
                "update the adapter instead of guessing"
            ),
        )
    actual_id = payload.get("id")
    if isinstance(actual_id, bool) or not isinstance(actual_id, int):
        raise ProviderError(f"Spiget resource response is missing the numeric id: {resource_id}")
    if actual_id != int(resource_id):
        raise ProviderError(
            f"Spiget resource mismatch: requested {resource_id}, got id {actual_id}",
            hint="the resource id must match exactly; no aliasing is assumed",
        )
    name = payload.get("name")
    if not isinstance(name, str) or not name:
        raise ProviderError(f"Spiget resource response is missing the name: {resource_id}")
    tested_raw = payload.get("testedVersions")
    tested = (
        tuple(item for item in tested_raw if isinstance(item, str))
        if isinstance(tested_raw, list)
        else ()
    )
    return SpigetResource(
        resource_id=actual_id,
        name=name,
        tested_versions=tested,
        premium=payload.get("premium") is True,
        external=payload.get("external") is True,
    )


def _parse_version(payload: Any) -> SpigetVersion | None:
    if not isinstance(payload, dict):
        return None
    version_id = payload.get("id")
    name = payload.get("name")
    if (
        isinstance(version_id, bool)
        or not isinstance(version_id, int)
        or not isinstance(name, str)
        or not name
    ):
        return None
    release_date = payload.get("releaseDate")
    if isinstance(release_date, bool) or not isinstance(release_date, int) or release_date < 0:
        release_date = None
    return SpigetVersion(version_id=version_id, name=name, release_date=release_date)


def _one_line(value: Any) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""
