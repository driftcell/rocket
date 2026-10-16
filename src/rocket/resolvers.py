"""Resolver: turn a Provider entry into a deterministic Artifact.

Scope: handle external links and attachment selection, read checksums;
does not decide plugin upgrades or guess the best file among candidates.
"""

from __future__ import annotations

from .context import PipelineContext
from .errors import ResolutionError
from .github import GitHubURLClass, classify_github_url, select_release_asset
from .models import Artifact, EntryKind, ReleaseInfo, ResolverChoice


class DirectResolver:
    """Pin down a direct link or file candidate; the shared downloader does the JAR validation."""

    name = "direct"

    def __init__(self, context: PipelineContext) -> None:
        self.context = context

    def resolve(self, release: ReleaseInfo) -> Artifact:
        entry = release.entry
        if entry.kind is EntryKind.GITHUB_RELEASE:
            raise ResolutionError(
                f"[{release.artifact_id}] resolver=direct does not match a GitHub Release entry",
                hint="remove the explicit resolver (use auto) or switch to github_release",
            )
        selector = release.asset
        filename = entry.filename
        if selector is not None:
            if filename is None:
                raise ResolutionError(
                    f"[{release.artifact_id}] this entry provides no upstream filename; "
                    f"cannot verify asset={selector!r}",
                    hint="remove asset or use a source that provides filenames",
                )
            if filename != selector:
                raise ResolutionError(
                    f"[{release.artifact_id}] asset={selector!r} does not match "
                    f"the upstream filename {filename!r}",
                    hint="asset must exactly match the upstream artifact filename",
                )
        source_chain = release.source_chain
        if entry.kind is EntryKind.EXTERNAL:
            source_chain = (*source_chain, "external-direct")
        return Artifact(
            artifact_id=release.artifact_id,
            kind=release.kind,
            provider=release.provider,
            project=release.project,
            version=release.version,
            url=entry.url,
            upstream_filename=filename,
            size=entry.size,
            checksum=entry.checksum,
            source_chain=source_chain,
            target=self.context.target_path(release.artifact_id, release.kind),
            detail=release.detail,
            build=release.build,
        )


class GitHubReleaseResolver:
    """GitHub Release attachment selection and normalization."""

    name = "github_release"

    def __init__(self, context: PipelineContext) -> None:
        self.context = context

    def resolve(self, release: ReleaseInfo) -> Artifact:
        entry = release.entry
        coordinates = entry.github
        if coordinates is None:
            url_class, parsed = classify_github_url(entry.url)
            if url_class is not GitHubURLClass.RELEASE_TAG or parsed is None:
                raise ResolutionError(
                    f"[{release.artifact_id}] entry is not a fixed-tag GitHub Release page: "
                    f"{entry.url}",
                    hint="only https://github.com/<owner>/<repo>/releases/tag/<tag> is supported",
                )
            coordinates = parsed
        github_release = self.context.github.get_release(
            coordinates.owner, coordinates.repo, coordinates.tag
        )
        asset = select_release_asset(github_release, selector=release.asset)
        source_chain = (*release.source_chain, "github-release", "github-asset")
        return Artifact(
            artifact_id=release.artifact_id,
            kind=release.kind,
            provider=release.provider,
            project=release.project,
            version=release.version,
            url=asset.download_url,
            upstream_filename=asset.name,
            size=asset.size,
            checksum=asset.checksum,
            source_chain=source_chain,
            target=self.context.target_path(release.artifact_id, release.kind),
            detail=release.detail,
            build=release.build,
        )


def select_resolver(
    release: ReleaseInfo,
    choice: ResolverChoice,
    context: PipelineContext,
) -> DirectResolver | GitHubReleaseResolver:
    """Pick a Resolver by explicit choice or the auto rules; raise on mismatch,
    never switch silently."""
    entry = release.entry

    if choice is ResolverChoice.DIRECT:
        if entry.kind is EntryKind.GITHUB_RELEASE:
            raise ResolutionError(
                f"[{release.artifact_id}] resolver=direct does not match a GitHub Release entry",
                hint="switch to github_release or remove the explicit resolver (use auto)",
            )
        return DirectResolver(context)

    if choice is ResolverChoice.GITHUB_RELEASE:
        if entry.kind is EntryKind.GITHUB_RELEASE:
            return GitHubReleaseResolver(context)
        if entry.kind is EntryKind.EXTERNAL:
            url_class, _ = classify_github_url(entry.url)
            if url_class is GitHubURLClass.RELEASE_TAG:
                return GitHubReleaseResolver(context)
        raise ResolutionError(
            f"[{release.artifact_id}] resolver=github_release does not match the entry: "
            f"{entry.url}",
            hint="this resolver only accepts fixed-tag GitHub Release pages or "
            "GitHubProvider entries",
        )

    # auto: pick by entry kind and URL classification order
    if entry.kind is EntryKind.DIRECT:
        return DirectResolver(context)
    if entry.kind is EntryKind.GITHUB_RELEASE:
        return GitHubReleaseResolver(context)
    url_class, _ = classify_github_url(entry.url)
    if url_class is GitHubURLClass.RELEASE_TAG:
        return GitHubReleaseResolver(context)
    if url_class is GitHubURLClass.NOT_FIXED:
        raise ResolutionError(
            f"[{release.artifact_id}] external entry is not pinned to a specific version: "
            f"{entry.url}",
            hint="only fixed-tag Release pages are supported; latest and list pages "
            "are not, and no latest version is picked automatically",
        )
    return DirectResolver(context)
