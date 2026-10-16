"""Providers: Fill (Paper), Hangar, GitHub, Modrinth, Spiget.

Scope: query the declared project and exact version; return download entries and metadata;
never treat an arbitrary web page as a JAR and never select other versions.

Every plugin provider implements the trait surface declared on :class:`PluginProvider` —
coordinate validation, exact-version resolution, keyword search, newest-version
recommendation and the pin catalogs — and registers in ``PROVIDERS``. The CLI dispatches
through the registry and keeps no per-provider branches, so a new provider is a new class
here plus one registry entry.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, urlencode

from .catalog import version_forms
from .config import PaperConfig, PluginConfig
from .context import PipelineContext, ProviderClients
from .errors import HTTPStatusError, ProviderError, RocketError
from .github import (
    GitHubRelease,
    release_asset_name,
    release_for_external_link,
)
from .github import (
    parse_project as _parse_owner_name,
)
from .http_client import HTTPClient
from .models import (
    ArtifactKind,
    Checksum,
    Compatibility,
    DownloadEntry,
    EntryKind,
    GitHubCoordinates,
    RecommendedPlugin,
    ReleaseInfo,
    SearchResult,
)
from .modrinth import (
    PAPER_LOADERS,
    ModrinthFile,
    ModrinthVersion,
    check_loaders,
    parse_slug,
    select_file,
    select_version,
)
from .spiget import (
    SpigetResource,
    check_tested_versions,
    parse_resource_id,
    version_detail,
    version_download_url,
)
from .spiget import (
    select_version as select_spiget_version,
)

_FILL_BASE = "https://fill.papermc.io/v3"
_HANGAR_BASE = "https://hangar.papermc.io/api/v1"
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")

_HANGAR_VERSION_LIMIT = 10
_PIN_CATALOG_LIMIT = 30  # select prompts stay navigable
_GITHUB_NOTE = "GitHub Releases provide no Minecraft compatibility info (treated as unknown)"


class PluginProvider:
    """Base class for plugin providers: the full operation surface the CLI dispatches on.

    Instance methods share the per-run clients; the staticmethods are pure and need no
    client. The remaining provider asymmetry is declared as capabilities, not hidden in
    call-site branches: ``supports_search`` marks keyword search, and ``upgrade_note``
    marks pins that ``rocket upgrade`` does not check (the note is printed instead).
    """

    name = "plugin"
    supports_search = False
    upgrade_note: str | None = None

    def __init__(self, context: ProviderClients) -> None:
        self.context = context

    @staticmethod
    def parse_project(project: str, *, field_path: str) -> str:
        """Validate the project coordinate; return its canonical form."""
        raise NotImplementedError

    @staticmethod
    def page_url(project: str) -> str:
        """Public project page linked from search output (searchable providers only)."""
        raise NotImplementedError

    def resolve(self, spec: PluginConfig, *, minecraft_version: str) -> ReleaseInfo:
        """Resolve the declared exact version to a download entry."""
        raise NotImplementedError

    def search(self, query: str, *, limit: int) -> tuple[SearchResult, ...]:
        """Search projects by keyword, most downloaded first."""
        raise NotImplementedError

    def recommend(
        self,
        project: str,
        *,
        templates: tuple[str, ...],
        minecraft_version: str,
        plugin_id: str,
    ) -> RecommendedPlugin:
        """Pick the newest version the download pipeline can use exactly, and pin it.

        ``templates`` holds ``{version}`` filename templates for sources where the exact
        attachment has to be picked from a release page.
        """
        raise NotImplementedError

    def list_versions(self, project: str, *, minecraft_version: str) -> list[tuple[str, str]]:
        """(version, detail) pairs usable with the target Minecraft version, newest first."""
        raise NotImplementedError

    def file_choices(self, project: str, *, version: str) -> tuple[str | None, list[str]]:
        """(asset, choices) for one exact version: a unique JAR is used directly, several
        JARs are returned as choices, sources with unnamed files pin no asset."""
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
    ids = fetch_stable_build_ids(http, version)
    return ids[0] if ids else None


def fetch_stable_build_ids(http: HTTPClient, version: str) -> list[int]:
    """All STABLE build ids for an exact Paper release, highest first."""
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
    return sorted(stable_ids, reverse=True)


class HangarProvider(PluginProvider):
    """Hangar API v1."""

    name = "hangar"
    supports_search = True

    def __init__(self, context: ProviderClients) -> None:
        super().__init__(context)
        # a project shared by several catalog entries (the EssentialsX module group)
        # is resolved only once per run, failures included
        self._recommended: dict[str, tuple[str, dict[str, Any]] | RocketError] = {}

    @staticmethod
    def parse_project(project: str, *, field_path: str) -> str:
        _parse_owner_name(project, field_path=field_path)
        return project

    @staticmethod
    def page_url(project: str) -> str:
        return f"https://hangar.papermc.io/{project}"

    def resolve(self, spec: PluginConfig, *, minecraft_version: str) -> ReleaseInfo:
        owner, slug = _parse_owner_name(spec.project, field_path=f"plugins.{spec.id}.project")
        self._verify_namespace(spec, owner=owner, slug=slug)
        version = self._fetch_version(spec, slug)
        entry = self._select_entry(spec, version)
        compatibility, note = _check_platform_dependencies(
            version,
            platform=spec.platform,
            minecraft_version=minecraft_version,
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

    def search(self, query: str, *, limit: int) -> tuple[SearchResult, ...]:
        params = urlencode(
            {"query": query, "limit": str(limit), "offset": "0", "sort": "-downloads"}
        )
        payload = self.context.http.get_json(f"{_HANGAR_BASE}/projects?{params}")
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
                    provider=self.name,
                    project=f"{owner}/{slug}",
                    name=name,
                    description=_one_line(item.get("description")),
                    downloads=downloads if isinstance(downloads, int) else None,
                )
            )
        return tuple(results)

    def recommend(
        self,
        project: str,
        *,
        templates: tuple[str, ...],
        minecraft_version: str,
        plugin_id: str,
    ) -> RecommendedPlugin:
        version, paper_download = self._recommended_version(
            project, minecraft_version=minecraft_version
        )
        download_url = paper_download.get("downloadUrl")
        external_url = paper_download.get("externalUrl")
        if isinstance(download_url, str) and download_url:
            file_info = paper_download.get("fileInfo")
            hosted_name = file_info.get("name") if isinstance(file_info, dict) else None
            wanted = [template.replace("{version}", version) for template in templates]
            hosted = hosted_name if isinstance(hosted_name, str) and hosted_name else None
            if not wanted or hosted in wanted:
                return RecommendedPlugin(
                    id=plugin_id, provider=self.name, project=project, version=version
                )
            if hosted is not None:
                raise ProviderError(
                    f"asset does not match the Hangar hosted filename {hosted!r} "
                    f"for {project} {version}"
                )
            raise ProviderError(
                f"asset is declared but Hangar provided no hosted filename to verify "
                f"for {project} {version}"
            )
        if isinstance(external_url, str) and external_url:
            release = release_for_external_link(self.context.github, external_url)
            asset = release_asset_name(release, version=version, templates=templates)
            return RecommendedPlugin(
                id=plugin_id,
                provider=self.name,
                project=project,
                version=version,
                asset=asset,
            )
        raise ProviderError(f"Hangar version {version} of {project} has no PAPER download entry")

    def list_versions(self, project: str, *, minecraft_version: str) -> list[tuple[str, str]]:
        """(version name, channel), newest first, all channels; only versions declaring
        the target platform version are listed."""
        _, slug = _parse_owner_name(project, field_path="project")
        quoted_slug = quote(slug, safe="")
        params = urlencode(
            {
                "platform": "PAPER",
                "platformVersion": minecraft_version,
                "limit": str(_PIN_CATALOG_LIMIT),
            }
        )
        payload = _hangar_get_or_not_found(
            self.context.http,
            f"{_HANGAR_BASE}/projects/{quoted_slug}/versions?{params}",
            project=project,
        )
        if not isinstance(payload, dict):
            raise ProviderError(f"Hangar version list has an invalid structure: {project}")
        results = payload.get("result")
        if not isinstance(results, list):
            raise ProviderError(
                f"Hangar version list is missing the result field: {project}",
                hint=(
                    "the upstream response contract may have changed; "
                    "update the adapter instead of guessing"
                ),
            )
        versions: list[tuple[str, str]] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            channel = _channel_name(item.get("channel"))
            if isinstance(name, str) and name and channel is not None:
                versions.append((name, channel))
        if not versions:
            raise ProviderError(
                f"Hangar project {project} has no version for Minecraft {minecraft_version}",
                hint="pin another Paper version first (rocket pin core), or edit the pin by hand",
            )
        return versions

    def file_choices(self, project: str, *, version: str) -> tuple[str | None, list[str]]:
        """(asset, choices): a hosted file pins no asset; an external link lists the
        attachments of the GitHub Release it points at."""
        _, slug = _parse_owner_name(project, field_path="project")
        quoted_slug = quote(slug, safe="")
        detail_url = f"{_HANGAR_BASE}/projects/{quoted_slug}/versions/{quote(version, safe='')}"
        detail = _hangar_get_or_not_found(self.context.http, detail_url, project=project)
        if not isinstance(detail, dict):
            raise ProviderError(
                f"Hangar version response has an invalid structure: {project} {version}"
            )
        downloads = detail.get("downloads")
        paper = downloads.get("PAPER") if isinstance(downloads, dict) else None
        if not isinstance(paper, dict):
            raise ProviderError(
                f"Hangar version {version} of {project} has no PAPER download entry"
            )
        download_url = paper.get("downloadUrl")
        if isinstance(download_url, str) and download_url:
            return None, []  # a hosted file is a single choice; no asset is pinned
        external_url = paper.get("externalUrl")
        if isinstance(external_url, str) and external_url:
            release = release_for_external_link(self.context.github, external_url)
            return _file_choices(
                [asset.name for asset in release.assets],
                context=f"release {release.owner}/{release.repo}@{release.tag}",
            )
        raise ProviderError(
            f"Hangar version {version} of {project} has no download entry "
            "(both downloadUrl and externalUrl are empty)"
        )

    def _recommended_version(
        self, project: str, *, minecraft_version: str
    ) -> tuple[str, dict[str, Any]]:
        cached = self._recommended.get(project)
        if isinstance(cached, RocketError):
            raise cached
        if cached is not None:
            return cached
        try:
            resolved = self._newest_release_version(project, minecraft_version=minecraft_version)
        except RocketError as exc:
            self._recommended[project] = exc
            raise
        self._recommended[project] = resolved
        return resolved

    def _newest_release_version(
        self, project: str, *, minecraft_version: str
    ) -> tuple[str, dict[str, Any]]:
        """Newest Release-channel version declaring PAPER support, plus its PAPER download
        entry."""
        _, slug = _parse_owner_name(project, field_path="project")
        quoted_slug = quote(slug, safe="")
        params = urlencode(
            {
                "platform": "PAPER",
                "platformVersion": minecraft_version,
                "channel": "Release",
                "limit": str(_HANGAR_VERSION_LIMIT),
            }
        )
        payload = _hangar_get_or_not_found(
            self.context.http,
            f"{_HANGAR_BASE}/projects/{quoted_slug}/versions?{params}",
            project=project,
        )
        if not isinstance(payload, dict):
            raise ProviderError(f"Hangar version list has an invalid structure: {project}")
        results = payload.get("result")
        if not isinstance(results, list):
            raise ProviderError(
                f"Hangar version list is missing the result field: {project}",
                hint="the upstream response contract may have changed; "
                "update the adapter instead of guessing",
            )
        release_names: list[str] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if isinstance(name, str) and name and _channel_name(item.get("channel")) == "Release":
                release_names.append(name)
        if not release_names:
            raise ProviderError(
                f"Hangar project {project} has no Release-channel version for "
                f"Minecraft {minecraft_version}",
                hint="newer Minecraft releases are sometimes only covered by snapshot builds; "
                "pick another Paper version or add this plugin manually",
            )

        version = release_names[0]
        detail_url = f"{_HANGAR_BASE}/projects/{quoted_slug}/versions/{quote(version, safe='')}"
        detail = _hangar_get_or_not_found(self.context.http, detail_url, project=project)
        if not isinstance(detail, dict):
            raise ProviderError(
                f"Hangar version response has an invalid structure: {project} {version}"
            )
        _check_declared_platform(
            detail, project=project, version=version, minecraft_version=minecraft_version
        )
        downloads = detail.get("downloads")
        paper = downloads.get("PAPER") if isinstance(downloads, dict) else None
        if not isinstance(paper, dict):
            raise ProviderError(
                f"Hangar version {version} of {project} has no PAPER download entry"
            )
        return version, paper

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


class GitHubProvider(PluginProvider):
    """GitHub Releases REST API (exact tag queries)."""

    name = "github"
    upgrade_note = "pins an exact tag; bump it by hand or re-run rocket add"

    @staticmethod
    def parse_project(project: str, *, field_path: str) -> str:
        _parse_owner_name(project, field_path=field_path)
        return project

    def resolve(self, spec: PluginConfig, *, minecraft_version: str) -> ReleaseInfo:
        owner, repo = _parse_owner_name(spec.project, field_path=f"plugins.{spec.id}.project")
        release = self.context.github.get_release(owner, repo, spec.version)
        note = _GITHUB_NOTE
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

    def recommend(
        self,
        project: str,
        *,
        templates: tuple[str, ...],
        minecraft_version: str,
        plugin_id: str,
    ) -> RecommendedPlugin:
        release, asset = self._newest_usable_release(project, templates=templates)
        return RecommendedPlugin(
            id=plugin_id,
            provider=self.name,
            project=project,
            version=release.tag,
            asset=asset,
            note=_GITHUB_NOTE,
        )

    def list_versions(self, project: str, *, minecraft_version: str) -> list[tuple[str, str]]:
        """Release tags, newest first (drafts and prereleases excluded); GitHub has no
        Minecraft compatibility metadata, so every release is listed."""
        owner, repo = _parse_owner_name(project, field_path="project")
        tags = [
            release.tag
            for release in self.context.github.get_releases(owner, repo)
            if not release.draft and not release.prerelease
        ]
        if not tags:
            raise ProviderError(f"GitHub project {project} has no non-prerelease releases")
        return [(tag, "release") for tag in tags]

    def file_choices(self, project: str, *, version: str) -> tuple[str | None, list[str]]:
        owner, repo = _parse_owner_name(project, field_path="project")
        release = self.context.github.get_release(owner, repo, version)
        return _file_choices(
            [asset.name for asset in release.assets],
            context=f"release {release.owner}/{release.repo}@{release.tag}",
        )

    def _newest_usable_release(
        self, project: str, *, templates: tuple[str, ...]
    ) -> tuple[GitHubRelease, str]:
        """Newest non-draft, non-prerelease release that has the wanted JAR attachment."""
        owner, repo = _parse_owner_name(project, field_path="project")
        for release in self.context.github.get_releases(owner, repo):
            if release.draft or release.prerelease:
                continue
            try:
                asset = release_asset_name(release, version=release.tag, templates=templates)
            except ProviderError:
                continue
            return release, asset
        raise ProviderError(
            f"no recent release of {project} has a usable JAR attachment",
            hint="the newest releases were checked; add this plugin manually with an exact version",
        )


class ModrinthProvider(PluginProvider):
    """Modrinth API v2 (exact version_number and exact file)."""

    name = "modrinth"
    supports_search = True

    @staticmethod
    def parse_project(project: str, *, field_path: str) -> str:
        return parse_slug(project, field_path=field_path)

    @staticmethod
    def page_url(project: str) -> str:
        return f"https://modrinth.com/project/{project}"

    def resolve(self, spec: PluginConfig, *, minecraft_version: str) -> ReleaseInfo:
        slug = parse_slug(spec.project, field_path=f"plugins.{spec.id}.project")
        self.context.modrinth.get_project(slug)
        versions = self.context.modrinth.get_versions(slug)
        version = select_version(
            versions,
            version_number=spec.version,
            minecraft_version=minecraft_version,
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

    def search(self, query: str, *, limit: int) -> tuple[SearchResult, ...]:
        return self.context.modrinth.search_projects(query, limit=limit)

    def recommend(
        self,
        project: str,
        *,
        templates: tuple[str, ...],
        minecraft_version: str,
        plugin_id: str,
    ) -> RecommendedPlugin:
        version = self._newest_compatible_version(project, minecraft_version=minecraft_version)
        selector = templates[0] if templates else None
        file = self._select_recommended_file(version, selector)
        note = ""
        if version.version_type != "release":
            note = (
                f"no stable release for Minecraft {minecraft_version}; using {version.version_type}"
            )
        return RecommendedPlugin(
            id=plugin_id,
            provider=self.name,
            project=project,
            version=version.version_number,
            asset=file.filename,
            note=note,
        )

    def list_versions(self, project: str, *, minecraft_version: str) -> list[tuple[str, str]]:
        """(version number, version type) usable with PAPER on the target Minecraft version."""
        slug = parse_slug(project, field_path="project")
        versions = self.context.modrinth.get_versions(slug)
        compatible = [
            (version.version_number, version.version_type)
            for version in versions
            if minecraft_version in version.game_versions
            and set(version.loaders) & set(PAPER_LOADERS)
        ]
        if not compatible:
            raise ProviderError(
                f"Modrinth project {slug} has no PAPER version for Minecraft {minecraft_version}",
                hint="pin another Paper version first (rocket pin core), or edit the pin by hand",
            )
        return compatible[:_PIN_CATALOG_LIMIT]

    def file_choices(self, project: str, *, version: str) -> tuple[str | None, list[str]]:
        slug = parse_slug(project, field_path="project")
        versions = self.context.modrinth.get_versions(slug)
        found = next((item for item in versions if item.version_number == version), None)
        if found is None:
            raise ProviderError(f"Modrinth version number {version!r} does not exist: {slug!r}")
        return _file_choices(
            [file.filename for file in found.files], context=f"Modrinth version {version!r}"
        )

    def _newest_compatible_version(
        self, project: str, *, minecraft_version: str
    ) -> ModrinthVersion:
        """Newest version listing the target game version and a paper-family loader;
        a non-release version is used only with a note."""
        slug = parse_slug(project, field_path="project")
        versions = self.context.modrinth.get_versions(slug)
        candidates = [
            version
            for version in versions
            if minecraft_version in version.game_versions
            and set(version.loaders) & set(PAPER_LOADERS)
        ]
        if not candidates:
            raise ProviderError(
                f"Modrinth project {slug} has no PAPER version for Minecraft {minecraft_version}",
                hint="nearby Minecraft versions are not assumed equivalent; "
                "add this plugin manually with an exact version",
            )
        releases = [version for version in candidates if version.version_type == "release"]
        return releases[0] if releases else candidates[0]

    @staticmethod
    def _select_recommended_file(version: ModrinthVersion, selector: str | None) -> ModrinthFile:
        """Templates try every filename form of the tag, then the unique-JAR rule applies.

        A template derived from an old pin may match nothing (the new filenames spell the
        version differently); then a unique JAR is still unambiguous, and several JARs fail
        with the usual "cannot choose" listing.
        """
        if selector is not None and "{version}" in selector:
            names = {file.filename for file in version.files}
            for form in version_forms(version.version_number):
                candidate = selector.replace("{version}", form)
                if candidate in names:
                    return select_file(version, selector=candidate)
            return select_file(version, selector=None)
        return select_file(version, selector=selector)


class SpigetProvider(PluginProvider):
    """Spiget API v2 (SpigotMC resources; the exact version name is pinned)."""

    name = "spiget"
    supports_search = True

    @staticmethod
    def parse_project(project: str, *, field_path: str) -> str:
        return parse_resource_id(project, field_path=field_path)

    @staticmethod
    def page_url(project: str) -> str:
        return f"https://www.spigotmc.org/resources/{project}"

    def resolve(self, spec: PluginConfig, *, minecraft_version: str) -> ReleaseInfo:
        resource_id = parse_resource_id(spec.project, field_path=f"plugins.{spec.id}.project")
        resource = self._checked_resource(resource_id)
        versions = self.context.spiget.get_versions(resource_id)
        version = select_spiget_version(versions, name=spec.version, resource_id=resource_id)
        compatibility, note = check_tested_versions(resource, minecraft_version=minecraft_version)
        if compatibility is Compatibility.UNKNOWN:
            self.context.log.warning("[%s] warning: %s", spec.id, note)
        self.context.log.info(
            "[%s] selected Spigot version %s (id %s)", spec.id, version.name, version.version_id
        )
        entry = DownloadEntry(
            kind=EntryKind.DIRECT,
            url=version_download_url(resource_id, version.version_id),
            origin=self.name,
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
            source_chain=("spiget",),
        )

    def search(self, query: str, *, limit: int) -> tuple[SearchResult, ...]:
        return self.context.spiget.search_resources(query, limit=limit)

    def recommend(
        self,
        project: str,
        *,
        templates: tuple[str, ...],
        minecraft_version: str,
        plugin_id: str,
    ) -> RecommendedPlugin:
        resource_id = parse_resource_id(project, field_path="project")
        if templates:
            raise ProviderError(
                "Spiget versions ship a single unnamed file; asset cannot be verified",
                hint="drop the asset and declare only the exact version",
            )
        resource = self._checked_resource(resource_id)
        _compatibility, note = check_tested_versions(resource, minecraft_version=minecraft_version)
        versions = self.context.spiget.get_versions(resource_id)
        return RecommendedPlugin(
            id=plugin_id,
            provider=self.name,
            project=project,
            version=versions[0].name,
            note=note,
        )

    def list_versions(self, project: str, *, minecraft_version: str) -> list[tuple[str, str]]:
        """(version name, release date), newest first; compatibility is resource-level,
        so a declared mismatch rejects the whole list."""
        resource_id = parse_resource_id(project, field_path="project")
        resource = self._checked_resource(resource_id)
        check_tested_versions(resource, minecraft_version=minecraft_version)
        versions = self.context.spiget.get_versions(resource_id)
        return [
            (version.name, version_detail(version)) for version in versions[:_PIN_CATALOG_LIMIT]
        ]

    def file_choices(self, project: str, *, version: str) -> tuple[str | None, list[str]]:
        return None, []  # Spiget serves a single unnamed file per version; no asset is pinned

    def _checked_resource(self, resource_id: str) -> SpigetResource:
        """Fetch the resource and reject the ones that can never yield a JAR."""
        resource = self.context.spiget.get_resource(resource_id)
        if resource.premium:
            raise ProviderError(
                f"Spigot resource {resource_id} ({resource.name}) is premium",
                hint="premium resources cannot be downloaded without purchase; "
                "use another provider or place the JAR manually",
            )
        if resource.external:
            raise ProviderError(
                f"Spigot resource {resource_id} ({resource.name}) is hosted externally",
                hint="external Spigot resources point at an arbitrary page, not a JAR; "
                "use the hangar, github or modrinth provider instead",
            )
        return resource


PROVIDERS: dict[str, type[PluginProvider]] = {
    HangarProvider.name: HangarProvider,
    GitHubProvider.name: GitHubProvider,
    ModrinthProvider.name: ModrinthProvider,
    SpigetProvider.name: SpigetProvider,
}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _raise_on_api_error(payload: Any, *, context: str) -> None:
    """Fill error objects look like {"ok": false, "error": ..., "message": ...}."""
    if isinstance(payload, dict) and payload.get("ok") is False:
        detail = payload.get("message") or payload.get("error") or "unknown error"
        raise ProviderError(f"Fill API returned an error ({context}): {detail}")


def _hangar_get_or_not_found(http: HTTPClient, url: str, *, project: str) -> Any:
    try:
        return http.get_json(url)
    except HTTPStatusError as exc:
        if exc.status_code == 404:
            raise ProviderError(
                f"Hangar project or version not found: {project}",
                hint="check the project coordinates and availability on hangar.papermc.io",
            ) from exc
        raise


def _check_declared_platform(
    detail: dict[str, Any],
    *,
    project: str,
    version: str,
    minecraft_version: str,
) -> None:
    """The download pipeline rejects a declared-but-missing platform version, so the
    recommendation checks it up front."""
    dependencies = detail.get("platformDependencies")
    declared = dependencies.get("PAPER") if isinstance(dependencies, dict) else None
    if not isinstance(declared, list):
        return
    if minecraft_version not in {str(item) for item in declared}:
        raise ProviderError(
            f"Hangar version {version} of {project} does not declare PAPER support for "
            f"Minecraft {minecraft_version}",
            hint="nearby patch versions are not assumed equivalent",
        )


def _channel_name(value: Any) -> str | None:
    if isinstance(value, dict):
        name = value.get("name")
        return name if isinstance(name, str) else None
    return value if isinstance(value, str) else None


def _file_choices(names: list[str], *, context: str) -> tuple[str | None, list[str]]:
    """One JAR is used directly; several JARs are returned for the user to choose from."""
    jars = [name for name in names if name.lower().endswith(".jar")]
    if not jars:
        raise ProviderError(
            f"{context} has no JAR files", hint=f"available files: {_summarize_names(names)}"
        )
    if len(jars) == 1:
        return jars[0], []
    return None, jars


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


def _summarize_names(names: list[str]) -> str:
    return ", ".join(names) if names else "(none)"


def _one_line(value: Any) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""
