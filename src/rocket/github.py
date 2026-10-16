"""GitHub Release API client and shared attachment selection logic.

GitHubProvider and GitHubReleaseResolver share the same GitHubClient;
each (owner, repo, tag) is queried only once per run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from .errors import HTTPStatusError, ProviderError, ResolutionError
from .http_client import HTTPClient
from .models import Checksum, GitHubCoordinates

_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")
_API_BASE = "https://api.github.com"


@dataclass(frozen=True)
class GitHubAsset:
    """Release attachment (keeps only the fields needed for download and verification)."""

    name: str
    download_url: str
    size: int | None
    checksum: Checksum | None


@dataclass(frozen=True)
class GitHubRelease:
    """A queried Release (minimal required structure)."""

    owner: str
    repo: str
    tag: str
    html_url: str
    assets: tuple[GitHubAsset, ...]
    draft: bool = False
    prerelease: bool = False


class GitHubURLClass(StrEnum):
    """Classification result of a GitHub URL."""

    RELEASE_TAG = "release_tag"
    NOT_FIXED = "not_fixed"
    ASSET = "asset"
    OTHER = "other"


def parse_project(project: str, *, field_path: str) -> tuple[str, str]:
    """Split "owner/name" into two parts and validate characters.

    Shared Hangar/GitHub coordinate rule.
    """
    parts = project.split("/")
    if len(parts) != 2 or not all(parts):
        raise ProviderError(
            f"{field_path} must be in owner/name form, got {project!r}",
            hint="e.g. ViaVersion/ViaVersion or EssentialsX/Essentials",
        )
    owner, name = parts
    for part in (owner, name):
        if not _NAME_RE.fullmatch(part):
            raise ProviderError(
                f"{field_path} contains invalid characters: {part!r}",
                hint="only letters, digits, dots, underscores and hyphens are allowed",
            )
    return owner, name


def classify_github_url(url: str) -> tuple[GitHubURLClass, GitHubCoordinates | None]:
    """Classify a GitHub link.

    Categories: fixed-tag release page / not-fixed entry / asset direct link / other.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host not in {"github.com", "www.github.com"}:
        return GitHubURLClass.OTHER, None
    segments = [unquote(segment) for segment in parts.path.split("/") if segment]
    if len(segments) < 3 or segments[2] != "releases":
        return GitHubURLClass.OTHER, None
    owner, repo = segments[0], segments[1]
    if not _NAME_RE.fullmatch(owner) or not _NAME_RE.fullmatch(repo):
        return GitHubURLClass.OTHER, None
    if len(segments) == 3:  # /releases
        return GitHubURLClass.NOT_FIXED, None
    section = segments[3]
    if section == "tag":
        if len(segments) < 5 or not segments[4]:
            return GitHubURLClass.NOT_FIXED, None
        tag = "/".join(segments[4:])  # a "/" in the tag may come from decoding %2F
        return GitHubURLClass.RELEASE_TAG, GitHubCoordinates(owner=owner, repo=repo, tag=tag)
    if section == "download":  # /releases/download/<tag>/<file>
        return GitHubURLClass.ASSET, None
    if section == "latest":
        return GitHubURLClass.NOT_FIXED, None
    return GitHubURLClass.OTHER, None


class GitHubClient:
    """GitHub Release query client with in-run caching."""

    def __init__(self, http: HTTPClient) -> None:
        self._http = http
        self._cache: dict[tuple[str, str, str], GitHubRelease] = {}
        self._release_lists: dict[tuple[str, str, int], tuple[GitHubRelease, ...]] = {}

    def get_release(self, owner: str, repo: str, tag: str) -> GitHubRelease:
        """Query a Release by exact tag; fail explicitly on 404, no latest fallback."""
        key = (owner.lower(), repo.lower(), tag)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        url = (
            f"{_API_BASE}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
            f"/releases/tags/{quote(tag, safe='')}"
        )
        try:
            payload = self._http.get_json(url)
        except HTTPStatusError as exc:
            if exc.status_code == 404:
                raise ResolutionError(
                    f"GitHub Release not found: {owner}/{repo}@{tag} (HTTP 404)",
                    hint="the tag must exist exactly; latest and version ranges are not supported, "
                    "and no v prefix is added or removed automatically",
                ) from exc
            if exc.status_code in (403, 429):
                raise ResolutionError(
                    f"GitHub API rejected the request: {owner}/{repo}@{tag} "
                    f"(HTTP {exc.status_code}, possibly rate limited)",
                    hint="set the GITHUB_TOKEN environment variable (only sent to api.github.com) "
                    "to raise the limit",
                ) from exc
            raise
        release = _parse_release(payload, owner=owner, repo=repo, tag=tag)
        self._cache[key] = release
        return release

    def get_releases(
        self,
        owner: str,
        repo: str,
        *,
        per_page: int = 30,
    ) -> tuple[GitHubRelease, ...]:
        """List recent Releases, newest first (used by ``rocket init`` recommendations)."""
        key = (owner.lower(), repo.lower(), per_page)
        cached = self._release_lists.get(key)
        if cached is not None:
            return cached
        url = (
            f"{_API_BASE}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
            f"/releases?per_page={per_page}"
        )
        try:
            payload = self._http.get_json(url)
        except HTTPStatusError as exc:
            if exc.status_code == 404:
                raise ResolutionError(
                    f"GitHub repository not found: {owner}/{repo} (HTTP 404)",
                    hint="check the project coordinates; no guessing by name",
                ) from exc
            if exc.status_code in (403, 429):
                raise ResolutionError(
                    f"GitHub API rejected the request: {owner}/{repo} "
                    f"(HTTP {exc.status_code}, possibly rate limited)",
                    hint="set the GITHUB_TOKEN environment variable (only sent to api.github.com) "
                    "to raise the limit",
                ) from exc
            raise
        if not isinstance(payload, list):
            raise ResolutionError(
                f"GitHub release list response has invalid structure (expected array): "
                f"{owner}/{repo}"
            )
        releases: list[GitHubRelease] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            tag = item.get("tag_name")
            if not isinstance(tag, str) or not tag:
                continue
            releases.append(_parse_release(item, owner=owner, repo=repo, tag=tag))
        self._release_lists[key] = tuple(releases)
        return tuple(releases)


