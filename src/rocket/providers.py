"""Providers: Fill (Paper), Hangar, GitHub.

Scope: query the declared project and exact version; return download entries and metadata;
never treat an arbitrary web page as a JAR and never select other versions.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, urlencode

from .config import PaperConfig, PluginConfig
from .context import PipelineContext
from .errors import HTTPStatusError, ProviderError
from .github import parse_project
from .http_client import HTTPClient
from .models import (
    ArtifactKind,
    Checksum,
    Compatibility,
    DownloadEntry,
    EntryKind,
    GitHubCoordinates,
    ReleaseInfo,
    SearchResult,
)
from .modrinth import check_loaders, parse_slug, select_file, select_version

_FILL_BASE = "https://fill.papermc.io/v3"
_HANGAR_BASE = "https://hangar.papermc.io/api/v1"
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")


class PluginProvider:
    """Base class for plugin providers."""

    name = "plugin"

    def __init__(self, context: PipelineContext) -> None:
        self.context = context

    def resolve(self, spec: PluginConfig) -> ReleaseInfo:
        raise NotImplementedError


class FillPaperProvider:
    """Official Paper Fill API v3."""

    name = "fill"

    def __init__(self, context: PipelineContext) -> None:
        self.context = context

    def resolve(self, paper: PaperConfig) -> ReleaseInfo:
        versions = fetch_paper_versions(self.context.http)
        all_releases = {member for members in versions.values() for member in members}
        if paper.version not in all_releases:
            if paper.version in versions:
                raise ProviderError(
                    f"paper.version={paper.version!r} is a version group, "
                    f"not a downloadable release",
                    hint=f"group contains: {', '.join(versions[paper.version]) or '(empty)'}",
                )
            known = ", ".join(sorted(versions)) or "(none)"
            raise ProviderError(
                f"Paper release {paper.version!r} does not exist",
                hint=f"known version groups: {known}",
            )

        builds = self._fetch_builds(paper.version)
        build = self._select_build(builds, paper)
        entry = self._build_entry(build)
        build_id = build.get("id")
        channel = build.get("channel")
        self.context.log.info(
            "[paper] selected release %s, build %s (%s)",
            paper.version,
            build_id,
            channel,
        )
        return ReleaseInfo(
            artifact_id="paper",
            kind=ArtifactKind.PAPER,
            provider=self.name,
            project="paper",
            version=paper.version,
            entry=entry,
            platform="PAPER",
            compatibility=Compatibility.COMPATIBLE,
            detail=f"build {build_id}",
            build=build_id if isinstance(build_id, int) else None,
            source_chain=("fill",),
        )

    def _fetch_builds(self, version: str) -> list[dict[str, Any]]:
        url = f"{_FILL_BASE}/projects/paper/versions/{quote(version, safe='')}/builds"
        payload = self.context.http.get_json(url)
        _raise_on_api_error(payload, context=f"build list for version {version}")
        if not isinstance(payload, list):
            raise ProviderError(
                f"Fill build list response has invalid structure (version {version}, "
                f"expected array)",
                hint=(
                    "the upstream response contract may have changed; "
                    "update the adapter instead of guessing"
                ),
            )
        return [
            item
            for item in payload
            if isinstance(item, dict)
            and isinstance(item.get("id"), int)
            and not isinstance(item.get("id"), bool)
        ]

    def _select_build(self, builds: list[dict[str, Any]], paper: PaperConfig) -> dict[str, Any]:
        stable = [build for build in builds if build.get("channel") == "STABLE"]
        if paper.build is not None:
            for build in builds:
                if build.get("id") == paper.build and build.get("channel") != "STABLE":
                    channel = build.get("channel")
                    raise ProviderError(
                        f"build {paper.build} exists but is not on the STABLE channel "
                        f"(channel: {channel!r})",
                        hint=(
                            "an explicit build is used as-is; there is no fallback to other builds"
                        ),
                    )
            matches = [build for build in stable if build.get("id") == paper.build]
            if not matches:
                available = ", ".join(str(build.get("id")) for build in stable) or "none"
                raise ProviderError(
                    f"version {paper.version} has no stable build {paper.build}",
                    hint=(
                        f"stable build ids for this version: {available}; "
                        f"no fallback to other builds"
                    ),
                )
            return matches[0]
        if not stable:
            channels = ", ".join(sorted({str(build.get("channel")) for build in builds})) or "none"
            raise ProviderError(
                f"version {paper.version} has no STABLE builds (channels present: {channels})",
                hint=(
                    "experimental builds are not used and there is no fallback "
                    "to other Minecraft versions"
                ),
            )
        return max(stable, key=lambda build: build["id"])

    def _build_entry(self, build: dict[str, Any]) -> DownloadEntry:
        build_id = build.get("id")
        downloads = build.get("downloads")
        server = downloads.get("server:default") if isinstance(downloads, dict) else None
        if not isinstance(server, dict):
            raise ProviderError(
                f"build {build_id} is missing downloads['server:default']",
                hint=(
                    "the upstream response contract may have changed; "
                    "no download URL will be invented"
                ),
            )
        url = server.get("url")
        filename = server.get("name")
        if not isinstance(url, str) or not url:
            raise ProviderError(f"build {build_id} is missing a download URL")
        if not isinstance(filename, str) or not filename:
            raise ProviderError(f"build {build_id} is missing an upstream filename")
        checksums = server.get("checksums")
        sha256 = checksums.get("sha256") if isinstance(checksums, dict) else None
        if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
            raise ProviderError(
                f"build {build_id} has no usable SHA-256 checksum",
                hint=(
                    "official Paper downloads include checksums.sha256; "
                    "the response contract may have changed"
                ),
            )
        size = server.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            size = None
        return DownloadEntry(
            kind=EntryKind.DIRECT,
            url=url,
            origin=self.name,
            filename=filename,
            size=size,
            checksum=Checksum(algorithm="sha256", value=sha256.lower()),
        )


def fetch_paper_versions(http: HTTPClient) -> dict[str, list[str]]:
    """Fetch the Fill version-group mapping (group -> member releases)."""
    payload = http.get_json(f"{_FILL_BASE}/projects/paper")
    if not isinstance(payload, dict):
        raise ProviderError("Fill project response has invalid structure (expected object)")
    _raise_on_api_error(payload, context="project")
    versions = payload.get("versions")
    if not isinstance(versions, dict):
        raise ProviderError(
            "Fill project response is missing the versions field",
            hint=(
                "the upstream response contract may have changed; "
                "update the adapter instead of guessing"
            ),
        )
    result: dict[str, list[str]] = {}
    for group, members in versions.items():
        if isinstance(members, list):
            result[str(group)] = [member for member in members if isinstance(member, str)]
    if not result:
        raise ProviderError("Fill project response has no usable version groups")
    return result


def fetch_newest_stable_build(http: HTTPClient, version: str) -> int | None:
    """The highest STABLE build id for an exact Paper release; None when there is none."""
    url = f"{_FILL_BASE}/projects/paper/versions/{quote(version, safe='')}/builds"
    payload = http.get_json(url)
    _raise_on_api_error(payload, context=f"build list for version {version}")
    if not isinstance(payload, list):
        raise ProviderError(
            f"Fill build list response has invalid structure (version {version}, expected array)",
            hint=(
                "the upstream response contract may have changed; "
                "update the adapter instead of guessing"
            ),
        )
    stable_ids = [
        item["id"]
        for item in payload
        if isinstance(item, dict)
        and isinstance(item.get("id"), int)
        and not isinstance(item.get("id"), bool)
        and item.get("channel") == "STABLE"
    ]
    return max(stable_ids) if stable_ids else None


class HangarProvider(PluginProvider):
    """Hangar API v1."""

    name = "hangar"

    def resolve(self, spec: PluginConfig) -> ReleaseInfo:
        owner, slug = parse_project(spec.project, field_path=f"plugins.{spec.id}.project")
        self._verify_namespace(spec, owner=owner, slug=slug)
        version = self._fetch_version(spec, slug)
        entry = self._select_entry(spec, version)
        compatibility, note = _check_platform_dependencies(
            version,
            platform=spec.platform,
            minecraft_version=self.context.minecraft_version,
        )
        if compatibility is Compatibility.UNKNOWN:
            self.context.log.warning("[%s] warning: %s", spec.id, note)
        required = _required_dependencies(version, spec.platform)
        if required:
            self.context.log.warning(
                "[%s] warning: this version declares a required dependency %s "
                "(not auto-installed by this tool)",
                spec.id,
                ", ".join(required),
            )
        return ReleaseInfo(
            artifact_id=spec.id,
            kind=ArtifactKind.PLUGIN,
            provider=self.name,
            project=spec.project,
            version=spec.version,
            entry=entry,
            asset=spec.asset,
            platform=spec.platform,
            compatibility=compatibility,
            compatibility_note=note,
            source_chain=("hangar",),
        )

    def _verify_namespace(self, spec: PluginConfig, *, owner: str, slug: str) -> None:
        url = f"{_HANGAR_BASE}/projects/{quote(slug, safe='')}"
        try:
            project = self.context.http.get_json(url)
        except HTTPStatusError as exc:
            if exc.status_code == 404:
                raise ProviderError(
                    f"Hangar project not found: {spec.project}",
                    hint="check owner/slug; no guessing by name",
                ) from exc
            raise
        if not isinstance(project, dict):
            raise ProviderError(f"Hangar project response has invalid structure: {spec.project}")
        namespace = project.get("namespace")
        ns_owner = namespace.get("owner") if isinstance(namespace, dict) else None
        ns_slug = namespace.get("slug") if isinstance(namespace, dict) else None
        if not isinstance(ns_owner, str) or not isinstance(ns_slug, str):
            raise ProviderError(
                f"Hangar project response is missing namespace.owner/namespace.slug: {spec.project}"
            )
        if ns_owner.lower() != owner.lower() or ns_slug.lower() != slug.lower():
            raise ProviderError(
                f"Hangar project mismatch: configured {owner}/{slug}, actual {ns_owner}/{ns_slug}",
                hint=(
                    "project must match the Hangar namespace.owner/namespace.slug; "
                    "downloads will not continue with a same-named project"
                ),
            )

    def _fetch_version(self, spec: PluginConfig, slug: str) -> dict[str, Any]:
        url = (
            f"{_HANGAR_BASE}/projects/{quote(slug, safe='')}"
            f"/versions/{quote(spec.version, safe='')}"
        )
        try:
            version = self.context.http.get_json(url)
        except HTTPStatusError as exc:
            if exc.status_code == 404:
                raise ProviderError(
                    f"Hangar version not found: {spec.project} {spec.version!r}",
                    hint=(
                        "the version must exist exactly; "
                        "latest and version ranges are not supported"
                    ),
                ) from exc
            raise
        if not isinstance(version, dict):
            raise ProviderError(
                f"Hangar version response has invalid structure: {spec.project} {spec.version!r}"
            )
        name = version.get("name")
        if name != spec.version:
            raise ProviderError(
                f"Hangar returned a different version name: "
                f"requested {spec.version!r}, got {name!r}",
            )
        return version

    def _select_entry(self, spec: PluginConfig, version: dict[str, Any]) -> DownloadEntry:
        downloads = version.get("downloads")
        platform_download = downloads.get(spec.platform) if isinstance(downloads, dict) else None
        if not isinstance(platform_download, dict):
            raise ProviderError(
                f"version {spec.version} has no download entry for platform {spec.platform}",
                hint="check whether this release publishes artifacts for the platform",
            )
        download_url = platform_download.get("downloadUrl")
        external_url = platform_download.get("externalUrl")
        file_info = platform_download.get("fileInfo")
        file_info = file_info if isinstance(file_info, dict) else {}

        if isinstance(download_url, str) and download_url:
            filename = file_info.get("name")
            filename = filename if isinstance(filename, str) and filename else None
            if spec.asset is not None:
                if filename is None:
                    raise ProviderError(
                        f"asset={spec.asset!r} is configured but Hangar provided "
                        f"no hosted filename to verify",
                        hint="remove asset or use a source that provides filenames",
                    )
                if filename != spec.asset:
                    raise ProviderError(
                        f"asset={spec.asset!r} does not match the Hangar hosted "
                        f"filename {filename!r}",
                        hint="asset must exactly match the upstream filename",
                    )
            checksum: Checksum | None = None
            sha256_raw = file_info.get("sha256Hash")
            if isinstance(sha256_raw, str) and sha256_raw:
                if _SHA256_RE.fullmatch(sha256_raw):
                    checksum = Checksum(algorithm="sha256", value=sha256_raw.lower())
                else:
                    self.context.log.warning(
                        "[%s] warning: Hangar fileInfo.sha256Hash is not a valid "
                        "SHA-256; proceeding without an upstream hash",
                        spec.id,
                    )
            size = file_info.get("sizeBytes")
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                size = None
            return DownloadEntry(
                kind=EntryKind.DIRECT,
                url=download_url,
                origin=self.name,
                filename=filename,
                size=size,
                checksum=checksum,
            )

        if isinstance(external_url, str) and external_url:
            self.context.log.info("[%s] external entry: %s", spec.id, external_url)
            return DownloadEntry(kind=EntryKind.EXTERNAL, url=external_url, origin=self.name)

        raise ProviderError(
            f"version {spec.version} has no download entry "
            f"(both downloadUrl and externalUrl are empty)",
            hint="no download URL will be invented when the entry is missing",
        )


def search_hangar_projects(http: HTTPClient, query: str, *, limit: int) -> tuple[SearchResult, ...]:
    """Search Hangar projects by keyword, most downloaded first."""
    params = urlencode({"query": query, "limit": str(limit), "offset": "0", "sort": "-downloads"})
    payload = http.get_json(f"{_HANGAR_BASE}/projects?{params}")
    if not isinstance(payload, dict):
        raise ProviderError("Hangar search response has invalid structure (expected object)")
    items = payload.get("result")
    if not isinstance(items, list):
        raise ProviderError(
            "Hangar search response is missing the result field",
            hint=(
                "the upstream response contract may have changed; "
                "update the adapter instead of guessing"
            ),
        )
    results: list[SearchResult] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        namespace = item.get("namespace")
        owner = namespace.get("owner") if isinstance(namespace, dict) else None
        slug = namespace.get("slug") if isinstance(namespace, dict) else None
        name = item.get("name")
        if (
            not isinstance(owner, str)
            or not owner
            or not isinstance(slug, str)
            or not slug
            or not isinstance(name, str)
            or not name
        ):
            continue
        stats = item.get("stats")
        downloads = stats.get("downloads") if isinstance(stats, dict) else None
        results.append(
            SearchResult(
                provider="hangar",
                project=f"{owner}/{slug}",
                name=name,
                description=_one_line(item.get("description")),
                downloads=downloads if isinstance(downloads, int) else None,
            )
        )
    return tuple(results)


class GitHubProvider(PluginProvider):
    """GitHub Releases REST API (exact tag queries)."""

    name = "github"

    def resolve(self, spec: PluginConfig) -> ReleaseInfo:
        owner, repo = parse_project(spec.project, field_path=f"plugins.{spec.id}.project")
        release = self.context.github.get_release(owner, repo, spec.version)
        note = "GitHub Releases provide no Minecraft compatibility info (treated as unknown)"
        self.context.log.warning("[%s] warning: %s", spec.id, note)
        entry = DownloadEntry(
            kind=EntryKind.GITHUB_RELEASE,
            url=release.html_url,
            origin=self.name,
            github=GitHubCoordinates(owner=owner, repo=repo, tag=spec.version),
        )
        return ReleaseInfo(
            artifact_id=spec.id,
            kind=ArtifactKind.PLUGIN,
            provider=self.name,
            project=spec.project,
            version=spec.version,
            entry=entry,
            asset=spec.asset,
            platform=spec.platform,
            compatibility=Compatibility.UNKNOWN,
            compatibility_note=note,
            source_chain=("github",),
        )


class ModrinthProvider(PluginProvider):
    """Modrinth API v2 (exact version_number and exact file)."""

    name = "modrinth"

    def resolve(self, spec: PluginConfig) -> ReleaseInfo:
        slug = parse_slug(spec.project, field_path=f"plugins.{spec.id}.project")
        self.context.modrinth.get_project(slug)
        versions = self.context.modrinth.get_versions(slug)
        version = select_version(
            versions,
            version_number=spec.version,
            minecraft_version=self.context.minecraft_version,
        )
        check_loaders(version, platform=spec.platform)
        file = select_file(version, selector=spec.asset)
        if file.checksum is None:
            self.context.log.warning(
                "[%s] warning: Modrinth provided no SHA-512/SHA-256; "
                "proceeding without an upstream hash",
                spec.id,
            )
        self.context.log.info(
            "[%s] selected Modrinth version %s, file %s (%s)",
            spec.id,
            version.version_number,
            file.filename,
            version.version_type,
        )
        entry = DownloadEntry(
            kind=EntryKind.DIRECT,
            url=file.url,
            origin=self.name,
            filename=file.filename,
            size=file.size,
            checksum=file.checksum,
        )
        return ReleaseInfo(
            artifact_id=spec.id,
            kind=ArtifactKind.PLUGIN,
            provider=self.name,
            project=spec.project,
            version=spec.version,
            entry=entry,
            asset=spec.asset,
            platform=spec.platform,
            compatibility=Compatibility.COMPATIBLE,
            source_chain=("modrinth",),
        )


PROVIDERS: dict[str, type[PluginProvider]] = {
    HangarProvider.name: HangarProvider,
    GitHubProvider.name: GitHubProvider,
    ModrinthProvider.name: ModrinthProvider,
}


def _raise_on_api_error(payload: Any, *, context: str) -> None:
    """Fill error objects look like {"ok": false, "error": ..., "message": ...}."""
    if isinstance(payload, dict) and payload.get("ok") is False:
        detail = payload.get("message") or payload.get("error") or "unknown error"
        raise ProviderError(f"Fill API returned an error ({context}): {detail}")


def _check_platform_dependencies(
    version: dict[str, Any],
    *,
    platform: str,
    minecraft_version: str,
) -> tuple[Compatibility, str]:
    dependencies = version.get("platformDependencies")
    if not isinstance(dependencies, dict) or platform not in dependencies:
        return (
            Compatibility.UNKNOWN,
            f"upstream provides no {platform} compatibility info; "
            f"compatibility with Minecraft {minecraft_version} "
            f"cannot be confirmed (treated as unknown)",
        )
    listed = dependencies.get(platform)
    if not isinstance(listed, list):
        return (
            Compatibility.UNKNOWN,
            f"upstream {platform} compatibility info is malformed (treated as unknown)",
        )
    declared = {str(item) for item in listed}
    if minecraft_version in declared:
        return Compatibility.COMPATIBLE, ""
    raise ProviderError(
        f"the version's declared {platform} compatibility does not include "
        f"Minecraft {minecraft_version}",
        hint=(
            f"declared supported versions: {_summarize_versions(declared)}; "
            f"nearby patch versions are not assumed equivalent"
        ),
    )


def _required_dependencies(version: dict[str, Any], platform: str) -> list[str]:
    dependencies = version.get("pluginDependencies")
    if not isinstance(dependencies, dict):
        return []
    entries = dependencies.get(platform)
    if not isinstance(entries, list):
        return []
    names: list[str] = []
    for entry in entries:
        if isinstance(entry, dict):
            name = entry.get("name")
            if isinstance(name, str) and entry.get("required") is True:
                names.append(name)
        elif isinstance(entry, str):
            names.append(entry)
    return names


def _summarize_versions(versions: set[str]) -> str:
    ordered = sorted(versions)
    if not ordered:
        return "(empty)"
    if len(ordered) > 20:
        return ", ".join(ordered[:20]) + f" ... ({len(ordered)} total)"
    return ", ".join(ordered)


def _one_line(value: Any) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""
