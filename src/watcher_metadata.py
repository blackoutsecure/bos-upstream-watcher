"""Package identity, independent of repository policy configuration.

Identity (name, version, author, description, legal details, and official
links) is owned by the action — never by config. Reserved identity keys are
stripped from every config tier before merging, so a repo, org, or inline
override cannot rebrand or misreport the action that is actually running.
"""

from __future__ import annotations

from typing import Any

PACKAGE_NAME = "bos-upstream-watcher"
PACKAGE_TITLE = "Blackout Secure Upstream Watcher"
PACKAGE_AUTHOR = "Blackout Secure"
PACKAGE_DESCRIPTION = (
    "Config-driven upstream version discovery — detect new releases, tags, "
    "packages, and container images, and report whether they changed."
)
PACKAGE_WEBSITE = "https://blackoutsecure.app"
PACKAGE_REPOSITORY = "https://github.com/blackoutsecure/bos-upstream-watcher"
PACKAGE_DOCUMENTATION = f"{PACKAGE_REPOSITORY}#readme"
PACKAGE_ISSUES = f"{PACKAGE_REPOSITORY}/issues"
PACKAGE_RELEASES = f"{PACKAGE_REPOSITORY}/releases"
PACKAGE_MARKETPLACE = (
    "https://github.com/marketplace/actions/blackout-secure-upstream-watcher"
)
PACKAGE_SUPPORT_EMAIL = "info@blackoutsecure.app"
PACKAGE_LICENSE = "Apache-2.0"
PACKAGE_COPYRIGHT = "Copyright © 2025-2026 Blackout Secure"

# Config keys that describe the package rather than repo policy. Stripped from
# every tier (marketplace, global, repo, inline) before the cascade is merged.
# `package_name` is deliberately absent: it is a watcher setting naming the
# npm/PyPI package to track, not this action's identity.
RESERVED_METADATA_KEYS = (
    "author",
    "author_email",
    "copyright",
    "documentation",
    "homepage",
    "issues",
    "license",
    "maintainer",
    "maintainer_email",
    "name",
    "package_author",
    "package_copyright",
    "package_description",
    "package_license",
    "package_repository",
    "package_version",
    "package_website",
    "releases",
    "repository",
    "support_email",
    "title",
    "version",
    "website",
)


def package_metadata(version: str) -> dict[str, str]:
    """Return action identity without loading any configuration."""
    return {
        "name": PACKAGE_NAME,
        "title": PACKAGE_TITLE,
        "version": version,
        "author": PACKAGE_AUTHOR,
        "author_email": PACKAGE_SUPPORT_EMAIL,
        "description": PACKAGE_DESCRIPTION,
        "website": PACKAGE_WEBSITE,
        "repository": PACKAGE_REPOSITORY,
        "documentation": PACKAGE_DOCUMENTATION,
        "issues": PACKAGE_ISSUES,
        "releases": PACKAGE_RELEASES,
        "marketplace": PACKAGE_MARKETPLACE,
        "support_email": PACKAGE_SUPPORT_EMAIL,
        "license": PACKAGE_LICENSE,
        "copyright": PACKAGE_COPYRIGHT,
    }


def strip_package_metadata(
    section: dict[str, Any],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Drop reserved identity keys from one config section.

    Only the top level of a section is stripped, so nested blocks keep their
    own descriptive keys.

    Returns:
        The cleaned section and the reserved keys that were ignored.
    """
    if not section:
        return section, ()
    ignored = tuple(key for key in section if key in RESERVED_METADATA_KEYS)
    if not ignored:
        return section, ()
    return {key: value for key, value in section.items() if key not in ignored}, ignored
