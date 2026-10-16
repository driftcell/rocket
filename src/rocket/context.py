"""Shared context for the resolution and download pipeline."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .github import GitHubClient
from .http_client import HTTPClient
from .models import ArtifactKind
from .modrinth import ModrinthClient


@dataclass
class PipelineContext:
    """Clients, target version and output directory shared within one run."""

    http: HTTPClient
    github: GitHubClient
    modrinth: ModrinthClient
    minecraft_version: str
    output_dir: Path
    log: logging.Logger

    def target_path(self, artifact_id: str, kind: ArtifactKind) -> Path:
        """Local target paths are derived from the id, never from remote filenames."""
        if kind is ArtifactKind.PAPER:
            return self.output_dir / "paper.jar"
        return self.output_dir / "plugins" / f"{artifact_id}.jar"
