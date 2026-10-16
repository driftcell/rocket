"""Streaming download, size/hash/JAR validation and per-file atomic replace."""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import time
import zipfile
import zlib
from collections.abc import Callable
from pathlib import Path

import httpx
from tenacity import (
    Retrying,
    before_sleep_log,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .errors import DownloadError, StreamInterruptedError, ValidationError
from .http_client import HTTPClient
from .models import Artifact, ArtifactKind, Checksum, DownloadResult, Status

_CHUNK_SIZE = 64 * 1024


class HTTPDownloader:
    """Turn an Artifact into a verified local file."""

    def __init__(
        self,
        http: HTTPClient,
        *,
        retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        logger: logging.Logger | None = None,
    ) -> None:
        self._http = http
        self._log = logger or logging.getLogger("rocket.download")
        self._retryer = Retrying(
            retry=retry_if_exception_type(StreamInterruptedError),
            stop=stop_after_attempt(max(0, retries) + 1),
            wait=wait_exponential(multiplier=0.5, max=8.0),
            sleep=sleep,
            reraise=True,
            before_sleep=before_sleep_log(self._log, logging.WARNING),
        )

    def fetch(self, artifact: Artifact) -> DownloadResult:
        """Download and verify; on failure clean up the temporary file and keep the existing
        target."""
        target = artifact.target
        if artifact.checksum is not None and target.is_file():
            skipped = self._try_skip(artifact)
            if skipped is not None:
                return skipped

        temp_path = self._retryer(self._download_attempt, artifact)
        try:
            size = temp_path.stat().st_size
            _check_size(artifact, size)
            algorithm = artifact.checksum.algorithm if artifact.checksum is not None else "sha256"
            digest = _hash_file(temp_path, algorithm)
            _check_hash(artifact, digest)
            _validate_jar(temp_path, artifact)
            try:
                os.replace(temp_path, target)
            except OSError as exc:
                raise DownloadError(
                    f"failed to write destination file {target}: {exc}",
                    hint="check the output directory write permissions and free space",
                ) from exc
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

        return DownloadResult(
            artifact_id=artifact.artifact_id,
            kind=artifact.kind,
            provider=artifact.provider,
            project=artifact.project,
            version=artifact.version,
            detail=artifact.detail,
            source_chain=artifact.source_chain,
            target=target,
            status=Status.DOWNLOADED,
            size=size,
            checksum=Checksum(algorithm=algorithm, value=digest),
            upstream_checksum=artifact.checksum,
            upstream_hash_verified=artifact.checksum is not None,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _try_skip(self, artifact: Artifact) -> DownloadResult | None:
        """Skip only when an upstream checksum exists and the existing file passes hash and JAR
        validation."""
        checksum = artifact.checksum
        if checksum is None:  # fetch guarantees non-None; double-check here
            return None
        target = artifact.target
        algorithm = checksum.algorithm
        try:
            size = target.stat().st_size
            if artifact.size is not None and size != artifact.size:
                self._log.info(
                    "[%s] existing file size does not match upstream; re-downloading",
                    artifact.artifact_id,
                )
                return None
            digest = _hash_file(target, algorithm)
            if digest != checksum.value:
                self._log.info(
                    "[%s] existing file %s does not match upstream; re-downloading",
                    artifact.artifact_id,
                    _algorithm_label(algorithm),
                )
                return None
            _validate_jar(target, artifact)
        except ValidationError as exc:
            self._log.info(
                "[%s] existing file failed JAR validation (%s); re-downloading",
                artifact.artifact_id,
                exc.message,
            )
            return None
        except OSError:
            return None
        self._log.info(
            "[%s] existing file matches upstream %s and is a valid JAR; skipping download",
            artifact.artifact_id,
            _algorithm_label(algorithm),
        )
        return DownloadResult(
            artifact_id=artifact.artifact_id,
            kind=artifact.kind,
            provider=artifact.provider,
            project=artifact.project,
            version=artifact.version,
            detail=artifact.detail,
            source_chain=artifact.source_chain,
            target=target,
            status=Status.SKIPPED,
            size=size,
            checksum=Checksum(algorithm=algorithm, value=digest),
            upstream_checksum=checksum,
            upstream_hash_verified=True,
        )

    def _download_attempt(self, artifact: Artifact) -> Path:
        """One full download attempt; cleans up its temporary file on failure."""
        target = artifact.target
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise DownloadError(
                f"cannot create output directory {target.parent}: {exc}",
                hint="check the path and write permissions",
            ) from exc
        temp_path = _new_temp_path(target)
        try:
            self._stream_to_file(artifact.url, temp_path)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
        return temp_path

    def _stream_to_file(self, url: str, temp_path: Path) -> None:
        with self._http.stream(url) as response:
            try:
                file_obj = temp_path.open("wb")
            except OSError as exc:
                raise DownloadError(
                    f"cannot create temporary file {temp_path}: {exc}",
                    hint="check the output directory write permissions",
                ) from exc
            with file_obj:
                try:
                    for chunk in response.iter_bytes(_CHUNK_SIZE):
                        file_obj.write(chunk)
                except httpx.TransportError as exc:
                    raise StreamInterruptedError(
                        f"download interrupted ({exc.__class__.__name__}): {url}",
                        hint="the whole file will be re-downloaded per the retry policy",
                    ) from exc
                except OSError as exc:
                    raise DownloadError(
                        f"failed to write temporary file {temp_path}: {exc}"
                    ) from exc


def _new_temp_path(target: Path) -> Path:
    """Create a unique temporary file next to the target with a fully controlled path."""
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
    except OSError as exc:
        raise DownloadError(f"cannot create temporary file in {target.parent}: {exc}") from exc
    os.close(descriptor)
    return Path(name)


def _hash_file(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _check_size(artifact: Artifact, size: int) -> None:
    if artifact.size is not None and size != artifact.size:
        raise ValidationError(
            f"size mismatch: expected {artifact.size} bytes, got {size} bytes",
            hint="the download may be truncated or the upstream artifact updated; "
            "no automatic repair",
        )


def _algorithm_label(algorithm: str) -> str:
    """Render 'sha256'/'sha512' as 'SHA-256'/'SHA-512'."""
    name = algorithm.lower()
    if name.startswith("sha") and name[3:].isdigit():
        return f"SHA-{name[3:]}"
    return algorithm.upper()


def _check_hash(artifact: Artifact, digest: str) -> None:
    checksum = artifact.checksum
    if checksum is None:
        return
    if digest != checksum.value:
        raise ValidationError(
            f"upstream {_algorithm_label(checksum.algorithm)} mismatch: "
            f"expected {checksum.value}, got {digest}",
            hint="fail immediately; expected values are never overwritten to force a pass",
        )


def _validate_jar(path: Path, artifact: Artifact) -> None:
    """zipfile validation: CRC check, Paper manifest, plugin descriptor."""
    try:
        with zipfile.ZipFile(path) as archive:
            damaged = archive.testzip()
            if damaged is not None:
                raise ValidationError(
                    f"ZIP CRC check failed (damaged entry: {damaged})",
                    hint="file is corrupted or has been tampered with",
                )
            names = set(archive.namelist())
            if artifact.kind is ArtifactKind.PAPER:
                if "META-INF/MANIFEST.MF" not in names:
                    raise ValidationError(
                        "Paper JAR is missing META-INF/MANIFEST.MF",
                        hint="may be an error page or a non-Paper artifact",
                    )
                main_class = _manifest_main_class(archive.read("META-INF/MANIFEST.MF"))
                if not main_class:
                    raise ValidationError(
                        "Paper JAR MANIFEST.MF has no non-empty Main-Class",
                        hint="the main class is not hardcoded, but Main-Class must be declared",
                    )
            elif "plugin.yml" not in names and "paper-plugin.yml" not in names:
                raise ValidationError(
                    "plugin JAR root is missing plugin.yml or paper-plugin.yml",
                    hint="source/dev attachments are rejected; "
                    "make sure asset points to a real plugin artifact",
                )
    except zipfile.BadZipFile as exc:
        raise ValidationError(
            f"content is not a valid ZIP/JAR: {path.name} ({exc})",
            hint="may be an HTML/JSON error page, an empty file, or a truncated file",
        ) from exc
    except zlib.error as exc:
        raise ValidationError(
            f"ZIP extraction failed: {path.name}: {exc}",
            hint="file is corrupted or truncated",
        ) from exc
    except EOFError as exc:
        raise ValidationError(
            f"ZIP data is truncated: {path.name}", hint="incomplete download"
        ) from exc


def _manifest_main_class(raw: bytes) -> str:
    """Parse the non-empty Main-Class from MANIFEST.MF (line folding supported,
    case-insensitive keys)."""
    text = raw.decode("utf-8", errors="replace")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    unfolded: list[str] = []
    for line in lines:
        if line.startswith(" ") and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    for line in unfolded:
        name, separator, value = line.partition(":")
        if separator and name.strip().lower() == "main-class":
            return value.strip()
    return ""
