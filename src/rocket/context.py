"""Shared context for the resolution and download pipeline."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .github import GitHubClient
from .http_client import HTTPClient
from .models import ArtifactKind
from .modrinth import ModrinthClient
from .spiget import SpigetClient


@dataclass
class ProviderClients:
    """HTTP client facades and the logger shared by provider operations within one run.

    Lives outside the download pipeline too (search/add/pin have no output directory or
    target version at construction time), so those are layered on in the subclass.
    """

    http: HTTPClient
    github: GitHubClient
    modrinth: ModrinthClient
    spiget: SpigetClient
    log: logging.Logger


@dataclass
class PipelineContext(ProviderClients):
    """Clients plus the run-wide target Minecraft version and output directory."""

    minecraft_version: str
    output_dir: Path

    def target_path(self, artifact_id: str, kind: ArtifactKind) -> Path:
        """Local target paths are derived from the id, never from remote filenames."""
        if kind is ArtifactKind.PAPER:
            return self.output_dir / "paper.jar"
        return self.output_dir / "plugins" / f"{artifact_id}.jar"


def provider_clients(http: HTTPClient) -> ProviderClients:
    """The shared client bundle for provider operations outside the download pipeline."""
    return ProviderClients(
        http=http,
        github=GitHubClient(http),
        modrinth=ModrinthClient(http),
        spiget=SpigetClient(http),
        log=logging.getLogger("rocket"),
    )
