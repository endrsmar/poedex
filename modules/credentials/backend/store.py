"""On-disk credential storage.

One file, ``~/.config/poedex/session.json``, mode ``0600`` inside a ``0700``
directory (SPEC §8). Writes go through :func:`runtime.storage.write_private_file`,
which creates the temp file with the final mode already set — the value is never
readable by another user, not even for the moment between ``open`` and ``chmod``.

Every error raised here is constructed from metadata only. There is no code path
that can put the credential into an exception message, because the value is only
ever held as a :class:`~runtime.secrets.Secret`, whose ``str`` is a placeholder.
"""

from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from modules.credentials.backend.api import CredentialError, InvalidCredentialError
from runtime.log import get_logger
from runtime.secrets import Secret, forget_secret
from runtime.storage import DIR_MODE, FILE_MODE, config_dir, ensure_private_dir, write_private_file

__all__ = [
    "CredentialError",
    "InvalidCredentialError",
    "SessionRecord",
    "SessionStore",
    "default_session_path",
    "normalize_session_id",
    "utcnow",
]

SESSION_FILENAME = "session.json"

# POESESSID is a 32-character hex string today. We accept a wider range rather than
# hard-failing on a format change, but reject obvious paste accidents (a whole
# cookie header, an empty string, a value with whitespace in it).
MIN_LENGTH = 16
MAX_LENGTH = 256

_log = get_logger("module.credentials.store")


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """The stored credential and its health metadata."""

    session_id: Secret
    account: str | None = None
    added_at: datetime | None = None
    last_ok_at: datetime | None = None
    rejected_at: datetime | None = None
    note: str | None = None

    def to_json(self) -> dict[str, Any]:
        """Serialize for disk. This is the *only* place the value is written out."""

        def iso(value: datetime | None) -> str | None:
            return value.isoformat() if value else None

        return {
            "poesessid": self.session_id.reveal(),
            "account": self.account,
            "added_at": iso(self.added_at),
            "last_ok_at": iso(self.last_ok_at),
            "rejected_at": iso(self.rejected_at),
            "note": self.note,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SessionRecord:
        raw = data.get("poesessid")
        if not isinstance(raw, str) or not raw:
            raise CredentialError("session file has no credential in it")

        def parse(key: str) -> datetime | None:
            value = data.get(key)
            if not isinstance(value, str) or not value:
                return None
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                _log.warning("session file has an unparseable %s; ignoring it", key)
                return None

        return cls(
            session_id=Secret(raw),
            account=data.get("account") or None,
            added_at=parse("added_at"),
            last_ok_at=parse("last_ok_at"),
            rejected_at=parse("rejected_at"),
            note=data.get("note") or None,
        )

    def __repr__(self) -> str:
        # Explicit, even though Secret already redacts itself: this object is the
        # single most likely thing to end up in a traceback.
        return (
            f"SessionRecord(session_id={self.session_id!r}, account={self.account!r}, "
            f"added_at={self.added_at!r})"
        )


def normalize_session_id(value: str) -> str:
    """Validate and clean a pasted credential. Never echoes it back in an error."""
    if not isinstance(value, str):
        raise InvalidCredentialError("credential must be a string")
    cleaned = value.strip()
    if cleaned.lower().startswith("poesessid="):
        cleaned = cleaned.split("=", 1)[1].strip().strip(";")
    if not cleaned:
        raise InvalidCredentialError("credential is empty")
    if any(ch.isspace() for ch in cleaned):
        raise InvalidCredentialError("credential contains whitespace; paste only the cookie value")
    if not (MIN_LENGTH <= len(cleaned) <= MAX_LENGTH):
        raise InvalidCredentialError(
            f"credential length {len(cleaned)} is outside the plausible range "
            f"{MIN_LENGTH}-{MAX_LENGTH}"
        )
    if len(cleaned) == 32 and not all(c in "0123456789abcdefABCDEF" for c in cleaned):
        _log.warning("credential is 32 characters but not hex; storing it anyway")
    return cleaned


def default_session_path() -> Path:
    return config_dir() / SESSION_FILENAME


class SessionStore:
    """Reads and writes the one credential file."""

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = Path(path).expanduser() if path is not None else None

    @property
    def path(self) -> Path:
        # Resolved on access, not in __init__: the module singleton is constructed at
        # import time, and POEDEX_CONFIG_DIR may be set after that (it is, in tests).
        return self._path if self._path is not None else default_session_path()

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> SessionRecord | None:
        """Return the stored record, or ``None`` if there is nothing stored."""
        if not self.path.is_file():
            return None
        self.enforce_permissions()
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CredentialError(f"session file is unreadable ({type(exc).__name__})") from None
        if not isinstance(data, dict):
            raise CredentialError("session file must contain a JSON object")
        return SessionRecord.from_json(data)

    def save(self, record: SessionRecord) -> None:
        ensure_private_dir(self.path.parent)
        payload = json.dumps(record.to_json(), indent=2, sort_keys=True).encode("utf-8")
        write_private_file(self.path, payload)
        self.enforce_permissions()

    def clear(self) -> bool:
        """Delete the credential file. Returns whether there was one."""
        existing = self.load_quietly()
        if existing is not None:
            forget_secret(existing.session_id.reveal())
        if not self.path.exists():
            return False
        self.path.unlink()
        return True

    def load_quietly(self) -> SessionRecord | None:
        """:meth:`load`, but a corrupt file yields ``None`` instead of raising."""
        try:
            return self.load()
        except CredentialError:
            return None

    def permissions(self) -> int | None:
        if not self.path.exists():
            return None
        return stat.S_IMODE(self.path.stat().st_mode)

    def enforce_permissions(self) -> None:
        """Tighten the file and its directory if anything loosened them.

        A credential that was correct at write time but is now world-readable is
        still a leak, and the likeliest cause — a user copying the config directory
        around — is not something we can prevent, only repair.
        """
        parent = self.path.parent
        try:
            if parent.exists() and stat.S_IMODE(parent.stat().st_mode) != DIR_MODE:
                _log.warning("tightening permissions on %s", parent)
                parent.chmod(DIR_MODE)
            if self.path.exists() and stat.S_IMODE(self.path.stat().st_mode) != FILE_MODE:
                _log.warning("tightening permissions on the session file")
                self.path.chmod(FILE_MODE)
        except OSError as exc:  # pragma: no cover - unusual filesystems
            _log.warning("could not tighten credential permissions: %s", type(exc).__name__)


def utcnow() -> datetime:
    return datetime.now(UTC)
