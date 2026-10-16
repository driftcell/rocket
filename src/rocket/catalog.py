"""Curated default plugin catalog for ``rocket init``.

No versions are stored here: the wizard resolves the exact version of every entry
for the chosen Minecraft release at init time. ``assets`` holds upstream filename
templates (``{version}`` is substituted) for sources where the exact attachment has
to be picked from a release page; Hangar-hosted single files need no asset.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CatalogPlugin:
    """One selectable plugin; maps to one [plugins.<id>] section."""

    id: str
    name: str
    description: str
    provider: str
    project: str
    assets: tuple[str, ...] = ()


DEFAULT_PLUGINS: tuple[CatalogPlugin, ...] = (
    CatalogPlugin(
        "placeholderapi",
        "PlaceholderAPI",
        "placeholder hub",
        "hangar",
        "HelpChat/PlaceholderAPI",
    ),
    CatalogPlugin(
        "viaversion",
        "ViaVersion",
        "newer clients on older servers",
        "hangar",
        "ViaVersion/ViaVersion",
    ),
    CatalogPlugin(
        "viabackwards",
        "ViaBackwards",
        "older clients on newer servers",
        "hangar",
        "ViaVersion/ViaBackwards",
    ),
    CatalogPlugin(
        "essentialsx",
        "EssentialsX",
        "commands, kits and economy",
        "hangar",
        "EssentialsX/Essentials",
        assets=("EssentialsX-{version}.jar",),
    ),
    CatalogPlugin(
        "essentialsx_chat",
        "EssentialsX Chat",
        "chat formatting module",
        "hangar",
        "EssentialsX/Essentials",
        assets=("EssentialsXChat-{version}.jar",),
    ),
    CatalogPlugin(
        "essentialsx_spawn",
        "EssentialsX Spawn",
        "spawn and warp module",
        "hangar",
        "EssentialsX/Essentials",
        assets=("EssentialsXSpawn-{version}.jar",),
    ),
    CatalogPlugin(
        "coreprotect",
        "CoreProtect",
        "block logging and rollback",
        "modrinth",
        "coreprotect",
    ),
    CatalogPlugin(
        "residence",
        "Residence",
        "land claiming and protection",
        "github",
        "Zrips/Residence",
        assets=("Residence{version}.jar",),
    ),
    CatalogPlugin(
        "cmilib",
        "CMILib",
        "shared library for Zrips plugins",
        "github",
        "Zrips/CMILib",
        assets=("CMILib{version}.jar",),
    ),
    CatalogPlugin(
        "luckperms",
        "LuckPerms",
        "permissions and groups",
        "modrinth",
        "luckperms",
    ),
    CatalogPlugin(
        "tab",
        "TAB",
        "tab list and scoreboard",
        "github",
        "NEZNAMY/TAB",
        assets=("TAB.v{version}.jar",),
    ),
    CatalogPlugin(
        "vault",
        "Vault",
        "economy and permission API",
        "github",
        "MilkBowl/Vault",
        assets=("Vault.jar",),
    ),
)
