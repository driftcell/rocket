"""Command-line entry point: ``python -m rocket download --config server.toml``."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

import httpx

from . import __version__
from .config import Config, PluginConfig, load_config
from .context import PipelineContext
from .downloader import HTTPDownloader
from .errors import (
    EXIT_FAILURE,
    EXIT_OK,
    ExternalNotFollowedError,
    LockError,
    RocketError,
)
from .github import GitHubClient
from .http_client import HTTPClient
from .lockfile import LOCK_FILENAME, LockData, LockedEntry, load_lock, write_lock
from .models import (
    Artifact,
    ArtifactKind,
    DownloadResult,
    EntryKind,
    ReleaseInfo,
    ResolverChoice,
    Status,
)
from .modrinth import ModrinthClient
from .providers import PROVIDERS, FillPaperProvider
from .resolvers import select_resolver

_log = logging.getLogger("rocket")


@dataclass(frozen=True)
class _Item:
    """An item to process in one run (paper or a single plugin)."""

    id: str
    kind: ArtifactKind
    follow_external: bool = True
    plugin: PluginConfig | None = None


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """CLI entry point; ``transport``/``sleep`` are only for test injection."""
    parser = argparse.ArgumentParser(
        prog="rocket",
        description="Declaratively download the Paper server and plugin JARs",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    download_parser = subparsers.add_parser(
        "download",
        help="Download Paper and plugins from server.toml",
    )
    download_parser.add_argument(
        "--config",
        default="server.toml",
        help="path to the config file (default: ./server.toml)",
    )
    download_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="show file paths, sizes and checksums for every item",
    )
    download_parser.add_argument(
        "--upgrade",
        action="store_true",
        help="ignore rocket.lock, re-resolve every item and rewrite the lock",
    )
    args = parser.parse_args(argv)
    return run_download(
        Path(args.config),
        transport=transport,
        sleep=sleep,
        verbose=args.verbose,
        upgrade=args.upgrade,
    )


def run_download(
    config_path: Path,
    *,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
    verbose: bool = False,
    upgrade: bool = False,
) -> int:
    """Run one download; return the process exit code (0 all ok, 1 failures, 2 invalid config)."""
    handler = _install_logging(logging.INFO if verbose else logging.WARNING)
    try:
        return _run_download(
            config_path, transport=transport, sleep=sleep, verbose=verbose, upgrade=upgrade
        )
    finally:
        _remove_logging(handler)


def _run_download(
    config_path: Path,
    *,
    transport: httpx.BaseTransport | None,
    sleep: Callable[[float], None],
    verbose: bool,
    upgrade: bool,
) -> int:
    try:
        config = load_config(config_path)
    except RocketError as exc:
        _print_error(exc.message, hint=exc.hint)
        return exc.exit_code
    for warning in config.warnings:
        _log.warning("config warning: %s", warning)

    lock_file = config.path.parent / LOCK_FILENAME
    previous_lock, lock_warnings = load_lock(lock_file)
    for warning in lock_warnings:
        _log.warning("lock warning: %s", warning)
    reuse_lock = None if upgrade else previous_lock

    entries: list[DownloadResult] = []
    fresh_entries: dict[str, LockedEntry] = {}
    failures = 0
    resolved = 0
    resolve_seconds = 0.0
    download_seconds = 0.0
    with HTTPClient(
        user_agent=config.http.user_agent,
        timeout_seconds=config.http.timeout_seconds,
        retries=config.http.retries,
        transport=transport,
        sleep=sleep,
        github_token=os.environ.get("GITHUB_TOKEN"),
    ) as http:
        context = PipelineContext(
            http=http,
            github=GitHubClient(http),
            modrinth=ModrinthClient(http),
            minecraft_version=config.paper.version,
            output_dir=config.download.output_dir,
            log=_log,
        )
        downloader = HTTPDownloader(http, retries=config.http.retries, sleep=sleep, logger=_log)
        for item in _iter_items(config):
            started = time.perf_counter()
            try:
                artifact = _locked_artifact(item, config, context, reuse_lock)
                if artifact is None:
                    artifact = _resolve_artifact(item, config, context)
            except RocketError as exc:
                failures += 1
                _print_item_error(item.id, exc)
                continue
            except Exception as exc:
                failures += 1
                _print_unexpected_error(item.id, exc)
                continue
            resolve_seconds += time.perf_counter() - started
            resolved += 1
            fresh_entries[item.id] = _lock_entry(item, config, artifact)

            started = time.perf_counter()
            try:
                result = downloader.fetch(artifact)
            except RocketError as exc:
                failures += 1
                _print_item_error(item.id, exc)
            except Exception as exc:
                failures += 1
                _print_unexpected_error(item.id, exc)
            else:
                entries.append(result)
            download_seconds += time.perf_counter() - started

    if resolved or previous_lock is not None:
        try:
            changed = write_lock(lock_file, _merge_lock(config, fresh_entries, previous_lock))
        except LockError as exc:
            failures += 1
            _print_error(exc.message, hint=exc.hint)
        else:
            if changed:
                _log.info("[lock] wrote %s", lock_file)

    downloaded = sum(1 for result in entries if result.status is Status.DOWNLOADED)
    unchanged = len(entries) - downloaded
    _print_run_summary(resolved, downloaded, unchanged, resolve_seconds, download_seconds)
    for result in entries:
        _print_item_result(result, verbose=verbose)
    return EXIT_FAILURE if failures else EXIT_OK


def _iter_items(config: Config) -> list[_Item]:
    """paper first, then plugins in config order; all are downloaded; no enable switch yet."""
    items = [_Item(id="paper", kind=ArtifactKind.PAPER)]
    items.extend(
        _Item(
            id=plugin.id,
            kind=ArtifactKind.PLUGIN,
            follow_external=plugin.follow_external,
            plugin=plugin,
        )
        for plugin in config.plugins
    )
    return items


def _resolve_artifact(item: _Item, config: Config, context: PipelineContext) -> Artifact:
    release = _resolve_release(item, config, context)
    if release.entry.kind is EntryKind.EXTERNAL and not item.follow_external:
        raise ExternalNotFollowedError(
            f"external entry rejected by follow_external=false: {release.entry.url}",
            hint="set follow_external = true on the plugin item, or use a native content source",
        )
    resolver = select_resolver(release, _resolver_choice(item), context)
    return resolver.resolve(release)


def _resolve_release(item: _Item, config: Config, context: PipelineContext) -> ReleaseInfo:
    if item.kind is ArtifactKind.PAPER:
        return FillPaperProvider(context).resolve(config.paper)
    if item.plugin is None:  # pragma: no cover - guaranteed by _iter_items
        raise RocketError(f"internal error: plugin item {item.id} is missing its config")
    provider_class = PROVIDERS[item.plugin.provider]
    return provider_class(context).resolve(item.plugin)


def _resolver_choice(item: _Item) -> ResolverChoice:
    if item.plugin is None:
        return ResolverChoice.AUTO
    return item.plugin.resolver


# ---------------------------------------------------------------------------
# Lock file (rocket.lock)
# ---------------------------------------------------------------------------


def _locked_artifact(
    item: _Item,
    config: Config,
    context: PipelineContext,
    lock: LockData | None,
) -> Artifact | None:
    """Rebuild an artifact from a matching lock entry; None means resolve fresh."""
    entry = _locked_entry(lock, item)
    if entry is None or not _lock_entry_matches(item, config, entry):
        return None
    detail = f" ({entry.detail})" if entry.detail else ""
    _log.info("[%s] using locked artifact%s", item.id, detail)
    return Artifact(
        artifact_id=item.id,
        kind=item.kind,
        provider=entry.provider,
        project=entry.project,
        version=entry.version,
        url=entry.url,
        upstream_filename=entry.upstream_filename,
        size=entry.size,
        checksum=entry.checksum,
        source_chain=entry.source_chain,
        target=context.target_path(item.id, item.kind),
        detail=entry.detail,
        build=entry.build,
    )


def _locked_entry(lock: LockData | None, item: _Item) -> LockedEntry | None:
    if lock is None:
        return None
    if item.kind is ArtifactKind.PAPER:
        return lock.paper
    return lock.plugins.get(item.id)


def _lock_entry_matches(item: _Item, config: Config, entry: LockedEntry) -> bool:
    """Whether a lock entry still corresponds to the declared config identity."""
    if item.kind is ArtifactKind.PAPER:
        if entry.kind is not ArtifactKind.PAPER:
            return False
        paper = config.paper
        return (
            entry.version == paper.version
            and entry.channel == paper.channel
            and (paper.build is None or entry.build == paper.build)
        )
    plugin = item.plugin
    if plugin is None or entry.kind is not ArtifactKind.PLUGIN:
        return False
    return (
        entry.provider == plugin.provider
        and entry.project == plugin.project
        and entry.version == plugin.version
        and entry.asset == plugin.asset
        and entry.resolver == plugin.resolver.value
        and entry.follow_external == plugin.follow_external
    )


def _lock_entry(item: _Item, config: Config, artifact: Artifact) -> LockedEntry:
    """Record one resolved artifact (fresh or from the lock) in the new lock data."""
    if item.kind is ArtifactKind.PAPER:
        return LockedEntry(
            kind=ArtifactKind.PAPER,
            provider=artifact.provider,
            project=artifact.project,
            version=artifact.version,
            url=artifact.url,
            upstream_filename=artifact.upstream_filename,
            size=artifact.size,
            checksum=artifact.checksum,
            detail=artifact.detail,
            source_chain=artifact.source_chain,
            build=artifact.build,
            channel=config.paper.channel,
        )
    plugin = item.plugin
    if plugin is None:  # pragma: no cover - guaranteed by _iter_items
        raise RocketError(f"internal error: plugin item {item.id} is missing its config")
    return LockedEntry(
        kind=ArtifactKind.PLUGIN,
        provider=artifact.provider,
        project=artifact.project,
        version=artifact.version,
        url=artifact.url,
        upstream_filename=artifact.upstream_filename,
        size=artifact.size,
        checksum=artifact.checksum,
        detail=artifact.detail,
        source_chain=artifact.source_chain,
        asset=plugin.asset,
        resolver=plugin.resolver.value,
        follow_external=plugin.follow_external,
    )


def _merge_lock(
    config: Config,
    fresh: dict[str, LockedEntry],
    previous: LockData | None,
) -> LockData:
    """Assemble the new lock: fresh entries plus still-matching previous entries for items
    that were not resolved this run (e.g. ``--upgrade`` while a provider is unreachable)."""
    paper: LockedEntry | None = None
    plugins: dict[str, LockedEntry] = {}
    for item in _iter_items(config):
        entry = fresh.get(item.id)
        if entry is None:
            candidate = _locked_entry(previous, item)
            if candidate is None or not _lock_entry_matches(item, config, candidate):
                continue
            entry = candidate
        if item.kind is ArtifactKind.PAPER:
            paper = entry
        else:
            plugins[item.id] = entry
    return LockData(paper=paper, plugins=plugins)


def _print_run_summary(
    resolved: int,
    downloaded: int,
    unchanged: int,
    resolve_seconds: float,
    download_seconds: float,
) -> None:
    if resolved:
        print(_phase_line(f"Resolved {_plural(resolved, 'item')}", resolve_seconds))
    if downloaded:
        tail = f" ({unchanged} unchanged)" if unchanged else ""
        print(
            _phase_line(f"Downloaded {_plural(downloaded, 'item')}", download_seconds) + tail
        )
    elif unchanged:
        print(_phase_line(f"Audited {_plural(unchanged, 'item')}", download_seconds))


def _phase_line(text: str, seconds: float) -> str:
    return f"{text} in {_style(_format_duration(seconds), '2', stream=sys.stdout)}"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _format_duration(seconds: float) -> str:
    """Render a phase duration like ``12ms``, ``1.4s`` or ``2m 5s``."""
    if seconds < 1:
        return f"{round(seconds * 1000)}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes)}m {rest:.0f}s"


def _print_item_result(result: DownloadResult, *, verbose: bool) -> None:
    downloaded = result.status is Status.DOWNLOADED
    if downloaded:
        print(_format_item_line(result, downloaded=True))
    elif verbose:
        print(_format_item_line(result, downloaded=False))
    if verbose:
        print(_style(_format_item_detail(result), "2", stream=sys.stdout))


def _format_item_line(result: DownloadResult, *, downloaded: bool) -> str:
    version = result.version
    if result.detail:
        version = f"{version} {result.detail}"
    chain = " → ".join(result.source_chain)
    marker = (
        _style("+", "32", stream=sys.stdout) if downloaded else _style("=", "2", stream=sys.stdout)
    )
    return f" {marker} {result.target.name} ({version}, {chain})"


def _format_item_detail(result: DownloadResult) -> str:
    if result.upstream_hash_verified:
        hash_state = "upstream hash verified"
    else:
        hash_state = "upstream hash unavailable"
    checksum = f"{result.checksum.algorithm}={result.checksum.value}"
    return f"    {result.target} {result.size} bytes {checksum} ({hash_state})"


def _print_error(message: str, *, hint: str | None = None) -> None:
    text = f"{_style('error:', '1;31', stream=sys.stderr)} {message}"
    if hint:
        text += " " + _style(f"(hint: {hint})", "2", stream=sys.stderr)
    print(text, file=sys.stderr)


def _print_item_error(item_id: str, exc: RocketError) -> None:
    _print_error(f"{item_id}: {exc.message}", hint=exc.hint)


def _print_unexpected_error(item_id: str, exc: Exception) -> None:
    _log.debug("[%s] unexpected error details", item_id, exc_info=True)
    unexpected = RocketError(f"unexpected error ({exc.__class__.__name__}): {exc}")
    _print_item_error(item_id, unexpected)


def _style(text: str, code: str, *, stream: TextIO) -> str:
    if not _use_color(stream):
        return text
    return f"\x1b[{code}m{text}\x1b[0m"


def _use_color(stream: TextIO) -> bool:
    if os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return False
    try:
        return stream.isatty()
    except (AttributeError, ValueError):  # pragma: no cover - closed or unusual streams
        return False


def _install_logging(level: int = logging.WARNING) -> logging.Handler:
    """Attach a stderr handler to the rocket logger for this run; removed afterwards."""
    logger = logging.getLogger("rocket")
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(level)
    return handler


def _remove_logging(handler: logging.Handler) -> None:
    logging.getLogger("rocket").removeHandler(handler)
    handler.close()
