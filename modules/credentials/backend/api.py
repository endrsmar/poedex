"""The public surface of the `credentials` module.

**This is the only file in this module that other modules may import** (plan §1.4,
enforced by ``tests/test_boundaries.py``). Everything here is either a Protocol, a
plain data type, or an enum — no implementation, no state, no imports from the rest
of the module.

Nothing in this file ever exposes the credential except
:meth:`CredentialsApi.session_id`, which exists for exactly one caller: the `net`
core module, which needs the value to build a ``Cookie`` header. It returns a bare
``str`` rather than a wrapper because the header builder needs the characters; the
wrapper protects everything up to that point.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from runtime.errors import PoedexError

CREDENTIAL_CHANGED = "credential_changed"
"""Event topic emitted whenever the stored credential or its state changes."""

PAIRING_CHANGED = "pairing_changed"
"""Event topic for the LAN pairing window opening, closing or being refused.

Separate from :data:`CREDENTIAL_CHANGED` because a window that expired unpaired
changed nothing about the credential, and a panel redrawing its verdict list every
time a code timed out would be redrawing on a non-event. The payload is
``PairingStatus.to_json()``, which carries a code and a URL and — like everything
else crossing this boundary — never the credential."""


class CredentialError(PoedexError):
    """A credential problem. The message never contains the credential.

    Part of the public surface: a caller that stores a credential has to be able to
    catch a bad one, so the exception type belongs in ``api.py`` alongside the
    Protocol rather than in the implementation.
    """


class InvalidCredentialError(CredentialError):
    """The supplied value cannot be a POESESSID."""


class CredentialState(StrEnum):
    """How much we know about the stored credential.

    The distinction that matters to a caller is *never set* versus *set but never
    exercised* versus *known good* versus *rejected*: "please pair" and "your
    session expired, pair again" are different messages, and showing the wrong one
    is how a user concludes the tool is broken.
    """

    NEVER_SET = "never_set"
    """No credential on disk."""

    SET = "set"
    """Stored, but no request has been made with it yet. Validity unknown."""

    OK = "ok"
    """The API accepted it at ``last_ok_at``."""

    REJECTED = "rejected"
    """The API returned 401/403 at ``rejected_at``. Expired or revoked."""


@dataclass(frozen=True, slots=True)
class CredentialStatus:
    """Everything a caller may know about the credential, minus the credential.

    Safe to log, safe to serialize to the frontend, safe to put in an error.
    """

    state: CredentialState
    account: str | None = None
    added_at: datetime | None = None
    last_ok_at: datetime | None = None
    rejected_at: datetime | None = None
    stale: bool = False
    """``True`` when the credential is known-good but has not been confirmed within
    the module's ``session_max_age_days`` setting. Advisory, not authoritative —
    only the API can say whether a session is still alive."""

    note: str | None = None
    """Why it was rejected, when we know. Never contains the credential."""

    @property
    def usable(self) -> bool:
        """Whether there is a value worth attempting a request with."""
        return self.state in (CredentialState.SET, CredentialState.OK)

    def to_json(self) -> dict[str, Any]:
        def iso(value: datetime | None) -> str | None:
            return value.isoformat() if value else None

        return {
            "state": self.state.value,
            "account": self.account,
            "added_at": iso(self.added_at),
            "last_ok_at": iso(self.last_ok_at),
            "rejected_at": iso(self.rejected_at),
            "stale": self.stale,
            "note": self.note,
            "usable": self.usable,
        }


@runtime_checkable
class CredentialsApi(Protocol):
    """What dependents get from ``ctx.require(CredentialsApi)``."""

    async def status(self) -> CredentialStatus:
        """Current state. Never includes the credential value."""
        ...

    async def session_id(self) -> str | None:
        """The POESESSID value, or ``None`` if none is stored.

        Callers must not log it, store it, or put it in an exception message.
        """
        ...

    async def set(self, value: str, account: str | None = None) -> CredentialStatus:
        """Store a credential. ``value`` must never have come from argv."""
        ...

    async def clear(self) -> CredentialStatus:
        """Delete the stored credential."""
        ...

    async def mark_ok(self, account: str | None = None) -> CredentialStatus:
        """Record that the API accepted the credential just now."""
        ...

    async def mark_rejected(self, note: str | None = None) -> CredentialStatus:
        """Record that the API rejected the credential (401/403)."""
        ...
