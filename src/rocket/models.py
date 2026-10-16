"""Unified data models: download entries, releases, artifacts and results.

These models live only in runtime memory, command output and ``rocket.lock``:
they are never written back to server.toml.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class ArtifactKind(StrEnum):
    """Artifact kind."""

    PAPER = "paper"
    PLUGIN = "plugin"


class EntryKind(StrEnum):
    """Kind of download entry returned by a provider."""

    DIRECT = "direct"
    EXTERNAL = "external"
    GITHUB_RELEASE = "github_release"


class ResolverChoice(StrEnum):
    """Resolver choice explicitly set in config."""

    AUTO = "auto"
    DIRECT = "direct"
    GITHUB_RELEASE = "github_release"


class Compatibility(StrEnum):
    """Compatibility check result (incompatible aborts with an error, not a status)."""

    COMPATIBLE = "compatible"
    UNKNOWN = "unknown"


class Status(StrEnum):
    """Final download status."""

    DOWNLOADED = "downloaded"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class Checksum:
    """Checksum: algorithm (sha256/sha512) + lowercase hex value."""

    algorithm: str
    value: str


@dataclass(frozen=True)
class GitHubCoordinates:
    """GitHub Release coordinates (owner/repo/tag)."""

    owner: str
    repo: str
    tag: str


@dataclass(frozen=True)
class DownloadEntry:
    """Download entry found by a provider.

    ``origin`` is the source provider declared in config and is not rewritten
    by redirects; ``kind`` describes the entry form (direct/external/GitHub
    Release). direct/external entries may carry an upstream filename, size and
    upstream checksum; GitHub Release entries carry structured coordinates for
    the resolver to query exactly.
    """

    kind: EntryKind
    url: str
    origin: str
    filename: str | None = None
    size: int | None = None
    checksum: Checksum | None = None
    github: GitHubCoordinates | None = None


@dataclass(frozen=True)
class ReleaseInfo:
    """Provider query result, handed to the resolver for further resolution."""

    artifact_id: str
    kind: ArtifactKind
    provider: str
    project: str
    version: str
    entry: DownloadEntry
    asset: str | None = None
    platform: str = "PAPER"
    compatibility: Compatibility = Compatibility.UNKNOWN
    compatibility_note: str = ""
    detail: str | None = None
    build: int | None = None
    source_chain: tuple[str, ...] = ()


@dataclass(frozen=True)
class Artifact:
    """Resolved file candidate: final download URL, upstream checksum info and target path."""

    artifact_id: str
    kind: ArtifactKind
    provider: str
    project: str
    version: str
    url: str
    upstream_filename: str | None
    size: int | None
    checksum: Checksum | None
    source_chain: tuple[str, ...]
    target: Path
    detail: str | None = None
    build: int | None = None


@dataclass(frozen=True)
class SearchResult:
    """One plugin search hit; ``provider:project`` is the coordinates form used by config."""

    provider: str
    project: str
    name: str
    description: str = ""
    downloads: int | None = None


@dataclass(frozen=True)
class DownloadResult:
    """Final result for a single item (used for command output)."""

    artifact_id: str
    kind: ArtifactKind
    provider: str
    project: str
    version: str
    detail: str | None
    source_chain: tuple[str, ...]
    target: Path
    status: Status
    size: int
    checksum: Checksum
    upstream_checksum: Checksum | None
    upstream_hash_verified: bool
