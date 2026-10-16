"""Command-line entry point: ``rocket download``, ``rocket init`` and ``rocket search``."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import USER_AGENT, __version__
from .config import Config, PluginConfig, is_valid_plugin_id, load_config
from .config_edit import add_plugin_section, remove_plugin_section
from .console import install_logging, print_error, remove_logging, style
from .context import PipelineContext
from .downloader import HTTPDownloader
from .errors import (
    EXIT_FAILURE,
    EXIT_OK,
    ExternalNotFollowedError,
    LockError,
    RocketError,
)
from .github import GitHubClient, parse_project
from .http_client import HTTPClient
from .lockfile import LOCK_FILENAME, LockData, LockedEntry, load_lock, write_lock
from .models import (
    Artifact,
    ArtifactKind,
    DownloadResult,
    EntryKind,
    ReleaseInfo,
    ResolverChoice,
    SearchResult,
    Status,
)
from .modrinth import ModrinthClient, parse_slug
from .providers import PROVIDERS, FillPaperProvider, search_hangar_projects
from .recommend import recommend_plugin
from .resolvers import select_resolver
from .wizard import Prompter, run_init

_log = logging.getLogger("rocket")

_SEARCH_LIMIT = 5
_DOWNLOAD_WORKERS = 4


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
    prompter: Prompter | None = None,
) -> int:
    """CLI entry point; ``transport``/``sleep``/``prompter`` are only for test injection."""
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
    init_parser = subparsers.add_parser(
        "init",
        help="Interactively generate a starter server.toml",
    )
    init_parser.add_argument(
        "--config",
        default="server.toml",
        help="path of the file to write (default: ./server.toml)",
    )
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing config file",
    )
    search_parser = subparsers.add_parser(
        "search",
        help="Search Hangar and Modrinth for plugins",
    )
    search_parser.add_argument(
        "query",
        help="search keywords, e.g. 'luckperms'",
    )
    add_parser = subparsers.add_parser(
        "add",
        help="Add a plugin to server.toml",
    )
    add_parser.add_argument(
        "spec",
        help="plugin coordinates: <provider>:<project>, e.g. modrinth:luckperms "
        "or hangar:ViaVersion/ViaVersion",
    )
    add_parser.add_argument(
        "--config",
        default="server.toml",
        help="path to the config file (default: ./server.toml)",
    )
    add_parser.add_argument(
        "--version",
        help="exact version to pin; the newest usable version is resolved when omitted",
    )
    add_parser.add_argument(
        "--asset",
        help="exact upstream filename, needed when a release has multiple files",
    )
    add_parser.add_argument(
        "--id",
        dest="plugin_id",
        help="local plugin id (default: derived from the project name)",
    )
    remove_parser = subparsers.add_parser(
        "remove",
        help="Remove a plugin from server.toml",
    )
    remove_parser.add_argument(
        "plugin_id",
        metavar="id",
        help="the plugin id to remove",
    )
    remove_parser.add_argument(
        "--config",
        default="server.toml",
        help="path to the config file (default: ./server.toml)",
    )
    args = parser.parse_args(argv)
    if args.command == "init":
        return run_init(
            Path(args.config),
            force=args.force,
            transport=transport,
            sleep=sleep,
            prompter=prompter,
        )
    if args.command == "search":
        return run_search(args.query, transport=transport, sleep=sleep)
    if args.command == "add":
        return run_add(
            args.spec,
            version=args.version,
            asset=args.asset,
            plugin_id=args.plugin_id,
            config_path=Path(args.config),
            transport=transport,
            sleep=sleep,
        )
    if args.command == "remove":
        return run_remove(args.plugin_id, config_path=Path(args.config))
    return run_download(
        Path(args.config),
        transport=transport,
        sleep=sleep,
        verbose=args.verbose,
        upgrade=args.upgrade,
    )


def run_search(
    query: str,
    *,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Search Hangar and Modrinth; return 0 when the search ran, 1 when a provider failed.

    ``transport``/``sleep`` are only for test injection.
    """
    handler = install_logging()
    try:
        return _run_search(query, transport=transport, sleep=sleep)
    finally:
        remove_logging(handler)


