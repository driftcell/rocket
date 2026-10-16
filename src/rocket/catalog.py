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
        "zrips",
        "Residence",
    ),
    CatalogPlugin(
        "cmilib",
        "CMILib",
        "shared library for Zrips plugins",
        "zrips",
        "CMILib",
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


def asset_template(asset: str, version: str) -> str:
    """Recover a catalog-style ``{version}`` template from a concrete pinned filename.

    Filenames do not always spell the version tag literally (``v5.5.60`` may appear as
    ``5.5.60``), so every filename form of the pinned version is tried.
    """
    for form in version_forms(version):
        if form in asset:
            return asset.replace(form, "{version}")
    return asset


def version_forms(version: str) -> tuple[str, ...]:
    """Filename spellings of a version tag: itself, without a ``v`` prefix, without suffixes."""
    forms = [version]
    unv = version[1:] if version.startswith("v") else version
    if unv != version:
        forms.append(unv)
    for base in (version, unv):
        stem = base.split("-", 1)[0]
        if stem and stem not in forms:
            forms.append(stem)
    return tuple(forms)
