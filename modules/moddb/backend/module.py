"""The `moddb` core module: a trimmed Path of Exile mod database.

Core, and ``requires`` is empty. Game data is factual: which affixes exist, what each
rolls, where each can spawn, what tier a roll is on a given base. Judging whether a
T1 roll makes an item worth keeping is a feature opinion and stays in `appraisal`.

Nothing here fetches. The database is a committed build artifact —
``modules/moddb/data/moddb.json``, produced by ``scripts/build_moddb.py`` from
repoe-fork — because a Decky plugin installs from a zip with no pip and no business
downloading 30 MB of JSON on the user's connection. **Re-run that script every
league**; the module logs its age at start for exactly that reason, and
:meth:`ModDbModule.version` exposes it so a surface can, too.

Loading is lazy and cached. The artifact is half a megabyte of JSON and about 8 000
mod records; parsing it costs single-digit milliseconds, but a module that reads a
file in ``start()`` fails at startup when the file is missing, and a mod database
that is missing should degrade the features that use it rather than the whole app.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, ClassVar

from modules.moddb.backend.api import (
    Affix,
    ItemMods,
    ModDbApi,
    ModDbUnavailable,
    Origin,
)
from modules.moddb.backend.database import ModDatabase, load
from runtime.context import ModuleContext
from runtime.log import get_logger

__all__ = ["MODULE", "ModDbModule"]

_fallback_log = get_logger("module.moddb")

STALE_AFTER_DAYS = 120.0
"""Roughly one league. Past this the module says so at start.

Not an error and not a refusal — the data is still mostly right, and refusing to
answer would be worse than answering with a date attached. It is a line in the log
that makes "why does it say T3?" a five-second question instead of an afternoon."""


class ModDbModule:
    id = "moddb"
    name = "Mod database"
    kind = "core"
    requires: ClassVar[list[str]] = []
    provides: type | None = ModDbApi

    def __init__(self, *, path: Path | None = None) -> None:
        self._path = path
        self._db: ModDatabase | None = None
        self._ctx: ModuleContext | None = None
        self._failure: str | None = None

    # -- lifecycle -------------------------------------------------------------

    async def start(self, ctx: ModuleContext) -> None:
        self._ctx = ctx
        try:
            database = self._database()
        except ModDbUnavailable as exc:
            # Deliberately not fatal. A missing artifact disables tiers; it does not
            # stop the tool reading a bag, and the reason is recorded rather than
            # swallowed so the surface can say what is wrong.
            self._failure = str(exc)
            ctx.logger.error("mod database unavailable: %s", exc)
            return
        version = database.version()
        ctx.logger.info("moddb ready: %s", version.describe())
        if version.age_days() > STALE_AFTER_DAYS:
            ctx.logger.warning(
                "the mod database is %.0f days old (game version %s). A stale mod "
                "database does not fail, it answers confidently and wrongly — "
                "re-run scripts/build_moddb.py",
                version.age_days(),
                version.game_version,
            )

    async def stop(self) -> None:
        self._ctx = None

    def methods(self) -> dict[str, Callable[..., Any]]:
        return {
            "version": self.version_json,
            "base": self.base_json,
            "identify": self.identify_json,
            "report": self.report_json,
        }

    def settings_schema(self) -> dict[str, Any]:
        return {}

    # -- ModDbApi --------------------------------------------------------------

    def version(self) -> Any:
        return self._database().version()

    def base(self, base_type: str) -> Any:
        return self._database().base(base_type)

    def identify(
        self,
        text: str,
        *,
        base_type: str | None = None,
        ilvl: int = 0,
        origin: Origin = Origin.EXPLICIT,
        influences: Sequence[str] = (),
    ) -> Any:
        return self._database().identify(
            text, base_type=base_type, ilvl=ilvl, origin=origin, influences=influences
        )

    def report(self, item: ItemMods) -> Any:
        return self._database().report(item)

    def ceiling(
        self,
        text: str,
        *,
        base_type: str,
        ilvl: int = 0,
        influences: Sequence[str] = (),
    ) -> float | None:
        return self._database().ceiling(
            text, base_type=base_type, ilvl=ilvl, influences=influences
        )

    def trade_stat_id(self, text: str, *, origin: Origin = Origin.EXPLICIT) -> str | None:
        return self._database().trade_stat_id(text, origin=origin)

    def game_stat_ids(self, text: str) -> tuple[str, ...]:
        return self._database().game_stat_ids(text)

    def groups(self, base_type: str, *, affix: Affix | None = None) -> tuple[str, ...]:
        return self._database().groups(base_type, affix=affix)

    # -- JSON wrappers for the method registry ---------------------------------

    async def version_json(self) -> dict[str, Any]:
        return self.version().to_json()

    async def base_json(self, base_type: str) -> dict[str, Any] | None:
        found = self.base(base_type)
        return found.to_json() if found is not None else None

    async def identify_json(
        self,
        text: str,
        base_type: str | None = None,
        ilvl: int = 0,
        origin: str = Origin.EXPLICIT.value,
        influences: list[str] | None = None,
    ) -> dict[str, Any]:
        return self.identify(
            text,
            base_type=base_type,
            ilvl=ilvl,
            origin=Origin(origin),
            influences=influences or (),
        ).to_json()

    async def report_json(
        self,
        base_type: str,
        ilvl: int = 0,
        rarity: str = "rare",
        implicit: list[str] | None = None,
        explicit: list[str] | None = None,
        crafted: list[str] | None = None,
        fractured: list[str] | None = None,
        enchant: list[str] | None = None,
        influences: list[str] | None = None,
    ) -> dict[str, Any]:
        item = ItemMods(
            base_type=base_type,
            ilvl=ilvl,
            rarity=rarity,
            implicit=implicit or (),
            explicit=explicit or (),
            crafted=crafted or (),
            fractured=fractured or (),
            enchant=enchant or (),
            influences=influences or (),
        )
        return self.report(item).to_json()

    # -- internals -------------------------------------------------------------

    def _database(self) -> ModDatabase:
        if self._db is None:
            self._db = load(self._path)
            self._failure = None
        return self._db

    @property
    def failure(self) -> str | None:
        """Why the database is unusable, if it is. ``None`` when everything is fine."""
        return self._failure

    def __repr__(self) -> str:
        return f"ModDbModule(loaded={self._db is not None})"


MODULE = ModDbModule()
