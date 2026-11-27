"""Omnigent Extension API v1 manifest for the Coldtake Scorecard UI."""

from __future__ import annotations

from omnigent.extensions.api import (
    EXTENSION_API_VERSION,
    ExtensionEntrypoints,
    ExtensionManifest,
    ExtensionPermission,
    PageContribution,
    PrimaryNavigationContribution,
)

__version__ = "0.1.0"

EXTENSION_ID = "vicennial.coldtake"
PAGE_ID = "vicennial.coldtake.scorecard"
NAV_ID = "vicennial.coldtake.nav"


def manifest() -> ExtensionManifest:
    """Return the declarative Omnigent ExtensionManifest (API v1)."""
    return ExtensionManifest(
        id=EXTENSION_ID,
        display_name="Coldtake",
        distribution="coldtake-omnigent",
        version=__version__,
        requires_omnigent=">=0.16.0",
        extension_api=EXTENSION_API_VERSION,
        entrypoints=ExtensionEntrypoints(
            browser="dist/extension.js",
            browser_css="dist/extension.css",
        ),
        permissions=frozenset(
            {
                ExtensionPermission.NAVIGATION,
                ExtensionPermission.SESSIONS_READ,
                ExtensionPermission.STORAGE_USER,
            }
        ),
        activation_events=(f"onPage:{PAGE_ID}",),
        pages=(
            PageContribution(
                id=PAGE_ID,
                title="Coldtake Scorecard",
                route="scorecard",
                view="scorecard",
            ),
        ),
        primary_navigation=(
            PrimaryNavigationContribution(
                id=NAV_ID,
                label="Coldtake",
                page=PAGE_ID,
                icon="scale",
                order=450,
            ),
        ),
    )


__all__ = ["EXTENSION_ID", "NAV_ID", "PAGE_ID", "__version__", "manifest"]
