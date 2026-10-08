"""Release identity and the schema compatibility interlock (SHARED-0010).

Wax, HiveAR, and each module version independently, so compatibility is
tracked per component. For each one the database records the release whose
migrations were last applied, the oldest code release that schema still
supports, and the release now serving users. By rule the oldest supported
release is the previous minor, which is what lets the old color keep serving
during a cutover and lets an operator roll back.
"""

import os
import re
import time
from dataclasses import dataclass

from django.db import connection

RELEASE = os.environ.get("HIVEAR_RELEASE", "0.2.0")
COLOR = os.environ.get("DEPLOYMENT_COLOR", "blue")

_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z.]+)?$")


def previous_minor(version: str) -> str:
    """The first release of the minor line before this one. By the migration
    rule, that is the oldest code a release's schema must still support."""
    major, minor_number, _patch = (int(part) for part in _VERSION.match(version).groups())
    return f"{major}.{max(minor_number - 1, 0)}.0"


# A build may state a different floor, for example at a major version boundary.
MINIMUM_CODE_RELEASE = os.environ.get("HIVEAR_MINIMUM_CODE_RELEASE") or previous_minor(RELEASE)
_components = {}
_cache = {"at": 0.0, "problems": None}


class IncompatibleSchema(Exception):
    pass


@dataclass(frozen=True)
class Component:
    name: str
    release: str
    minimum_code_release: str
    apps: tuple


def parse(version: str) -> tuple[int, int, int]:
    """A release as numbers, so that 1.10.0 sorts after 1.9.0. Anything that
    is not MAJOR.MINOR.PATCH is an error, not a guess."""
    match = _VERSION.match(str(version))
    if not match:
        raise ValueError(f"{version!r} is not a MAJOR.MINOR.PATCH release")
    return tuple(int(part) for part in match.groups())


def minor(version: str) -> tuple[int, int]:
    return parse(version)[:2]


def register_component(name, release, minimum_code_release, apps):
    parse(release), parse(minimum_code_release)
    _components[name] = Component(name, release, minimum_code_release, tuple(apps))


def components() -> dict:
    return dict(_components)


def component_for_app(app_label):
    for component in _components.values():
        if app_label in component.apps:
            return component
    return None


def deployment_state() -> dict:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT active_color, candidate_color, previous_color, changed_at FROM core.deployment_state"
        )
        active, candidate, previous, changed_at = cursor.fetchone()
    return {
        "active_color": active,
        "candidate_color": candidate,
        "previous_color": previous,
        "changed_at": changed_at,
    }


def recorded_components() -> dict:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT component, schema_release, minimum_code_release, live_release "
            "FROM core.component_release ORDER BY component"
        )
        return {
            name: {"schema_release": schema, "minimum_code_release": minimum, "live_release": live}
            for name, schema, minimum, live in cursor.fetchall()
        }


def is_active_color() -> bool:
    """Singleton background work runs only on the live color."""
    return deployment_state()["active_color"] == COLOR


def incompatibilities() -> list[str]:
    """Why this code may not run against this database. Empty means it may."""
    problems = []
    for name, row in recorded_components().items():
        mine = _components.get(name)
        if mine is None:
            problems.append(f"{name} is installed in this database and missing from this image")
        elif parse(mine.release) < parse(row["minimum_code_release"]):
            problems.append(
                f"{name} here is {mine.release}; the schema is at {row['schema_release']} and "
                f"supports code from {row['minimum_code_release']} onward"
            )
    return problems


def assert_schema_supports_this_code(max_age_seconds=0):
    """Refuse to run against a database that has moved past this code. The
    request path passes a few seconds of tolerance so the check is one query
    every few seconds, not one per command."""
    now = time.monotonic()
    if _cache["problems"] is None or now - _cache["at"] >= max_age_seconds:
        _cache.update(at=now, problems=incompatibilities())
    if _cache["problems"]:
        raise IncompatibleSchema("; ".join(_cache["problems"]))
