"""Command-line entry point: ``rocket download``, ``rocket init`` and ``rocket search``."""

from __future__ import annotations

import argparse
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from blessed import Terminal

from . import USER_AGENT, __version__
from .catalog import asset_template
from .config import Config, PluginConfig, is_valid_plugin_id, load_config
from .config_edit import (
    PaperPin,
    PluginPin,
    add_plugin_section,
    remove_plugin_section,
    update_version_pins,
)
from .console import install_logging, print_error, remove_logging, stdin_is_tty, terminal
from .context import PipelineContext, provider_clients
from .downloader import HTTPDownloader
from .errors import (
    EXIT_FAILURE,
    EXIT_OK,
    ExternalNotFollowedError,
    LockError,
    ProviderError,
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
    SearchResult,
    Status,
)
from .modrinth import ModrinthClient
from .providers import (
    PROVIDERS,
    FillPaperProvider,
    fetch_newest_stable_build,
    fetch_paper_versions,
    fetch_stable_build_ids,
)
from .recommend import recommend_plugin
from .resolvers import select_resolver
from .spiget import SpigetClient
from .wizard import Prompter, QuestionaryPrompter, run_init

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
        help="Generate a starter server.toml",
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
    init_parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="ask for the Paper version and plugins instead of using defaults",
    )
    search_parser = subparsers.add_parser(
        "search",
        help="Search Hangar, Modrinth and Spiget for plugins",
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
    upgrade_parser = subparsers.add_parser(
        "upgrade",
        help="Check for newer versions, apply the updates, then download",
    )
    upgrade_parser.add_argument(
        "target",
        nargs="?",
        default=None,
        choices=("all", "plugins", "core"),
        help="what to upgrade (default: all; --interactive asks)",
    )
    upgrade_parser.add_argument(
        "--config",
        default="server.toml",
        help="path to the config file (default: ./server.toml)",
    )
    upgrade_parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="choose the target, Paper jump and plugin updates interactively",
    )
    pin_parser = subparsers.add_parser(
        "pin",
        help="Pin core or a plugin to an exact version, then download",
    )
    pin_parser.add_argument(
        "target",
        nargs="?",
        default=None,
        help="what to pin: 'core' or a plugin id (default: required unless --interactive)",
    )
    pin_parser.add_argument(
        "--config",
        default="server.toml",
        help="path to the config file (default: ./server.toml)",
    )
    pin_parser.add_argument(
        "--version",
        help="exact version to pin (required unless --interactive)",
    )
    pin_parser.add_argument(
        "--build",
        type=int,
        help="Paper build id to pin; core only (default: newest STABLE build, unpinned)",
    )
    pin_parser.add_argument(
        "--asset",
        help="exact upstream filename; plugin only, needed when a release has multiple files",
    )
    pin_parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="choose the target, version and file interactively",
    )
    args = parser.parse_args(argv)
    if args.command == "init":
        return run_init(
            Path(args.config),
            force=args.force,
            interactive=args.interactive,
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
    if args.command == "upgrade":
        return run_upgrade(
            args.target,
            interactive=args.interactive,
            config_path=Path(args.config),
            transport=transport,
            sleep=sleep,
            prompter=prompter,
        )
    if args.command == "pin":
        return run_pin(
            args.target,
            version=args.version,
            build=args.build,
            asset=args.asset,
            interactive=args.interactive,
            config_path=Path(args.config),
            transport=transport,
            sleep=sleep,
            prompter=prompter,
        )
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
    """Search every searchable provider; return 0 when the search ran, 1 when one failed.

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
        clients = provider_clients(http)
        for provider_class in PROVIDERS.values():
            if not provider_class.supports_search:
                continue
            provider = provider_class(clients)
            try:
                results.extend(provider.search(query, limit=_SEARCH_LIMIT))
            except RocketError as exc:
                failures += 1
                print_error(f"{provider.name}: {exc.message}", hint=exc.hint)
            except Exception as exc:
                failures += 1
                _print_unexpected_error(provider.name, exc)
    if results:
        _print_search_results(results)
    elif not failures:
        print(f"no plugins found for {query!r}")
    return EXIT_FAILURE if failures else EXIT_OK


def _print_search_results(results: list[SearchResult]) -> None:
    """Print one block per hit: a bold linked name, dim coordinates and an indented tagline.

    Hits from all providers are merged and ordered by downloads (unknown last). On a TTY
    the name is a cmd+clickable link to the project page (OSC 8) and descriptions are
    truncated to the terminal width; off a TTY everything degrades to plain text.
    """
    term = terminal()
    ordered = sorted(
        results, key=lambda result: (result.downloads is None, -(result.downloads or 0))
    )
    width = term.width or 80  # degenerate ptys can report 0; fall back like shutil does
    description_width = max(width - 5, 20)
    for index, result in enumerate(ordered):
        if index:
            print()
        spec = f"{result.provider}:{result.project}"
        meta = (
            f" · {_format_count(result.downloads)} downloads"
            if result.downloads is not None
            else ""
        )
        # A name that merely restyles the slug ("EssentialsX" vs "essentialsx") adds no
        # information; lead with the spec instead so long owner/name pairs still fit.
        title, tail = (result.name, f" — {spec}{meta}") if _name_adds_info(result) else (spec, meta)
        page_url = PROVIDERS[result.provider].page_url(result.project)
        print("  " + term.link(page_url, term.bold(title)) + term.dim(tail))
        description = result.description
        if description:
            wrapped = term.wrap(description, width=description_width)
            if wrapped:
                first = wrapped[0]
                if len(wrapped) > 1:
                    first = first.rstrip() + "…"
                print(f"    {term.dim(first)}")


def _name_adds_info(result: SearchResult) -> bool:
    """Whether the display name differs enough from the project slug to show both."""

    def normalize(text: str) -> str:
        return re.sub(r"[\s_]+", "-", text.strip().lower())

    return normalize(result.name) != normalize(result.project.rsplit("/", 1)[-1])


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
    project = PROVIDERS[provider].parse_project(project, field_path="spec")
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
        spiget=SpigetClient(http),
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
        code, _failed_ids = _run_download(
            config_path, transport=transport, sleep=sleep, verbose=verbose, upgrade=upgrade
        )
        return code
    finally:
        remove_logging(handler)


def _run_download(
    config_path: Path,
    *,
    transport: httpx.BaseTransport | None,
    sleep: Callable[[float], None],
    verbose: bool,
    upgrade: bool,
    refresh: frozenset[str] = frozenset(),
) -> tuple[int, tuple[str, ...]]:
    """Run one download; return the exit code and the ids of the items that failed.

    ``refresh`` excludes the given item ids from lock reuse (``rocket upgrade`` uses it to
    re-resolve the Paper build without touching the plugin entries).
    """
    try:
        config = load_config(config_path)
    except RocketError as exc:
        print_error(exc.message, hint=exc.hint)
        return exc.exit_code, ()
    for warning in config.warnings:
        _log.warning("config warning: %s", warning)

    lock_file = config.path.parent / LOCK_FILENAME
    previous_lock, lock_warnings = load_lock(lock_file)
    for warning in lock_warnings:
        _log.warning("lock warning: %s", warning)
    reuse_lock = None if upgrade else previous_lock
    if reuse_lock is not None and refresh:
        reuse_lock = LockData(
            paper=None if "paper" in refresh else reuse_lock.paper,
            plugins={key: value for key, value in reuse_lock.plugins.items() if key not in refresh},
        )

    entries: list[DownloadResult] = []
    fresh_entries: dict[str, LockedEntry] = {}
    failures = 0
    failed_ids: list[str] = []
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
            spiget=SpigetClient(http),
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
                failed_ids.append(item.id)
                _print_item_error(item.id, exc)
                continue
            except Exception as exc:
                failures += 1
                failed_ids.append(item.id)
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
                failed_ids.append(pending[index].artifact_id)
                _print_item_error(pending[index].artifact_id, outcome)
            elif isinstance(outcome, Exception):
                failures += 1
                failed_ids.append(pending[index].artifact_id)
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
    term = terminal()
    _print_run_summary(
        resolved, downloaded, unchanged, resolve_seconds, download_seconds, term=term
    )
    for result in entries:
        _print_item_result(result, verbose=verbose, term=term)
    return EXIT_FAILURE if failures else EXIT_OK, tuple(failed_ids)


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


# ---------------------------------------------------------------------------
# rocket upgrade
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CoreCandidates:
    """Available Paper core upgrades, smallest to largest."""

    build: int | None  # newer STABLE build for the same Minecraft version
    patch: str | None  # newest release of the current Fill version group
    patch_group: str | None
    major: str | None  # newest release of a newer Fill version group
    major_group: str | None


@dataclass(frozen=True)
class _CoreChoice:
    """The user's pick: a newer build of the same release, or a newer Minecraft release."""

    kind: str  # "build" or "mc"
    value: int | str


@dataclass(frozen=True)
class _PluginUpdate:
    """A plugin whose pinned version/asset differs from the current recommendation."""

    plugin: PluginConfig
    version: str
    asset: str | None
    note: str


def run_upgrade(
    target: str | None,
    *,
    interactive: bool,
    config_path: Path,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
    prompter: Prompter | None = None,
) -> int:
    """Check for newer versions, apply the selected bumps, rewrite server.toml and download.

    Non-interactive runs apply every available update (a core bump takes the newest Minecraft
    line). ``--interactive`` chooses the target first and then the Paper jump and plugin
    updates. Returns 0 when everything applied (or nothing needed doing), 1 on check/download
    failures, 2 on invalid config. ``transport``/``sleep``/``prompter`` are only for test
    injection.
    """
    if interactive and prompter is None:
        if not stdin_is_tty():
            print_error(
                "rocket upgrade --interactive requires a terminal",
                hint="run it in a terminal, or omit --interactive to apply every update",
            )
            return EXIT_FAILURE
        prompter = QuestionaryPrompter()
    handler = install_logging()
    try:
        return _run_upgrade(
            target,
            interactive=interactive,
            config_path=config_path,
            transport=transport,
            sleep=sleep,
            prompter=prompter,
        )
    finally:
        remove_logging(handler)


def _run_upgrade(
    target: str | None,
    *,
    interactive: bool,
    config_path: Path,
    transport: httpx.BaseTransport | None,
    sleep: Callable[[float], None],
    prompter: Prompter | None,
) -> int:
    try:
        config = load_config(config_path)
    except RocketError as exc:
        print_error(exc.message, hint=exc.hint)
        return exc.exit_code
    for warning in config.warnings:
        _log.warning("config warning: %s", warning)

    selected_target = target
    if selected_target is None:
        if interactive:
            if prompter is None:  # unreachable: the guard in run_upgrade provides one
                raise RocketError("internal error: interactive upgrade requires a prompter")
            selected_target = prompter.select(
                "What to upgrade",
                [
                    ("Paper core", "core"),
                    ("plugins", "plugins"),
                    ("Paper core + plugins", "all"),
                ],
                default="all",
            )
        else:
            selected_target = "all"

    if selected_target == "core":
        print(f"Checking Paper {config.paper.version} for updates...")
    elif selected_target == "plugins":
        print(
            f"Checking updates for {_plural(len(config.plugins), 'plugin')} "
            f"against Paper {config.paper.version}..."
        )
    else:
        print(
            f"Checking updates for Paper {config.paper.version} "
            f"+ {_plural(len(config.plugins), 'plugin')}..."
        )
    check_failed = False
    with HTTPClient(
        user_agent=config.http.user_agent,
        timeout_seconds=config.http.timeout_seconds,
        retries=config.http.retries,
        transport=transport,
        sleep=sleep,
        github_token=os.environ.get("GITHUB_TOKEN"),
    ) as http:
        core_choice: _CoreChoice | None = None
        if selected_target in ("all", "core"):
            try:
                candidates = _core_candidates(config, http=http)
            except RocketError as exc:
                check_failed = True
                print_error(f"core: {exc.message}", hint=exc.hint)
            except Exception as exc:
                check_failed = True
                _print_unexpected_error("core", exc)
            else:
                core_choice = _choose_core(
                    config.paper.version,
                    candidates,
                    interactive=interactive,
                    prompter=prompter,
                )

        new_mc = (
            str(core_choice.value) if core_choice is not None and core_choice.kind == "mc" else None
        )
        updates: list[_PluginUpdate] = []
        if selected_target in ("all", "plugins"):
            updates, plugin_check_failed = _check_plugin_updates(
                config, minecraft_version=new_mc or config.paper.version, http=http
            )
            check_failed = check_failed or plugin_check_failed
            if updates and interactive:
                if prompter is None:  # unreachable: the guard in run_upgrade provides one
                    raise RocketError("internal error: interactive upgrade requires a prompter")
                chosen = set(
                    prompter.checkbox(
                        "Plugins to upgrade",
                        [(_update_title(update), update.plugin.id) for update in updates],
                    )
                )
                updates = [update for update in updates if update.plugin.id in chosen]

    paper_pin: PaperPin | None = None
    original_paper_pin: PaperPin | None = None
    refresh: frozenset[str] = frozenset()
    if core_choice is not None:
        original_paper_pin = PaperPin(version=config.paper.version, build=config.paper.build)
        if core_choice.kind == "build":
            if config.paper.build is not None:
                paper_pin = PaperPin(version=config.paper.version, build=int(core_choice.value))
            else:
                # an unpinned build re-resolves to the newest STABLE build; the lock
                # entry is the only thing keeping the old one
                refresh = frozenset({"paper"})
        else:
            # a build pin belongs to its Minecraft release; it never carries over
            paper_pin = PaperPin(version=str(core_choice.value), build=None)

    plugin_pins = {
        update.plugin.id: PluginPin(version=update.version, asset=update.asset)
        for update in updates
    }
    if paper_pin is None and not plugin_pins and not refresh:
        if not check_failed:
            print("everything is up to date")
        return EXIT_FAILURE if check_failed else EXIT_OK

    original_plugins = {
        update.plugin.id: PluginPin(version=update.plugin.version, asset=update.plugin.asset)
        for update in updates
    }
    exit_code, failed_ids = _apply_pins_and_download(
        config_path,
        paper=paper_pin,
        plugins=plugin_pins,
        refresh=refresh,
        original_paper=original_paper_pin,
        original_plugins=original_plugins,
        transport=transport,
        sleep=sleep,
    )

    core_moved = selected_target == "core" and new_mc is not None and "paper" not in failed_ids
    if core_moved:
        print(
            f"next: rocket upgrade plugins   # re-check plugin compatibility "
            f"with Minecraft {new_mc}"
        )
    return EXIT_FAILURE if check_failed or exit_code != EXIT_OK else EXIT_OK


def _apply_pins_and_download(
    config_path: Path,
    *,
    paper: PaperPin | None,
    plugins: dict[str, PluginPin],
    refresh: frozenset[str],
    original_paper: PaperPin | None,
    original_plugins: dict[str, PluginPin],
    transport: httpx.BaseTransport | None,
    sleep: Callable[[float], None],
) -> tuple[int, tuple[str, ...]]:
    """Rewrite the pins, run the download, and roll failed pins back to the originals.

    ``original_paper``/``original_plugins`` describe the pre-edit state of the bumped
    items only; a bumped item that fails its download is restored. Returns the download
    exit code and the failed item ids.
    """
    if paper is not None or plugins:
        update_version_pins(config_path, paper=paper, plugins=plugins)
    exit_code, failed_ids = _run_download(
        config_path,
        transport=transport,
        sleep=sleep,
        verbose=False,
        upgrade=False,
        refresh=refresh,
    )
    rollback_paper = original_paper if "paper" in failed_ids else None
    rollback_plugins = {
        plugin_id: original_plugins[plugin_id]
        for plugin_id in failed_ids
        if plugin_id in original_plugins
    }
    if rollback_paper is not None or rollback_plugins:
        update_version_pins(config_path, paper=rollback_paper, plugins=rollback_plugins)
        rolled = len(rollback_plugins) + (1 if rollback_paper is not None else 0)
        print(f"rolled back {_plural(rolled, 'pin')} to the previous version")
    return exit_code, failed_ids


def _core_candidates(config: Config, *, http: HTTPClient) -> _CoreCandidates:
    """Compute the build/patch/major upgrade options for the configured Paper release."""
    groups = fetch_paper_versions(http)
    current = config.paper.version
    current_build = config.paper.build
    if current_build is None:
        lock, _lock_warnings = load_lock(config.path.parent / LOCK_FILENAME)
        if lock is not None and lock.paper is not None:
            current_build = lock.paper.build

    build = None
    if current_build is not None:
        newest = fetch_newest_stable_build(http, current)
        if newest is not None and newest > current_build:
            build = newest

    patch = patch_group = major = major_group = None
    for group, members in groups.items():
        stable = [member for member in members if "-rc" not in member and "-pre" not in member]
        if current in stable:
            newer = stable[: stable.index(current)]
            if newer:
                patch, patch_group = newer[0], group
            break  # groups are newest-line-first; anything beyond is older
        if stable and major is None:
            major, major_group = stable[0], group
    return _CoreCandidates(
        build=build, patch=patch, patch_group=patch_group, major=major, major_group=major_group
    )


def _choose_core(
    current: str,
    candidates: _CoreCandidates,
    *,
    interactive: bool,
    prompter: Prompter | None,
) -> _CoreChoice | None:
    options: list[tuple[str, _CoreChoice]] = []
    if candidates.build is not None:
        options.append(
            (
                f"build {candidates.build} (same Minecraft {current}, newer Paper build)",
                _CoreChoice(kind="build", value=candidates.build),
            )
        )
    if candidates.patch is not None:
        options.append(
            (
                f"{candidates.patch} (newest patch of the {candidates.patch_group} line)",
                _CoreChoice(kind="mc", value=candidates.patch),
            )
        )
    if candidates.major is not None:
        options.append(
            (
                f"{candidates.major} (new Minecraft line {candidates.major_group})",
                _CoreChoice(kind="mc", value=candidates.major),
            )
        )
    if not options:
        return None
    if not interactive:
        return options[-1][1]  # non-interactive: the full jump
    if prompter is None:  # unreachable: the guard in run_upgrade provides one
        raise RocketError("internal error: interactive upgrade requires a prompter")
    keep = "keep"
    choices = [(title, str(index)) for index, (title, _choice) in enumerate(options)]
    choices.append((f"keep {current}", keep))
    picked = prompter.select(f"Paper core (current: {current})", choices, default="0")
    if picked == keep:
        return None
    return options[int(picked)][1]


def _check_plugin_updates(
    config: Config, *, minecraft_version: str, http: HTTPClient
) -> tuple[list[_PluginUpdate], bool]:
    """Compare every plugin pin with the current recommendation; failures are reported."""
    updates: list[_PluginUpdate] = []
    failed = False
    for plugin in config.plugins:
        upgrade_note = PROVIDERS[plugin.provider].upgrade_note
        if upgrade_note is not None:
            print(f"note: {plugin.id} ({plugin.provider}) {upgrade_note}")
            continue
        template = None
        if plugin.asset is not None:
            # config.asset is a concrete filename of the pinned version; the catalog
            # form is a template with {version}
            template = asset_template(plugin.asset, plugin.version)
        try:
            recommended = recommend_plugin(
                plugin.provider,
                plugin.project,
                asset=template,
                minecraft_version=minecraft_version,
                http=http,
            )
        except RocketError as exc:
            failed = True
            print_error(f"{plugin.id}: {exc.message}", hint=exc.hint)
            continue
        except Exception as exc:
            failed = True
            _print_unexpected_error(plugin.id, exc)
            continue
        if recommended.version != plugin.version or recommended.asset != plugin.asset:
            updates.append(
                _PluginUpdate(
                    plugin=plugin,
                    version=recommended.version,
                    asset=recommended.asset,
                    note=recommended.note,
                )
            )
    return updates, failed


def _update_title(update: _PluginUpdate) -> str:
    plugin = update.plugin
    if update.version != plugin.version:
        title = f"{plugin.id}: {plugin.version} → {update.version} ({plugin.provider})"
    else:
        title = (
            f"{plugin.id}: {plugin.version} "
            f"(asset: {plugin.asset} → {update.asset}) ({plugin.provider})"
        )
    if update.note:
        title += f" — {update.note}"
    return title


# ---------------------------------------------------------------------------
# rocket pin
# ---------------------------------------------------------------------------

_PIN_AUTO_BUILD = "__auto__"


def run_pin(
    target: str | None,
    *,
    version: str | None,
    build: int | None,
    asset: str | None,
    interactive: bool,
    config_path: Path,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
    prompter: Prompter | None = None,
) -> int:
    """Pin core or one plugin to an exact version, then download.

    Non-interactive runs require ``target`` and ``version``; ``--build`` selects a Paper build
    and ``--asset`` selects a plugin file. ``--interactive`` chooses the target, version and
    file with prompts. Returns 0 when the pin applied (or nothing needed changing), 1 on
    failures, 2 on invalid config. ``transport``/``sleep``/``prompter`` are only for test
    injection.
    """
    if interactive and prompter is None:
        if not stdin_is_tty():
            print_error(
                "rocket pin --interactive requires a terminal",
                hint="run it in a terminal, or pass the target and --version explicitly",
            )
            return EXIT_FAILURE
        prompter = QuestionaryPrompter()
    handler = install_logging()
    try:
        return _run_pin(
            target,
            version=version,
            build=build,
            asset=asset,
            interactive=interactive,
            config_path=config_path,
            transport=transport,
            sleep=sleep,
            prompter=prompter,
        )
    finally:
        remove_logging(handler)


def _run_pin(
    target: str | None,
    *,
    version: str | None,
    build: int | None,
    asset: str | None,
    interactive: bool,
    config_path: Path,
    transport: httpx.BaseTransport | None,
    sleep: Callable[[float], None],
    prompter: Prompter | None,
) -> int:
    try:
        config = load_config(config_path)
    except RocketError as exc:
        print_error(exc.message, hint=exc.hint)
        return exc.exit_code
    for warning in config.warnings:
        _log.warning("config warning: %s", warning)

    plugin: PluginConfig | None = None
    if target is not None and target != "core":
        plugin = next((item for item in config.plugins if item.id == target), None)
        if plugin is None:
            ids = ", ".join(item.id for item in config.plugins) or "(no plugins declared)"
            print_error(f"no plugin with id {target!r}", hint=f"available targets: core, {ids}")
            return EXIT_FAILURE

    paper_pin: PaperPin | None = None
    plugin_pin: PluginPin | None = None
    if interactive:
        if version is not None or build is not None or asset is not None:
            print_error(
                "cannot combine --interactive with --version, --build or --asset",
                hint="omit the exact-value flags; --interactive asks for them",
            )
            return EXIT_FAILURE
        if target is None:
            if not config.plugins:
                target = "core"
            else:
                if prompter is None:  # unreachable: the guard in run_pin provides one
                    raise RocketError("internal error: interactive pin requires a prompter")
                choices = [("Paper core", "core")]
                choices += [
                    (f"plugin {item.id} ({item.provider}:{item.project})", item.id)
                    for item in config.plugins
                ]
                target = prompter.select("What to pin", choices, default="core")
            if target != "core":
                plugin = next((item for item in config.plugins if item.id == target), None)
                if plugin is None:  # unreachable: the choices come from the config itself
                    raise RocketError(f"internal error: plugin {target!r} vanished from the config")
    else:
        if target is None:
            print_error(
                "rocket pin needs a target",
                hint="pass 'core' or a plugin id, or use --interactive to choose",
            )
            return EXIT_FAILURE
        if version is None:
            print_error(
                "rocket pin needs --version",
                hint="pass the exact version, or use --interactive to choose",
            )
            return EXIT_FAILURE
        version = version.strip()
        if not version:
            print_error("--version must not be empty")
            return EXIT_FAILURE
        if asset is not None:
            asset = asset.strip()
            if not asset:
                print_error("--asset must not be empty")
                return EXIT_FAILURE
        if plugin is None:
            if asset is not None:
                print_error(
                    "--asset only applies to plugins",
                    hint="pin a plugin id instead of core, or omit --asset",
                )
                return EXIT_FAILURE
            if build is not None and build < 1:
                print_error("--build must be a positive integer")
                return EXIT_FAILURE
            if version == config.paper.version and build == config.paper.build:
                print("nothing to change")
                return EXIT_OK
            paper_pin = PaperPin(version=version, build=build)
        else:
            if build is not None:
                print_error(
                    "--build only applies to core",
                    hint="pin core instead, or omit --build",
                )
                return EXIT_FAILURE
            if version == plugin.version and asset == plugin.asset:
                print("nothing to change")
                return EXIT_OK
            plugin_pin = PluginPin(version=version, asset=asset)

    with HTTPClient(
        user_agent=config.http.user_agent,
        timeout_seconds=config.http.timeout_seconds,
        retries=config.http.retries,
        transport=transport,
        sleep=sleep,
        github_token=os.environ.get("GITHUB_TOKEN"),
    ) as http:
        try:
            if interactive:
                if plugin is None:
                    paper_pin = _pin_core(config, http=http, prompter=_require_prompter(prompter))
                else:
                    plugin_pin = _pin_plugin(
                        plugin, config, http=http, prompter=_require_prompter(prompter)
                    )
            if paper_pin is not None:
                exit_code, _failed = _apply_pins_and_download(
                    config_path,
                    paper=paper_pin,
                    plugins={},
                    refresh=frozenset(),
                    original_paper=PaperPin(version=config.paper.version, build=config.paper.build),
                    original_plugins={},
                    transport=transport,
                    sleep=sleep,
                )
            elif plugin is not None and plugin_pin is not None:
                exit_code, _failed = _apply_pins_and_download(
                    config_path,
                    paper=None,
                    plugins={plugin.id: plugin_pin},
                    refresh=frozenset(),
                    original_paper=None,
                    original_plugins={
                        plugin.id: PluginPin(version=plugin.version, asset=plugin.asset)
                    },
                    transport=transport,
                    sleep=sleep,
                )
            else:
                print("nothing to change")
                return EXIT_OK
        except RocketError as exc:
            print_error(exc.message, hint=exc.hint)
            return EXIT_FAILURE
        except Exception as exc:
            _print_unexpected_error(target or "pin", exc)
            return EXIT_FAILURE
    return exit_code


def _require_prompter(prompter: Prompter | None) -> Prompter:
    if prompter is None:  # unreachable: the guards in run_pin/_run_pin provide one
        raise RocketError("internal error: interactive pin requires a prompter")
    return prompter


def _pin_core(config: Config, *, http: HTTPClient, prompter: Prompter) -> PaperPin | None:
    """Two-level exact Paper selection (release line → version → build); None = no change."""
    groups = fetch_paper_versions(http)
    stable_groups = [
        (group, [member for member in members if "-rc" not in member and "-pre" not in member])
        for group, members in groups.items()
    ]
    stable_groups = [(group, members) for group, members in stable_groups if members]
    if not stable_groups:
        raise ProviderError("Fill project response has no usable stable version groups")

    current = config.paper.version
    current_group = next(
        (group for group, members in stable_groups if current in members), stable_groups[0][0]
    )
    line = prompter.select(
        f"Paper release line (current: {current})",
        [(group, group) for group, _members in stable_groups],
        default=current_group,
    )
    members = dict(stable_groups)[line]
    default_version = current if current in members else members[0]
    version = prompter.select(
        f"Paper version (line {line})",
        [
            (f"{member} (current)", member) if member == current else (member, member)
            for member in members
        ],
        default=default_version,
    )

    builds = fetch_stable_build_ids(http, version)
    if not builds:
        raise ProviderError(f"Paper {version} has no STABLE builds", hint="pick another version")
    build_choices = [("always the newest STABLE build (unpinned)", _PIN_AUTO_BUILD)]
    build_choices += [(f"build {build} (pinned)", str(build)) for build in builds[:10]]
    default_build = (
        str(config.paper.build)
        if version == current and config.paper.build in builds
        else _PIN_AUTO_BUILD
    )
    picked = prompter.select(f"Paper build for {version}", build_choices, default=default_build)
    build = None if picked == _PIN_AUTO_BUILD else int(picked)
    if version == current and build == config.paper.build:
        return None
    return PaperPin(version=version, build=build)


def _version_choice_title(name: str, kind: str, *, current: bool) -> str:
    """Pin-catalog label; some providers (zrips.net) have entries without any detail."""
    title = f"{name} · {kind}" if kind else name
    return f"{title} (current)" if current else title


def _pin_plugin(
    plugin: PluginConfig, config: Config, *, http: HTTPClient, prompter: Prompter
) -> PluginPin | None:
    """Exact version (and file, when ambiguous) for one plugin; None = no change."""
    minecraft_version = config.paper.version
    provider = PROVIDERS[plugin.provider](provider_clients(http))
    versions = provider.list_versions(plugin.project, minecraft_version=minecraft_version)

    choices = [
        (
            _version_choice_title(name, kind, current=name == plugin.version),
            name,
        )
        for name, kind in versions
    ]
    default = (
        plugin.version if any(name == plugin.version for name, _kind in versions) else choices[0][1]
    )
    version = prompter.select(
        f"{plugin.id} version (current: {plugin.version})", choices, default=default
    )

    asset, file_choices = provider.file_choices(plugin.project, version=version)
    if file_choices:
        asset = prompter.select(
            f"{plugin.id} {version} file",
            [(name, name) for name in file_choices],
            default=file_choices[0],
        )
    if version == plugin.version and asset == plugin.asset:
        return None
    return PluginPin(version=version, asset=asset)


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
    return provider_class(context).resolve(item.plugin, minecraft_version=context.minecraft_version)


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
    *,
    term: Terminal,
) -> None:
    if resolved:
        print(_phase_line(f"Resolved {_plural(resolved, 'item')}", resolve_seconds, term=term))
    if downloaded:
        tail = f" ({unchanged} unchanged)" if unchanged else ""
        print(
            _phase_line(f"Downloaded {_plural(downloaded, 'item')}", download_seconds, term=term)
            + tail
        )
    elif unchanged:
        print(_phase_line(f"Audited {_plural(unchanged, 'item')}", download_seconds, term=term))


def _phase_line(text: str, seconds: float, *, term: Terminal) -> str:
    return f"{text} in {term.dim(_format_duration(seconds))}"


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


def _format_count(value: int) -> str:
    """Render a download count like ``845``, ``12.3K`` or ``5.2M``."""
    if value < 1_000:
        return str(value)
    if value < 999_950:  # below this, one decimal never rounds up to the next unit
        return f"{value / 1_000:.1f}K"
    if value < 999_950_000:
        return f"{value / 1_000_000:.1f}M"
    return f"{value / 1_000_000_000:.1f}G"


def _print_item_result(result: DownloadResult, *, verbose: bool, term: Terminal) -> None:
    downloaded = result.status is Status.DOWNLOADED
    if downloaded:
        print(_format_item_line(result, downloaded=True, term=term))
    elif verbose:
        print(_format_item_line(result, downloaded=False, term=term))
    if verbose:
        print(term.dim(_format_item_detail(result)))


def _format_item_line(result: DownloadResult, *, downloaded: bool, term: Terminal) -> str:
    version = result.version
    if result.detail:
        version = f"{version} {result.detail}"
    chain = " → ".join(result.source_chain)
    marker = term.green("+") if downloaded else term.dim("=")
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
