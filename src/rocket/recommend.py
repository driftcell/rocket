"""Version recommendation for ``rocket init``, ``rocket add`` and ``rocket upgrade``.

Given catalog entries (or one ad-hoc plugin) and the chosen Minecraft release, each
provider picks the newest version the download pipeline can use exactly. The selection
policy lives in the providers themselves (providers.py); this module only iterates the
catalog in order and isolates per-entry failures.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .catalog import CatalogPlugin
from .context import provider_clients
from .errors import ProviderError, RocketError
from .http_client import HTTPClient
from .models import RecommendedPlugin, SkippedPlugin
from .providers import PROVIDERS, PluginProvider, fetch_paper_versions


@dataclass(frozen=True)
class RecommendationResult:
    """Plugins resolved to exact versions, plus the ones that had to be skipped."""

    plugins: tuple[RecommendedPlugin, ...]
    skipped: tuple[SkippedPlugin, ...]


def list_paper_versions(http: HTTPClient) -> tuple[str, ...]:
    """Downloadable Paper releases, newest first (rc/pre builds are filtered out)."""
    groups = fetch_paper_versions(http)
    versions: dict[str, None] = {}
    for members in groups.values():
        for member in members:
            if "-rc" in member or "-pre" in member:
                continue
            versions.setdefault(member, None)
    return tuple(versions)


def recommend_plugins(
    plugins: Sequence[CatalogPlugin],
    *,
    minecraft_version: str,
    http: HTTPClient,
) -> RecommendationResult:
    """Resolve the newest usable version for every catalog entry; failures are skipped."""
    clients = provider_clients(http)
    providers: dict[str, PluginProvider] = {}
    resolved: list[RecommendedPlugin] = []
    skipped: list[SkippedPlugin] = []
    for plugin in plugins:
        provider = providers.get(plugin.provider)
        if provider is None:
            provider_class = PROVIDERS.get(plugin.provider)
            if provider_class is None:
                skipped.append(
                    SkippedPlugin(plugin.id, f"unsupported provider {plugin.provider!r}")
                )
                continue
            provider = provider_class(clients)
            providers[plugin.provider] = provider
        try:
            resolved.append(
                provider.recommend(
                    plugin.project,
                    templates=plugin.assets,
                    minecraft_version=minecraft_version,
                    plugin_id=plugin.id,
                )
            )
        except RocketError as exc:
            skipped.append(SkippedPlugin(plugin.id, exc.message))
        except Exception as exc:  # pragma: no cover - unexpected errors surface as skips
            skipped.append(
                SkippedPlugin(plugin.id, f"unexpected error ({exc.__class__.__name__}): {exc}")
            )
    return RecommendationResult(plugins=tuple(resolved), skipped=tuple(skipped))


def recommend_plugin(
    provider_name: str,
    project: str,
    *,
    asset: str | None,
    minecraft_version: str,
    http: HTTPClient,
) -> RecommendedPlugin:
    """Resolve the newest usable version of one ad-hoc plugin (used by ``rocket add``).

    Unlike :func:`recommend_plugins`, a resolution failure is raised, not skipped.
    """
    provider_class = PROVIDERS.get(provider_name)
    if provider_class is None:
        raise ProviderError(f"unsupported provider {provider_name!r}")
    provider = provider_class(provider_clients(http))
    return provider.recommend(
        project,
        templates=(asset,) if asset is not None else (),
        minecraft_version=minecraft_version,
        plugin_id="plugin",
    )
