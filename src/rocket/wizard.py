"""Interactive ``rocket init`` wizard: a few questions, recommended versions, server.toml."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

import httpx
import tomli_w

from . import USER_AGENT
from .catalog import DEFAULT_PLUGINS, CatalogPlugin
from .console import install_logging, print_error, remove_logging, stdin_is_tty, terminal
from .errors import EXIT_FAILURE, EXIT_OK, RocketError
from .http_client import HTTPClient
from .recommend import (
    RecommendationResult,
    RecommendedPlugin,
    list_paper_versions,
    recommend_plugins,
)

_log = logging.getLogger("rocket")

# referenced by the release skill and tests
INIT_USER_AGENT = USER_AGENT

_SERVER_TYPE_CHOICES = [("Paper", "paper")]


class _Aborted(Exception):
    """The user cancelled a prompt."""


class Prompter(Protocol):
    """Minimal interaction surface; tests inject a scripted implementation."""

    def select(self, message: str, choices: Sequence[tuple[str, str]], *, default: str) -> str:
        """Ask to pick one of (title, value); returns the chosen value."""
        ...

    def text(self, message: str, *, default: str = "") -> str:
        """Ask for free text; returns the raw answer."""
        ...

    def confirm(self, message: str, *, default: bool = True) -> bool:
        """Ask a yes/no question."""
        ...

    def checkbox(self, message: str, choices: Sequence[tuple[str, str]]) -> list[str]:
        """Ask to pick any of (title, value), all preselected; returns the selected values."""
        ...


class QuestionaryPrompter:
    """Prompter backed by questionary (imported lazily so plain downloads stay fast)."""

    def select(self, message: str, choices: Sequence[tuple[str, str]], *, default: str) -> str:
        import questionary

        answer = questionary.select(
            message,
            choices=[questionary.Choice(title, value) for title, value in choices],
            default=default,
        ).ask()
        return _answer(answer)

    def text(self, message: str, *, default: str = "") -> str:
        import questionary

        return _answer(questionary.text(message, default=default).ask())

    def confirm(self, message: str, *, default: bool = True) -> bool:
        import questionary

        return _answer(questionary.confirm(message, default=default).ask())

    def checkbox(self, message: str, choices: Sequence[tuple[str, str]]) -> list[str]:
        import questionary

        answer = questionary.checkbox(
            message,
            choices=[questionary.Choice(title, value, checked=True) for title, value in choices],
        ).ask()
        return _answer(answer)


def _answer[T](value: T | None) -> T:
    if value is None:
        raise _Aborted
    return value


def run_init(
    config_path: Path,
    *,
    force: bool = False,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
    prompter: Prompter | None = None,
    catalog: Sequence[CatalogPlugin] = DEFAULT_PLUGINS,
) -> int:
    """Run the wizard; return the process exit code (0 written, 1 aborted/failed).

    ``transport``/``sleep``/``prompter``/``catalog`` are only for test injection.
    """
    if prompter is None:
        if not _stdin_is_tty():
            print_error("rocket init is interactive; run it in a terminal")
            return EXIT_FAILURE
        prompter = QuestionaryPrompter()
    handler = install_logging()
    try:
        return _run_init(
            config_path,
            force=force,
            transport=transport,
            sleep=sleep,
            prompter=prompter,
            catalog=catalog,
        )
    except (KeyboardInterrupt, _Aborted):
        print_error("init aborted; no file was written")
        return EXIT_FAILURE
    finally:
        remove_logging(handler)


def _run_init(
    config_path: Path,
    *,
    force: bool,
    transport: httpx.BaseTransport | None,
    sleep: Callable[[float], None],
    prompter: Prompter,
    catalog: Sequence[CatalogPlugin],
) -> int:
    if config_path.exists() and not force:
        print_error(
            f"config file already exists: {config_path}",
            hint="pass --force to overwrite it",
        )
        return EXIT_FAILURE

    server_type = prompter.select("Server type", _SERVER_TYPE_CHOICES, default="paper")
    if server_type != "paper":
        print_error(f"unsupported server type: {server_type!r}")
        return EXIT_FAILURE

    with HTTPClient(
        user_agent=INIT_USER_AGENT,
        transport=transport,
        sleep=sleep,
        github_token=os.environ.get("GITHUB_TOKEN"),
    ) as http:
        paper_version = _ask_paper_version(prompter, http)
        if paper_version is None:
            return EXIT_FAILURE
        selected = _ask_plugins(prompter, catalog)
        if selected:
            print(f"Resolving {len(selected)} plugins for Paper {paper_version}...")
            result = recommend_plugins(selected, minecraft_version=paper_version, http=http)
            _report(result, selected=len(selected), paper_version=paper_version)
        else:
            result = RecommendationResult(plugins=(), skipped=())

    text = build_config_text(paper_version=paper_version, plugins=result.plugins)
    try:
        config_path.write_text(text, encoding="utf-8")
    except OSError as exc:
        print_error(
            f"cannot write config file {config_path}: {exc}",
            hint="check that the directory exists and is writable",
        )
        return EXIT_FAILURE
    print(f"wrote {config_path}")
    if config_path == Path("server.toml"):
        print("next: rocket download")
    else:
        print(f"next: rocket download --config {config_path}")
    return EXIT_OK


def _ask_paper_version(prompter: Prompter, http: HTTPClient) -> str | None:
    """Pick from the live Fill version list; fall back to manual input when unavailable."""
    try:
        versions = list_paper_versions(http)
    except RocketError as exc:
        versions = ()
        _log.warning("cannot list Paper versions (%s); enter one manually", exc.message)
    if versions:
        return prompter.select(
            "Paper version (newest first)",
            [(version, version) for version in versions],
            default=versions[0],
        )
    answer = prompter.text("Paper version (exact release, e.g. 1.21.11)").strip()
    if not answer:
        print_error("paper version must not be empty")
        return None
    return answer


def _ask_plugins(prompter: Prompter, catalog: Sequence[CatalogPlugin]) -> tuple[CatalogPlugin, ...]:
    if not catalog:
        return ()
    if not prompter.confirm("Add the default plugin set?", default=True):
        return ()
    choices = [(f"{item.name} ({item.provider}) — {item.description}", item.id) for item in catalog]
    selected = set(prompter.checkbox("Default plugins", choices))
    return tuple(item for item in catalog if item.id in selected)


def _report(result: RecommendationResult, *, selected: int, paper_version: str) -> None:
    for plugin in result.plugins:
        if plugin.note:
            _log.warning("[%s] warning: %s", plugin.id, plugin.note)
    for skipped in result.skipped:
        _log.warning("[%s] skipped: %s", skipped.id, skipped.reason)
    print(f"Resolved {len(result.plugins)} of {selected} plugins for Paper {paper_version}")
    term = terminal()
    for plugin in result.plugins:
        print(f"  {term.green('+')} {plugin.id} {plugin.version} ({plugin.provider})")


def build_config_text(*, paper_version: str, plugins: Sequence[RecommendedPlugin]) -> str:
    """Serialize a starter server.toml; field order mirrors server.example.toml."""
    data: dict[str, object] = {
        "schema_version": 1,
        "paper": {"version": paper_version, "channel": "STABLE"},
        "download": {"output_dir": "./artifacts"},
        "plugin_defaults": {"platform": "PAPER", "follow_external": True},
    }
    if plugins:
        entries: dict[str, object] = {}
        for plugin in plugins:
            entry: dict[str, object] = {
                "provider": plugin.provider,
                "project": plugin.project,
                "version": plugin.version,
            }
            if plugin.asset is not None:
                entry["asset"] = plugin.asset
            entries[plugin.id] = entry
        data["plugins"] = entries
    return tomli_w.dumps(data)


def _stdin_is_tty() -> bool:
    return stdin_is_tty()
