"""Per-user notification routing (Vol 4 §7.1's channel matrix, Phase 64).

Private to the `messaging` package; `router.py` is the only importer.

§7.1 defines a category × channel matrix and says "per-category routing is
user-configurable", but only half of it existed: `notifications.category` has
been real data since migration 0003, while the CHANNEL axis had no storage
anywhere. This adds the missing half.

**Defaults are not rows.** A user who has never opened the Notifications screen
has no preference rows, and the matrix they see is :data:`DEFAULT_CHANNELS` —
the table straight out of §7.1. Materialising defaults at signup would freeze
them: changing a default later would then apply only to users created after the
change, which is exactly the bug this shape avoids. A row exists only where a
user has actually overridden something, and the read merges over defaults.
"""

import uuid

import structlog
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from titan.messaging.models import NotificationPreference

logger = structlog.get_logger("titan.messaging.preferences")

#: §7.1's five categories, in the order the spec's table lists them.
CATEGORIES: tuple[str, ...] = ("approval", "failure", "progress", "activity", "security")

#: The three in-app surfaces of §7.1 plus external delivery. `plugins` is the
#: Slack/Discord fan-out; it is listed because the matrix is the place a user
#: would look for it, and reported by the API as unavailable rather than
#: silently accepting a preference nothing acts on.
CHANNELS: tuple[str, ...] = ("toast", "bell", "attention_queue", "plugins")

#: §7.1's "Default channels" column, verbatim.
DEFAULT_CHANNELS: dict[str, frozenset[str]] = {
    "approval": frozenset({"toast", "bell", "attention_queue"}),
    "failure": frozenset({"toast", "bell"}),
    "progress": frozenset({"bell"}),
    # "Feed only (muted by default)" — no toast, no bell.
    "activity": frozenset(),
    "security": frozenset({"toast", "bell", "attention_queue"}),
}

#: §7.1's "Default severity" column.
DEFAULT_SEVERITY: dict[str, str] = {
    "approval": "critical",
    "failure": "high",
    "progress": "info",
    "activity": "low",
    "security": "critical",
}

#: Channels with no delivery implementation. Phase 64 has no plugin fan-out for
#: notification rows — there is no producer and no consumer — so a preference
#: naming it would be a promise nothing keeps. Reported, never silently honoured.
UNAVAILABLE_CHANNELS: frozenset[str] = frozenset({"plugins"})


class CategoryPreference(BaseModel):
    category: str
    severity: str
    channels: list[str] = Field(default_factory=list)
    #: True when this row is the §7.1 default rather than a user's choice.
    is_default: bool = True


class PreferencesOut(BaseModel):
    categories: list[CategoryPreference] = Field(default_factory=list)
    #: Every channel the matrix offers, and whether it actually delivers.
    available_channels: list[str] = Field(default_factory=list)
    unavailable_channels: list[str] = Field(default_factory=list)


class PreferencesUpdate(BaseModel):
    """Replace the whole matrix. Partial updates would make "unchecked" and
    "unmentioned" indistinguishable, and unchecking every channel for a
    category is a thing a user is entitled to mean."""

    model_config = ConfigDict(extra="forbid")

    categories: list["CategoryUpdate"] = Field(default_factory=list, max_length=32)


class CategoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str = Field(min_length=1, max_length=64)
    channels: list[str] = Field(default_factory=list, max_length=16)


PreferencesUpdate.model_rebuild()


def validate(patch: PreferencesUpdate) -> None:
    """Reject unknown categories and channels.

    Neither axis has a CHECK constraint in the database — `category` is
    free-form Text — so this is the only thing standing between a typo and a
    preference row that silently never matches anything.
    """
    unknown_categories = sorted({c.category for c in patch.categories} - set(CATEGORIES))
    if unknown_categories:
        raise ValueError(f"unknown categor(y/ies): {', '.join(unknown_categories)}")
    seen: set[str] = set()
    for entry in patch.categories:
        if entry.category in seen:
            raise ValueError(f"category '{entry.category}' appears more than once")
        seen.add(entry.category)
        unknown = sorted(set(entry.channels) - set(CHANNELS))
        if unknown:
            raise ValueError(f"unknown channel(s) for '{entry.category}': {', '.join(unknown)}")


def _describe(stored: dict[str, list[str]]) -> PreferencesOut:
    categories = [
        CategoryPreference(
            category=category,
            severity=DEFAULT_SEVERITY[category],
            channels=sorted(stored.get(category, sorted(DEFAULT_CHANNELS[category]))),
            is_default=category not in stored,
        )
        for category in CATEGORIES
    ]
    return PreferencesOut(
        categories=categories,
        available_channels=[c for c in CHANNELS if c not in UNAVAILABLE_CHANNELS],
        unavailable_channels=sorted(UNAVAILABLE_CHANNELS),
    )


async def get_preferences(conn: AsyncConnection, user_id: uuid.UUID) -> PreferencesOut:
    rows = await conn.execute(
        select(NotificationPreference.category, NotificationPreference.channels).where(
            NotificationPreference.user_id == user_id
        )
    )
    stored = {r.category: list(r.channels or []) for r in rows}
    return _describe(stored)


async def set_preferences(
    conn: AsyncConnection, user_id: uuid.UUID, patch: PreferencesUpdate
) -> PreferencesOut:
    """Upsert the supplied categories; a category set back to its §7.1 default
    drops its row so it tracks future changes to the default again."""
    validate(patch)
    for entry in patch.categories:
        channels = sorted(set(entry.channels))
        if frozenset(channels) == DEFAULT_CHANNELS[entry.category]:
            await conn.execute(
                delete(NotificationPreference).where(
                    NotificationPreference.user_id == user_id,
                    NotificationPreference.category == entry.category,
                )
            )
            continue
        statement = pg_insert(NotificationPreference).values(
            user_id=user_id, category=entry.category, channels=channels
        )
        await conn.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    NotificationPreference.user_id,
                    NotificationPreference.category,
                ],
                set_={"channels": channels},
            )
        )
    return await get_preferences(conn, user_id)


def channels_for(preferences: PreferencesOut, category: str) -> frozenset[str]:
    """The delivering channels for one category — the read a producer would do.

    Unavailable channels are filtered out here rather than at write time, so a
    preference an operator saved survives the arrival of the delivery mechanism
    that honours it instead of having been silently discarded.
    """
    for entry in preferences.categories:
        if entry.category == category:
            return frozenset(entry.channels) - UNAVAILABLE_CHANNELS
    return DEFAULT_CHANNELS.get(category, frozenset()) - UNAVAILABLE_CHANNELS


__all__: list[str] = [
    "CATEGORIES",
    "CHANNELS",
    "DEFAULT_CHANNELS",
    "DEFAULT_SEVERITY",
    "UNAVAILABLE_CHANNELS",
    "CategoryPreference",
    "CategoryUpdate",
    "PreferencesOut",
    "PreferencesUpdate",
    "channels_for",
    "get_preferences",
    "set_preferences",
    "validate",
]
