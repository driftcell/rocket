"""Version recommendation for ``rocket init``.

Given catalog entries and the chosen Minecraft release, pick the version the
download pipeline can use exactly:

- Hangar: newest Release-channel version that declares the target platform version;
  a hosted file is used as-is, an external link must be a fixed-tag GitHub Release
  page whose attachments are selected by template;
- Modrinth: newest release version listing the target game version and a
  paper-family loader (a non-release version is used only with a note);
- GitHub Releases: newest release with a matching JAR attachment; GitHub provides
  no Minecraft compatibility metadata, so the newest usable release is used as-is.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .catalog import CatalogPlugin
from .errors import HTTPStatusError, ProviderError, RocketError
from .github import (
    GitHubClient,
    GitHubRelease,
    GitHubURLClass,
    classify_github_url,
    parse_project,
)
from .http_client import HTTPClient
from .modrinth import (
    PAPER_LOADERS,
    ModrinthClient,
    ModrinthFile,
    ModrinthVersion,
    parse_slug,
    select_file,
)
from .providers import fetch_paper_versions

_HANGAR_BASE = "https://hangar.papermc.io/api/v1"
_HANGAR_VERSION_LIMIT = 10
_GITHUB_NOTE = "GitHub Releases provide no Minecraft compatibility info (treated as unknown)"


@dataclass(frozen=True)
class RecommendedPlugin:
    """A catalog entry resolved to an exact, downloadable version."""

    id: str
    provider: str
    project: str
    version: str
    asset: str | None = None
    note: str = ""


@dataclass(frozen=True)
class SkippedPlugin:
    """A catalog entry that could not be resolved; the wizard reports it and continues."""

    id: str
    reason: str


@dataclass(frozen=True)
class RecommendationResult:
    plugins: tuple[RecommendedPlugin, ...]
    skipped: tuple[SkippedPlugin, ...]


def list_paper_versions(http: HTTPClient) -> tuple[str, ...]:
    """Downloadable Paper releases, newest first (rc/pre builds are filtered out)."""
    groups = fetch_paper_versions(http)
    versions: dict[str, None] = {}
    for members in groups.values():
        for member in members:
            if "-rc" in member or "-pre" in member:
                continue
            versions.setdefault(member, None)
    return tuple(versions)


def recommend_plugins(
    plugins: Sequence[CatalogPlugin],
    *,
    minecraft_version: str,
    http: HTTPClient,
) -> RecommendationResult:
    """Resolve the newest usable version for every catalog entry; failures are skipped."""
    github = GitHubClient(http)
    modrinth = ModrinthClient(http)
    resolved: list[RecommendedPlugin] = []
    skipped: list[SkippedPlugin] = []
    seen_hangar_projects: set[str] = set()
    for plugin in plugins:
        try:
            if plugin.provider == "hangar":
                if plugin.project in seen_hangar_projects:
                    continue
                seen_hangar_projects.add(plugin.project)
                group = [
                    item
                    for item in plugins
                    if item.provider == "hangar" and item.project == plugin.project
                ]
                group_resolved, group_skipped = _recommend_hangar_group(
                    group, minecraft_version=minecraft_version, http=http, github=github
                )
                resolved.extend(group_resolved)
                skipped.extend(group_skipped)
            elif plugin.provider == "github":
                outcome = _recommend_github_plugin(plugin, github=github)
                if isinstance(outcome, SkippedPlugin):
                    skipped.append(outcome)
                else:
                    resolved.append(outcome)
            elif plugin.provider == "modrinth":
                outcome = _recommend_modrinth_plugin(
                    plugin, modrinth=modrinth, minecraft_version=minecraft_version
                )
                if isinstance(outcome, SkippedPlugin):
                    skipped.append(outcome)
                else:
                    resolved.append(outcome)
            else:
                skipped.append(
                    SkippedPlugin(plugin.id, f"unsupported provider {plugin.provider!r}")
                )
        except RocketError as exc:
            skipped.append(SkippedPlugin(plugin.id, exc.message))
        except Exception as exc:  # pragma: no cover - unexpected errors surface as skips
            skipped.append(
                SkippedPlugin(plugin.id, f"unexpected error ({exc.__class__.__name__}): {exc}")
            )
    return RecommendationResult(plugins=tuple(resolved), skipped=tuple(skipped))


def recommend_plugin(
    provider: str,
    project: str,
    *,
    asset: str | None,
    minecraft_version: str,
    http: HTTPClient,
) -> RecommendedPlugin:
    """Resolve the newest usable version of one ad-hoc plugin (used by ``rocket add``).

    Unlike :func:`recommend_plugins`, a resolution failure is raised, not skipped.
    """
    entry = CatalogPlugin(
        id="plugin",
        name=project,
        description="",
        provider=provider,
        project=project,
        assets=(asset,) if asset is not None else (),
    )
    if provider == "hangar":
        resolved, skipped = _recommend_hangar_group(
            (entry,),
            minecraft_version=minecraft_version,
            http=http,
            github=GitHubClient(http),
        )
        if resolved:
            return resolved[0]
        raise ProviderError(skipped[0].reason)
    if provider == "github":
        outcome = _recommend_github_plugin(entry, github=GitHubClient(http))
    elif provider == "modrinth":
        outcome = _recommend_modrinth_plugin(
            entry, modrinth=ModrinthClient(http), minecraft_version=minecraft_version
        )
    else:
        raise ProviderError(f"unsupported provider {provider!r}")
    if isinstance(outcome, SkippedPlugin):
        raise ProviderError(outcome.reason)
    return outcome


def asset_template(asset: str, version: str) -> str:
    """Recover a catalog-style ``{version}`` template from a concrete pinned filename.

    Filenames do not always spell the version tag literally (``v5.5.60`` may appear as
    ``5.5.60``), so every filename form of the pinned version is tried.
    """
    for form in _version_forms(version):
        if form in asset:
            return asset.replace(form, "{version}")
    return asset


def _version_forms(version: str) -> tuple[str, ...]:
    """Filename spellings of a version tag: itself, without a ``v`` prefix, without suffixes."""
    forms = [version]
    unv = version[1:] if version.startswith("v") else version
    if unv != version:
        forms.append(unv)
    for base in (version, unv):
        stem = base.split("-", 1)[0]
        if stem and stem not in forms:
            forms.append(stem)
    return tuple(forms)


# ---------------------------------------------------------------------------
# Hangar
# ---------------------------------------------------------------------------


def _recommend_hangar_group(
    group: Sequence[CatalogPlugin],
    *,
    minecraft_version: str,
    http: HTTPClient,
    github: GitHubClient,
) -> tuple[list[RecommendedPlugin], list[SkippedPlugin]]:
    """Resolve a Hangar project once for every catalog entry that shares it."""
    project = group[0].project
    try:
        version, paper_download = _hangar_version_for(
            http, project=project, minecraft_version=minecraft_version
        )
    except RocketError as exc:
        return [], [SkippedPlugin(item.id, exc.message) for item in group]

    download_url = paper_download.get("downloadUrl")
    external_url = paper_download.get("externalUrl")
    if isinstance(download_url, str) and download_url:
        file_info = paper_download.get("fileInfo")
        hosted_name = file_info.get("name") if isinstance(file_info, dict) else None
        resolved: list[RecommendedPlugin] = []
        skipped: list[SkippedPlugin] = []
        for item in group:
            wanted = [template.replace("{version}", version) for template in item.assets]
            hosted = hosted_name if isinstance(hosted_name, str) and hosted_name else None
            if not wanted or hosted in wanted:
                resolved.append(_recommended(item, version))
            elif hosted is not None:
                skipped.append(
                    SkippedPlugin(
                        item.id,
                        f"asset does not match the Hangar hosted filename {hosted!r} "
                        f"for {project} {version}",
                    )
                )
            else:
                skipped.append(
                    SkippedPlugin(
                        item.id,
                        f"asset is declared but Hangar provided no hosted filename "
                        f"to verify for {project} {version}",
                    )
                )
        return resolved, skipped

    if isinstance(external_url, str) and external_url:
        try:
            release = _release_for_external_link(github, external_url)
        except RocketError as exc:
            return [], [SkippedPlugin(item.id, exc.message) for item in group]
        resolved = []
        skipped = []
        for item in group:
            try:
                asset = _release_asset_name(release, version=version, templates=item.assets)
            except RocketError as exc:
                skipped.append(SkippedPlugin(item.id, exc.message))
            else:
                resolved.append(_recommended(item, version, asset))
        return resolved, skipped

    return [], [
        SkippedPlugin(item.id, f"Hangar version {version} of {project} has no PAPER download entry")
        for item in group
    ]


def _hangar_version_for(
    http: HTTPClient,
    *,
    project: str,
    minecraft_version: str,
) -> tuple[str, dict[str, Any]]:
    """Newest Release-channel version declaring PAPER support, plus its PAPER download entry."""
    _, slug = parse_project(project, field_path="project")
    quoted_slug = urllib.parse.quote(slug, safe="")
    params = urllib.parse.urlencode(
        {
            "platform": "PAPER",
            "platformVersion": minecraft_version,
            "channel": "Release",
            "limit": str(_HANGAR_VERSION_LIMIT),
        }
    )
    payload = _get_json_or_not_found(
        http, f"{_HANGAR_BASE}/projects/{quoted_slug}/versions?{params}", project=project
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
    detail_url = (
        f"{_HANGAR_BASE}/projects/{quoted_slug}/versions/{urllib.parse.quote(version, safe='')}"
    )
    detail = _get_json_or_not_found(http, detail_url, project=project)
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
        raise ProviderError(f"Hangar version {version} of {project} has no PAPER download entry")
    return version, paper


# ---------------------------------------------------------------------------
# GitHub Releases
# ---------------------------------------------------------------------------


def _recommend_github_plugin(
    plugin: CatalogPlugin,
    *,
    github: GitHubClient,
) -> RecommendedPlugin | SkippedPlugin:
    try:
        release, asset = _pick_github_release(plugin, github=github)
    except RocketError as exc:
        return SkippedPlugin(plugin.id, exc.message)
    return RecommendedPlugin(
        id=plugin.id,
        provider=plugin.provider,
        project=plugin.project,
        version=release.tag,
        asset=asset,
        note=_GITHUB_NOTE,
    )


def _pick_github_release(
    plugin: CatalogPlugin,
    *,
    github: GitHubClient,
) -> tuple[GitHubRelease, str]:
    """Newest non-draft, non-prerelease release that has the wanted JAR attachment."""
    owner, repo = parse_project(plugin.project, field_path="project")
    releases = github.get_releases(owner, repo)
    for release in releases:
        if release.draft or release.prerelease:
            continue
        try:
            asset = _release_asset_name(release, version=release.tag, templates=plugin.assets)
        except ProviderError:
            continue
        return release, asset
    raise ProviderError(
        f"no recent release of {plugin.project} has a usable JAR attachment",
        hint="the newest releases were checked; add this plugin manually with an exact version",
    )


def _release_for_external_link(github: GitHubClient, url: str) -> GitHubRelease:
    """Follow an external Hangar link that points at a fixed-tag GitHub Release page."""
    url_class, coordinates = classify_github_url(url)
    if url_class is not GitHubURLClass.RELEASE_TAG or coordinates is None:
        raise ProviderError(
            f"external link is not a fixed GitHub Release tag page: {url}",
            hint="the wizard only follows fixed-tag Release pages",
        )
    return github.get_release(coordinates.owner, coordinates.repo, coordinates.tag)


def _release_asset_name(
    release: GitHubRelease,
    *,
    version: str,
    templates: tuple[str, ...],
) -> str:
    """Pick the attachment by template; otherwise only a unique JAR is allowed."""
    names = [asset.name for asset in release.assets]
    if templates:
        wanted = [template.replace("{version}", version) for template in templates]
        chosen = next((name for name in wanted if name in names), None)
        if chosen is None:
            raise ProviderError(
                f"release {release.owner}/{release.repo}@{release.tag} has none of the "
                f"expected attachments",
                hint=f"expected: {_summarize(wanted)}; available: {_summarize(names)}",
            )
    else:
        jars = [name for name in names if name.lower().endswith(".jar")]
        if len(jars) == 1:
            chosen = jars[0]
        elif jars:
            raise ProviderError(
                f"release {release.owner}/{release.repo}@{release.tag} has multiple JAR "
                f"attachments; cannot choose",
                hint=f"declare asset with the exact upstream filename: {_summarize(jars)}",
            )
        else:
            raise ProviderError(
                f"release {release.owner}/{release.repo}@{release.tag} has no JAR attachments",
                hint=f"available: {_summarize(names)}",
            )
    if not chosen.lower().endswith(".jar"):
        raise ProviderError(
            f"selected attachment {chosen!r} is not a JAR file",
            hint="only JAR attachments are accepted",
        )
    return chosen


# ---------------------------------------------------------------------------
# Modrinth
# ---------------------------------------------------------------------------


def _recommend_modrinth_plugin(
    plugin: CatalogPlugin,
    *,
    modrinth: ModrinthClient,
    minecraft_version: str,
) -> RecommendedPlugin | SkippedPlugin:
    try:
        version = _pick_modrinth_version(
            modrinth, plugin.project, minecraft_version=minecraft_version
        )
        selector = plugin.assets[0] if plugin.assets else None
        file = _select_modrinth_file(version, selector)
    except RocketError as exc:
        return SkippedPlugin(plugin.id, exc.message)
    note = ""
    if version.version_type != "release":
        note = f"no stable release for Minecraft {minecraft_version}; using {version.version_type}"
    return RecommendedPlugin(
        id=plugin.id,
        provider=plugin.provider,
        project=plugin.project,
        version=version.version_number,
        asset=file.filename,
        note=note,
    )


def _select_modrinth_file(version: ModrinthVersion, selector: str | None) -> ModrinthFile:
    """Pick the file for a recommendation; templates try every filename form of the tag.

    A template derived from an old pin may match nothing (the new filenames spell the
    version differently); then a unique JAR is still unambiguous, and several JARs fail
    with the usual "cannot choose" listing.
    """
    if selector is not None and "{version}" in selector:
        names = {file.filename for file in version.files}
        for form in _version_forms(version.version_number):
            candidate = selector.replace("{version}", form)
            if candidate in names:
                return select_file(version, selector=candidate)
        return select_file(version, selector=None)
    return select_file(version, selector=selector)


def _pick_modrinth_version(
    modrinth: ModrinthClient,
    project: str,
    *,
    minecraft_version: str,
) -> ModrinthVersion:
    slug = parse_slug(project, field_path="project")
    versions = modrinth.get_versions(slug)
    candidates = [
        version
        for version in versions
        if minecraft_version in version.game_versions and set(version.loaders) & set(PAPER_LOADERS)
    ]
    if not candidates:
        raise ProviderError(
            f"Modrinth project {slug} has no PAPER version for Minecraft {minecraft_version}",
            hint="nearby Minecraft versions are not assumed equivalent; "
            "add this plugin manually with an exact version",
        )
    releases = [version for version in candidates if version.version_type == "release"]
    return releases[0] if releases else candidates[0]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _recommended(
    plugin: CatalogPlugin,
    version: str,
    asset: str | None = None,
) -> RecommendedPlugin:
    return RecommendedPlugin(
        id=plugin.id,
        provider=plugin.provider,
        project=plugin.project,
        version=version,
        asset=asset,
    )


def _get_json_or_not_found(http: HTTPClient, url: str, *, project: str) -> Any:
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
    """The download pipeline rejects a declared-but-missing platform version, so check it here."""
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


def _summarize(names: list[str]) -> str:
    return ", ".join(names) if names else "(none)"
