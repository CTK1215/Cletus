"""The project registry: spoken names to folders on disk.

This is what lets Chris say "on the nurse app, run the tests" instead of
naming a path out loud. Every entry carries the aliases a person would
actually say, because "admin-nursetrack-app" is not a phrase anybody
pronounces.

Aliases are matched longest-first so "nursetrack admin" resolves to the
admin app rather than the mobile app that shares the first word. That
ordering is the whole reason ambiguity is survivable here, so keep the
specific phrases longer than the general ones.

Kept as data rather than scattered through the dispatcher so adding a
project is one entry and nothing else.
"""

from dataclasses import dataclass, field
from pathlib import Path

DEV = Path.home() / "dev"
VAULT = Path.home() / "Documents" / "Brain"


@dataclass(frozen=True)
class Project:
    key: str
    path: Path
    # What Cletus says back. Spoken aloud, so it has to sound like a thing a
    # person would call it, not a repo name.
    spoken: str
    aliases: tuple[str, ...] = field(default_factory=tuple)


PROJECTS: tuple[Project, ...] = (
    Project(
        key="nursetrack",
        path=DEV / "nursetrack",
        spoken="the NurseTrack mobile app",
        aliases=("nursetrack mobile", "the nurse app", "the mobile app", "nursetrack app", "nursetrack"),
    ),
    Project(
        key="admin",
        path=DEV / "admin-nursetrack-app",
        spoken="the NurseTrack admin",
        aliases=(
            "nursetrack admin", "the admin dashboard", "admin dashboard",
            "the dashboard", "the admin app", "the admin",
        ),
    ),
    Project(
        key="api",
        path=DEV / "ServeSync.API",
        spoken="the ServeSync API",
        aliases=("servesync api", "the servesync backend", "the backend", "the api", "servesync"),
    ),
    Project(
        key="unshackled",
        path=DEV / "unshackled-truth",
        spoken="Unshackled Truth",
        aliases=(
            "unshackled truth media", "unshackled truth", "the book site",
            "the ministry site", "unshackled",
        ),
    ),
    Project(
        key="kellybuilt",
        path=DEV / "kellybuilt",
        spoken="the Kellybuilt portfolio",
        aliases=("kelly built", "kellybuilt", "my portfolio", "the portfolio"),
    ),
    Project(
        key="senior",
        path=DEV / "pass-area-senior-placement",
        spoken="Pass Area Senior Placement",
        aliases=(
            "pass area senior placement", "senior placement",
            "the placement site", "the senior site",
        ),
    ),
    Project(
        key="wendell",
        path=DEV / "wendellturner-website",
        spoken="the Wendell Turner site",
        aliases=("wendell turner", "wendell's site", "wendells site", "wendell"),
    ),
    Project(
        key="servesync-app",
        path=DEV / "servesync-app",
        spoken="the ServeSync app",
        aliases=("servesync app", "the multi vertical app", "the multivertical app"),
    ),
    Project(
        key="cletus",
        path=DEV / "cletus",
        spoken="yourself",
        aliases=("cletus repo", "your own code", "yourself"),
    ),
    Project(
        key="vault",
        path=VAULT,
        spoken="the vault",
        aliases=("the vault", "my notes", "the brain", "obsidian"),
    ),
)


# Longest alias first. "nursetrack admin" has to beat "nursetrack", or every
# admin request lands in the mobile repo.
_ALIAS_INDEX: tuple[tuple[str, Project], ...] = tuple(
    sorted(
        ((alias.lower(), p) for p in PROJECTS for alias in p.aliases),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
)


def resolve(text: str) -> Project | None:
    """Find the project named in a spoken sentence, or None.

    Deliberately requires an explicit mention. Guessing which repo Chris
    meant and then editing files there is the worst possible failure mode
    for this feature, so silence is the correct answer when nothing matches.
    """
    low = f" {text.lower()} "
    for alias, project in _ALIAS_INDEX:
        if f" {alias} " in low or low.startswith(f" {alias}") or low.endswith(f"{alias} "):
            return project
    return None


def exists(project: Project) -> bool:
    return project.path.is_dir()


def spoken_list() -> str:
    """For the 'which project?' clarifying question, spoken aloud."""
    return ", ".join(p.spoken for p in PROJECTS if exists(p))