def _run_search(
    query: str,
    *,
    transport: httpx.BaseTransport | None,
    sleep: Callable[[float], None],
) -> int:
    query = query.strip()
    if not query:
        print_error("search query must not be empty", hint="example: rocket search luckperms")
        return EXIT_FAILURE
    results: list[SearchResult] = []
    failures = 0
    with HTTPClient(user_agent=USER_AGENT, transport=transport, sleep=sleep) as http:
        searches = (
            ("hangar", lambda: search_hangar_projects(http, query, limit=_SEARCH_LIMIT)),
            ("modrinth", lambda: ModrinthClient(http).search_projects(query, limit=_SEARCH_LIMIT)),
        )
        for provider, search in searches:
            try:
                results.extend(search())
            except RocketError as exc:
                failures += 1
                print_error(f"{provider}: {exc.message}", hint=exc.hint)
            except Exception as exc:
                failures += 1
                _print_unexpected_error(provider, exc)
    if results:
        _print_search_results(results)
    elif not failures:
        print(f"no plugins found for {query!r}")
    return EXIT_FAILURE if failures else EXIT_OK


def _print_search_results(results: list[SearchResult]) -> None:
    specs = [f"{result.provider}:{result.project}" for result in results]
    width = max(len(spec) for spec in specs)
    for spec, result in zip(specs, results, strict=True):
        line = f"  {spec.ljust(width)}  {result.name}"
        if result.description:
            line += f" — {result.description}"
        print(line)