def select_release_asset(release: GitHubRelease, *, selector: str | None) -> GitHubAsset:
    """Attachment selection: exact match when asset is configured.

    Otherwise only a unique JAR candidate is allowed.
    """
    names = [asset.name for asset in release.assets]
    if selector is not None:
        matches = [asset for asset in release.assets if asset.name == selector]
        if not matches:
            raise ResolutionError(
                f"attachment {selector!r} not found in "
                f"Release {release.owner}/{release.repo}@{release.tag}",
                hint=f"available attachments: {_summarize(names)}",
            )
        if len(matches) > 1:
            raise ResolutionError(
                f"Release has multiple attachments named {selector!r}; cannot decide",
            )
        chosen = matches[0]
        if not chosen.name.lower().endswith(".jar"):
            raise ResolutionError(
                f"the selected attachment {selector!r} is not a JAR file",
                hint="GitHub Release source archives (tarball/zipball) cannot be used as plugins",
            )
        return chosen

    jars = [asset for asset in release.assets if asset.name.lower().endswith(".jar")]
    if not jars:
        raise ResolutionError(
            f"Release {release.owner}/{release.repo}@{release.tag} has no usable JAR attachments",
            hint=f"available attachments: {_summarize(names)}; "
            f"URLs are not guessed and no other sources are tried",
        )
    if len(jars) > 1:
        raise ResolutionError(
            f"Release {release.owner}/{release.repo}@{release.tag} has multiple JAR attachments; "
            f"cannot choose automatically",
            hint=f"use asset to select one: {_summarize([asset.name for asset in jars])}",
        )
    return jars[0]


def _parse_release(payload: Any, *, owner: str, repo: str, tag: str) -> GitHubRelease:
    if not isinstance(payload, dict):
        raise ResolutionError(
            f"GitHub Release response has invalid structure (expected object): {owner}/{repo}@{tag}"
        )
    assets_raw = payload.get("assets")
    if not isinstance(assets_raw, list):
        raise ResolutionError(
            f"GitHub Release response is missing the assets field: {owner}/{repo}@{tag}",
            hint="the upstream response contract may have changed; "
            "update the adapter instead of guessing",
        )
    assets: list[GitHubAsset] = []
    for raw in assets_raw:
        if not isinstance(raw, dict):
            continue
        name = raw.get("name")
        download_url = raw.get("browser_download_url")
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(download_url, str)
            or not download_url
        ):
            continue
        size = raw.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            size = None
        assets.append(
            GitHubAsset(
                name=name,
                download_url=download_url,
                size=size,
                checksum=_parse_digest(raw.get("digest")),
            )
        )
    html_url = payload.get("html_url")
    if not isinstance(html_url, str) or not html_url:
        html_url = f"https://github.com/{owner}/{repo}/releases/tag/{quote(tag, safe='')}"
    return GitHubRelease(
        owner=owner,
        repo=repo,
        tag=tag,
        html_url=html_url,
        assets=tuple(assets),
        draft=payload.get("draft") is True,
        prerelease=payload.get("prerelease") is True,
    )


def _parse_digest(value: Any) -> Checksum | None:
    """Parse GitHub's digest field (form: sha256:<hex>); return None when missing or not sha256."""
    if not isinstance(value, str):
        return None
    prefix, _, digest = value.partition(":")
    if prefix.strip().lower() == "sha256" and re.fullmatch(r"[0-9a-fA-F]{64}", digest.strip()):
        return Checksum(algorithm="sha256", value=digest.strip().lower())
    return None


def _summarize(names: list[str]) -> str:
    if not names:
        return "(none)"
    return ", ".join(names)
