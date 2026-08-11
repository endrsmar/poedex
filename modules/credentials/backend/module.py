"""The `credentials` core module.

Owns the one POESESSID and everything known about its health. It is core because it
holds no feature opinion: it stores a credential, reports whether the API has
accepted it, and tells anyone who asks. What to *do* about a rejected credential is
a surface decision, not this module's.

Deliberate omission: ``session_id`` is **not** in :meth:`methods`. The method
registry is what transports expose to the frontend, and the credential must not be
retrievable over an RPC boundary — it crosses into the CEF console, which is exactly
the leak SPEC §8 is about. In-process dependents get it through the typed API.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any, ClassVar

from modules.credentials.backend.api import (
    CREDENTIAL_CHANGED,
    PAIRING_CHANGED,
    CredentialsApi,
    CredentialState,
    CredentialStatus,
)
from modules.credentials.backend.pairing import (
    DEFAULT_PORT as DEFAULT_PAIRING_PORT,
)
from modules.credentials.backend.pairing import (
    DEFAULT_TIMEOUT_SECONDS,
    PairingServer,
    PairingStatus,
)
from modules.credentials.backend.store import (
    SessionRecord,
    SessionStore,
    normalize_session_id,
    utcnow,
)
from runtime.context import ModuleContext
from runtime.secrets import Secret

DEFAULT_MAX_AGE_DAYS = 14


class CredentialsModule:
    id = "credentials"
    name = "Credentials"
    kind = "core"
    requires: ClassVar[list[str]] = []
    provides: type | None = CredentialsApi

    def __init__(self, store: SessionStore | None = None) -> None:
        # An injectable store keeps the tests off the real ``~/.config``.
        self._store = store or SessionStore()
        self._ctx: ModuleContext | None = None
        self._pairing: PairingServer | None = None

    # -- lifecycle -------------------------------------------------------------

    async def start(self, ctx: ModuleContext) -> None:
        self._ctx = ctx
        record = self._store.load_quietly()
        state = self._state_of(record)
        ctx.logger.info(
            "credentials ready: state=%s account=%s",
            state.value,
            record.account if record else None,
        )

    async def stop(self) -> None:
        # Before anything else. A stop that leaves a credential-intake socket bound
        # is the worst thing this module could do on the way out, and `_unload` on
        # the Deck has five seconds before SIGKILL (SPEC §6.2).
        await self.pair_cancel()
        self._ctx = None

    def methods(self) -> dict[str, Callable[..., Any]]:
        return {
            "status": self.status_json,
            "set": self.set_json,
            "clear": self.clear_json,
            "mark_ok": self.mark_ok_json,
            "mark_rejected": self.mark_rejected_json,
            # SPEC §4.1. `pair_submit` is deliberately absent: the value arrives over
            # the pairing socket from the *other* machine, never over the RPC channel
            # a panel can reach, so there is no method a CEF console could call with a
            # credential in it and none that could read one back.
            "pair_start": self.pair_start,
            "pair_status": self.pair_status,
            "pair_cancel": self.pair_cancel,
        }

    def settings_schema(self) -> dict[str, Any]:
        return {
            "session_max_age_days": {
                "type": "int",
                "default": DEFAULT_MAX_AGE_DAYS,
                "min": 1,
                "max": 365,
                "label": "Warn after",
                "description": (
                    "Days since the API last accepted the session before it is "
                    "flagged as stale. Advisory only."
                ),
            },
            "pairing_port": {
                "type": "int",
                "default": DEFAULT_PAIRING_PORT,
                "min": 1024,
                "max": 65535,
                "label": "Pairing port",
                "description": (
                    "TCP port the pairing page listens on, and only while a pairing "
                    "window is open. It is bound on every interface so the PC on your "
                    "LAN can reach it, and closed the moment a credential arrives."
                ),
            },
            "pairing_timeout_seconds": {
                "type": "int",
                "default": int(DEFAULT_TIMEOUT_SECONDS),
                "min": 30,
                "max": 900,
                "label": "Pairing window",
                "description": (
                    "How long the pairing page stays reachable before it closes "
                    "itself. Shorter is safer; you need long enough to walk to the PC."
                ),
            },
        }

    # -- CredentialsApi --------------------------------------------------------

    async def status(self) -> CredentialStatus:
        return self._status_of(self._store.load_quietly())

    async def session_id(self) -> str | None:
        record = self._store.load_quietly()
        return record.session_id.reveal() if record else None

    async def set(self, value: str, account: str | None = None) -> CredentialStatus:
        cleaned = normalize_session_id(value)
        previous = self._store.load_quietly()
        record = SessionRecord(
            session_id=Secret(cleaned),
            account=account or (previous.account if previous else None),
            added_at=utcnow(),
            last_ok_at=None,
            rejected_at=None,
            note=None,
        )
        self._store.save(record)
        return await self._changed(record, "set")

    async def clear(self) -> CredentialStatus:
        self._store.clear()
        return await self._changed(None, "cleared")

    async def mark_ok(self, account: str | None = None) -> CredentialStatus:
        record = self._store.load_quietly()
        if record is None:
            return self._status_of(None)
        updated = SessionRecord(
            session_id=record.session_id,
            account=account or record.account,
            added_at=record.added_at,
            last_ok_at=utcnow(),
            rejected_at=None,
            note=None,
        )
        self._store.save(updated)
        return await self._changed(updated, "accepted")

    async def mark_rejected(self, note: str | None = None) -> CredentialStatus:
        record = self._store.load_quietly()
        if record is None:
            return self._status_of(None)
        updated = SessionRecord(
            session_id=record.session_id,
            account=record.account,
            added_at=record.added_at,
            last_ok_at=record.last_ok_at,
            rejected_at=utcnow(),
            note=note,
        )
        self._store.save(updated)
        return await self._changed(updated, "rejected")

    # -- JSON-serializable wrappers for the method registry --------------------
    #
    # Anything crossing to a frontend must be JSON (CLAUDE.md), and none of these
    # can return the credential because CredentialStatus does not carry it.

    async def status_json(self) -> dict[str, Any]:
        return (await self.status()).to_json()

    async def set_json(self, value: str, account: str | None = None) -> dict[str, Any]:
        return (await self.set(value, account)).to_json()

    async def clear_json(self) -> dict[str, Any]:
        return (await self.clear()).to_json()

    async def mark_ok_json(self, account: str | None = None) -> dict[str, Any]:
        return (await self.mark_ok(account)).to_json()

    async def mark_rejected_json(self, note: str | None = None) -> dict[str, Any]:
        return (await self.mark_rejected(note)).to_json()

    # -- LAN pairing (SPEC §4.1) -----------------------------------------------

    async def pair_start(self) -> dict[str, Any]:
        """Open a pairing window and return the code and URL to put on screen.

        Idempotent in the useful direction: calling it again mints a fresh code and
        restarts the clock, which is what a player who walked away and came back
        actually wants, and it means a stale code can never be paired with.
        """
        server = PairingServer(
            self._pair_store,
            port=self._setting("pairing_port", DEFAULT_PAIRING_PORT),
            timeout=float(self._setting("pairing_timeout_seconds", int(DEFAULT_TIMEOUT_SECONDS))),
            on_change=self._pairing_changed,
        )
        if self._pairing is not None:
            await self._pairing.close()
        self._pairing = server
        return (await server.open()).to_json()

    async def pair_status(self) -> dict[str, Any]:
        if self._pairing is None:
            return PairingStatus(state="idle").to_json()
        return self._pairing.status().to_json()

    async def pair_cancel(self) -> dict[str, Any]:
        if self._pairing is None:
            return PairingStatus(state="idle").to_json()
        status = await self._pairing.close()
        self._pairing = None
        return status.to_json()

    async def _pair_store(self, value: str) -> CredentialStatus:
        """What the pairing socket calls once a code checks out.

        Routed through :meth:`set` rather than touching the store directly, so the
        ``0600`` write, the redaction and the ``credential_changed`` event are the
        same ones ``poedex auth set`` gets. A second path to disk for a credential is
        a second path to get wrong.
        """
        return await self.set(value)

    async def _pairing_changed(self, status: PairingStatus) -> None:
        if self._ctx is not None:
            await self._ctx.events.emit(PAIRING_CHANGED, status.to_json(), source=self.id)

    def _setting(self, key: str, default: int) -> int:
        if self._ctx is None:
            return default
        return int(self._ctx.settings.get(key, default))

    # -- internals -------------------------------------------------------------

    async def _changed(self, record: SessionRecord | None, action: str) -> CredentialStatus:
        status = self._status_of(record)
        if self._ctx is not None:
            self._ctx.logger.info("credential %s: state=%s", action, status.state.value)
            await self._ctx.events.emit(
                CREDENTIAL_CHANGED, status.to_json(), source=self.id
            )
        return status

    @staticmethod
    def _state_of(record: SessionRecord | None) -> CredentialState:
        if record is None:
            return CredentialState.NEVER_SET
        if record.rejected_at is not None and (
            record.last_ok_at is None or record.rejected_at > record.last_ok_at
        ):
            return CredentialState.REJECTED
        if record.last_ok_at is not None:
            return CredentialState.OK
        return CredentialState.SET

    def _max_age(self) -> timedelta:
        days = DEFAULT_MAX_AGE_DAYS
        if self._ctx is not None:
            days = int(self._ctx.settings.get("session_max_age_days", DEFAULT_MAX_AGE_DAYS))
        return timedelta(days=days)

    def _status_of(self, record: SessionRecord | None) -> CredentialStatus:
        state = self._state_of(record)
        if record is None:
            return CredentialStatus(state=state)
        stale = (
            state is CredentialState.OK
            and record.last_ok_at is not None
            and utcnow() - record.last_ok_at > self._max_age()
        )
        return CredentialStatus(
            state=state,
            account=record.account,
            added_at=record.added_at,
            last_ok_at=record.last_ok_at,
            rejected_at=record.rejected_at,
            stale=stale,
            note=record.note,
        )

    def __repr__(self) -> str:
        return f"CredentialsModule(path={str(self._store.path)!r})"


# The registry discovers modules by importing this file and reading MODULE.
MODULE = CredentialsModule()