def run_add(
    spec: str,
    *,
    version: str | None,
    asset: str | None,
    plugin_id: str | None,
    config_path: Path,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Add a plugin entry to server.toml; return the exit code (0 added, 1 failed, 2 config).

    ``transport``/``sleep`` are only for test injection.
    """
    handler = install_logging()
    try:
        return _run_add(
            spec,
            version=version,
            asset=asset,
            plugin_id=plugin_id,
            config_path=config_path,
            transport=transport,
            sleep=sleep,
        )
    finally:
        remove_logging(handler)


def _run_add(
    spec: str,
    *,
    version: str | None,
    asset: str | None,
    plugin_id: str | None,
    config_path: Path,
    transport: httpx.BaseTransport | None,
    sleep: Callable[[float], None],
) -> int:
    try:
        provider, project = _parse_add_spec(spec)
        plugin_id = _add_plugin_id(plugin_id, project)
        _check_add_option("version", version)
        _check_add_option("asset", asset)
    except RocketError as exc:
        print_error(exc.message, hint=exc.hint)
        return EXIT_FAILURE
    try:
        config = load_config(config_path)
    except RocketError as exc:
        print_error(exc.message, hint=exc.hint)
        return exc.exit_code
    if any(plugin.id == plugin_id for plugin in config.plugins):
        print_error(
            f"plugin id {plugin_id!r} already exists in {config.path}",
            hint="pass --id to use a different id, or run rocket remove first",
        )
        return EXIT_FAILURE
    try:
        with HTTPClient(
            user_agent=config.http.user_agent,
            timeout_seconds=config.http.timeout_seconds,
            retries=config.http.retries,
            transport=transport,
            sleep=sleep,
            github_token=os.environ.get("GITHUB_TOKEN"),
        ) as http:
            if version is None:
                resolved = _resolve_add_auto(
                    config,
                    http,
                    plugin_id=plugin_id,
                    provider=provider,
                    project=project,
                    asset=asset,
                )
            else:
                _verify_add_version(
                    config,
                    http,
                    plugin_id=plugin_id,
                    provider=provider,
                    project=project,
                    version=version,
                    asset=asset,
                )
                resolved = (version, asset)
        resolved_version, resolved_asset = resolved
        add_plugin_section(
            config.path,
            plugin_id=plugin_id,
            provider=provider,
            project=project,
            version=resolved_version,
            asset=resolved_asset,
        )
    except RocketError as exc:
        print_error(exc.message, hint=exc.hint)
        return exc.exit_code
    print(f"added {plugin_id} {resolved_version} ({provider})")
    _print_next_download(config_path)
    return EXIT_OK


def _parse_add_spec(spec: str) -> tuple[str, str]:
    """Split ``<provider>:<project>`` and validate both halves before any network request."""
    provider, separator, project = spec.partition(":")
    if not separator or not provider or not project:
        raise RocketError(
            f"invalid plugin spec: {spec!r}",
            hint="use <provider>:<project>, e.g. modrinth:luckperms "
            "or hangar:ViaVersion/ViaVersion",
        )
    if provider not in PROVIDERS:
        raise RocketError(
            f"unknown provider: {provider!r}",
            hint=f"available providers: {', '.join(PROVIDERS)}",
        )
    if provider == "modrinth":
        project = parse_slug(project, field_path="spec")
    else:
        parse_project(project, field_path="spec")
    return provider, project


def _add_plugin_id(plugin_id: str | None, project: str) -> str:
    if plugin_id is not None:
        if not is_valid_plugin_id(plugin_id):
            raise RocketError(
                f"invalid plugin id: {plugin_id!r}",
                hint="ids may only contain lowercase letters, digits, underscores and hyphens",
            )
        return plugin_id
    candidate = project.rsplit("/", 1)[-1].lower().replace(".", "-")
    if not is_valid_plugin_id(candidate):
        raise RocketError(
            f"cannot derive a plugin id from {project!r}",
            hint="pass --id explicitly",
        )
    return candidate


def _check_add_option(name: str, value: str | None) -> None:
    if value is not None and not value.strip():
        raise RocketError(f"--{name} must not be empty")


def _resolve_add_auto(
    config: Config,
    http: HTTPClient,
    *,
    plugin_id: str,
    provider: str,
    project: str,
    asset: str | None,
) -> tuple[str, str | None]:
    """Pick the newest usable version (and asset) and pin it, like rocket init does."""
    picked = recommend_plugin(
        provider,
        project,
        asset=asset,
        minecraft_version=config.paper.version,
        http=http,
    )
    if picked.note:
        _log.warning("[%s] warning: %s", plugin_id, picked.note)
    return picked.version, picked.asset


def _verify_add_version(
    config: Config,
    http: HTTPClient,
    *,
    plugin_id: str,
    provider: str,
    project: str,
    version: str,
    asset: str | None,
) -> None:
    """Fail fast when the declared exact version cannot be resolved by the download pipeline."""
    plugin = PluginConfig(
        id=plugin_id,
        provider=provider,
        project=project,
        version=version,
        asset=asset,
        platform=config.plugin_defaults.platform,
        follow_external=config.plugin_defaults.follow_external,
    )
    context = PipelineContext(
        http=http,
        github=GitHubClient(http),
        modrinth=ModrinthClient(http),
        minecraft_version=config.paper.version,
        output_dir=config.download.output_dir,
        log=_log,
    )
    item = _Item(
        id=plugin_id,
        kind=ArtifactKind.PLUGIN,
        follow_external=plugin.follow_external,
        plugin=plugin,
    )
    _resolve_artifact(item, config, context)


def run_remove(plugin_id: str, *, config_path: Path) -> int:
    """Remove a plugin entry from server.toml; return the exit code (0 removed, 1 failed)."""
    handler = install_logging()
    try:
        return _run_remove(plugin_id, config_path=config_path)
    finally:
        remove_logging(handler)


def _run_remove(plugin_id: str, *, config_path: Path) -> int:
    if not is_valid_plugin_id(plugin_id):
        print_error(
            f"invalid plugin id: {plugin_id!r}",
            hint="ids may only contain lowercase letters, digits, underscores and hyphens",
        )
        return EXIT_FAILURE
    try:
        config = load_config(config_path)
    except RocketError as exc:
        print_error(exc.message, hint=exc.hint)
        return exc.exit_code
    if not any(plugin.id == plugin_id for plugin in config.plugins):
        known = ", ".join(plugin.id for plugin in config.plugins) or "(none)"
        print_error(
            f"plugin {plugin_id!r} is not defined in {config.path}",
            hint=f"known plugin ids: {known}",
        )
        return EXIT_FAILURE
    try:
        remove_plugin_section(config.path, plugin_id)
    except RocketError as exc:
        print_error(exc.message, hint=exc.hint)
        return exc.exit_code
    print(f"removed {plugin_id}")
    jar = config.download.output_dir / "plugins" / f"{plugin_id}.jar"
    if jar.is_file():
        print(f"note: {jar} was kept on disk")
    return EXIT_OK


def _print_next_download(config_path: Path) -> None:
    if config_path == Path("server.toml"):
        print("next: rocket download")
    else:
        print(f"next: rocket download --config {config_path}")


def run_download(
    config_path: Path,
    *,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
    verbose: bool = False,
    upgrade: bool = False,
) -> int:
    """Run one download; return the process exit code (0 all ok, 1 failures, 2 invalid config)."""
    handler = install_logging(logging.INFO if verbose else logging.WARNING)
    try:
        return _run_download(
            config_path, transport=transport, sleep=sleep, verbose=verbose, upgrade=upgrade
        )
    finally:
        remove_logging(handler)


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
        print_error(exc.message, hint=exc.hint)
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
        pending: list[Artifact] = []
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
            pending.append(artifact)

        started = time.perf_counter()
        by_index = _download_all(downloader, pending)
        download_seconds = time.perf_counter() - started
        for index in sorted(by_index):
            outcome = by_index[index]
            if isinstance(outcome, RocketError):
                failures += 1
                _print_item_error(pending[index].artifact_id, outcome)
            elif isinstance(outcome, Exception):
                failures += 1
                _print_unexpected_error(pending[index].artifact_id, outcome)
            else:
                entries.append(outcome)

    if resolved or previous_lock is not None:
        try:
            changed = write_lock(lock_file, _merge_lock(config, fresh_entries, previous_lock))
        except LockError as exc:
            failures += 1
            print_error(exc.message, hint=exc.hint)
        else:
            if changed:
                _log.info("[lock] wrote %s", lock_file)

    downloaded = sum(1 for result in entries if result.status is Status.DOWNLOADED)
    unchanged = len(entries) - downloaded
    _print_run_summary(resolved, downloaded, unchanged, resolve_seconds, download_seconds)
    for result in entries:
        _print_item_result(result, verbose=verbose)
    return EXIT_FAILURE if failures else EXIT_OK


def _download_all(
    downloader: HTTPDownloader, artifacts: list[Artifact]
) -> dict[int, DownloadResult | Exception]:
    """Download artifacts concurrently; outcomes keep the config-order index.

    Resolution stays sequential (provider clients cache in-run); downloads fan out to a
    small pool: httpx.Client and the downloader are thread-safe, and each artifact writes
    only its own target. Failures are captured per item and reported by the caller.
    On a TTY a single shared progress bar (items plus transferred bytes) is shown and
    erased on completion; off a TTY tqdm disables itself and output is unchanged.
    """
    outcomes: dict[int, DownloadResult | Exception] = {}
    if not artifacts:
        return outcomes

    from tqdm import tqdm  # lazy import, like questionary: plain runs stay fast

    total_bytes = sum(artifact.size for artifact in artifacts if artifact.size is not None)
    transferred = 0
    lock = threading.Lock()
    with tqdm(
        total=len(artifacts), unit="item", desc="Downloading", leave=False, disable=None
    ) as progress:

        def on_bytes(delta: int) -> None:
            nonlocal transferred
            with lock:
                transferred += delta
                if total_bytes:
                    progress.set_postfix_str(
                        f"{_format_size(transferred)}/{_format_size(total_bytes)}"
                    )

        with ThreadPoolExecutor(max_workers=min(_DOWNLOAD_WORKERS, len(artifacts))) as pool:
            futures = {
                pool.submit(downloader.fetch, artifact, on_bytes=on_bytes): index
                for index, artifact in enumerate(artifacts)
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    outcomes[index] = future.result()
                except Exception as exc:  # per-item failure isolation; reported by the caller
                    outcomes[index] = exc
                progress.update(1)
    return outcomes


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
        print(_phase_line(f"Downloaded {_plural(downloaded, 'item')}", download_seconds) + tail)
    elif unchanged:
        print(_phase_line(f"Audited {_plural(unchanged, 'item')}", download_seconds))


def _phase_line(text: str, seconds: float) -> str:
    return f"{text} in {style(_format_duration(seconds), '2', stream=sys.stdout)}"


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


def _format_size(value: int) -> str:
    """Render a byte count like ``512B``, ``12.3MB`` or ``2.00GB``."""
    if value < 1024:
        return f"{value}B"
    if value < 1024**2:
        return f"{value / 1024:.1f}KB"
    if value < 1024**3:
        return f"{value / 1024**2:.1f}MB"
    return f"{value / 1024**3:.2f}GB"


def _print_item_result(result: DownloadResult, *, verbose: bool) -> None:
    downloaded = result.status is Status.DOWNLOADED
    if downloaded:
        print(_format_item_line(result, downloaded=True))
    elif verbose:
        print(_format_item_line(result, downloaded=False))
    if verbose:
        print(style(_format_item_detail(result), "2", stream=sys.stdout))


def _format_item_line(result: DownloadResult, *, downloaded: bool) -> str:
    version = result.version
    if result.detail:
        version = f"{version} {result.detail}"
    chain = " → ".join(result.source_chain)
    marker = (
        style("+", "32", stream=sys.stdout) if downloaded else style("=", "2", stream=sys.stdout)
    )
    return f" {marker} {result.target.name} ({version}, {chain})"


def _format_item_detail(result: DownloadResult) -> str:
    if result.upstream_hash_verified:
        hash_state = "upstream hash verified"
    else:
        hash_state = "upstream hash unavailable"
    checksum = f"{result.checksum.algorithm}={result.checksum.value}"
    return f"    {result.target} {result.size} bytes {checksum} ({hash_state})"


def _print_item_error(item_id: str, exc: RocketError) -> None:
    print_error(f"{item_id}: {exc.message}", hint=exc.hint)


def _print_unexpected_error(item_id: str, exc: Exception) -> None:
    _log.debug("[%s] unexpected error details", item_id, exc_info=True)
    unexpected = RocketError(f"unexpected error ({exc.__class__.__name__}): {exc}")
    _print_item_error(item_id, unexpected)
