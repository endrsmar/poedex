"""Secret handling.

Two mechanisms, deliberately independent:

* :class:`Secret` — a wrapper whose ``repr``/``str`` never contain the value, so a
  secret that ends up inside an f-string, a dataclass repr or an exception argument
  does not leak.
* A process-wide redaction registry — every known secret is substituted out of any
  text passed through :func:`redact`, which the logging filter in :mod:`runtime.log`
  applies to every record it sees.

The wrapper stops the mistakes we can foresee; the registry catches the ones we
cannot. SPEC §8: POESESSID is a full-account credential and must never reach a log
or an error message.
"""

from __future__ import annotations

import hmac
import re
import threading
from typing import Any

REDACTED = "***REDACTED***"

_lock = threading.Lock()
_secrets: set[str] = set()

# POESESSID is a 32-character hex string. Anything shaped like one is redacted even
# if it was never registered, which covers a value that reaches a log line before it
# has been stored.
_HEXLIKE = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])")

# Values too short to be worth substituting; redacting them would mangle unrelated text.
_MIN_REGISTERED_LENGTH = 8


def register_secret(value: str) -> None:
    """Add ``value`` to the set redacted from logs. Safe to call repeatedly."""
    if not value or len(value) < _MIN_REGISTERED_LENGTH:
        return
    with _lock:
        _secrets.add(value)


def forget_secret(value: str) -> None:
    with _lock:
        _secrets.discard(value)


def clear_secrets() -> None:
    """Drop every registered secret. Used by tests; harmless in production."""
    with _lock:
        _secrets.clear()


def redact(text: str) -> str:
    """Return ``text`` with every known secret and hex-shaped token replaced."""
    if not text:
        return text
    with _lock:
        known = sorted(_secrets, key=len, reverse=True)
    for secret in known:
        if secret in text:
            text = text.replace(secret, REDACTED)
    return _HEXLIKE.sub(REDACTED, text)


def redact_object(value: Any) -> Any:
    """Redact strings, recursing through the usual JSON-ish containers."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_object(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(redact_object(v) for v in value)
    return value


class Secret:
    """A string that refuses to render itself.

    ``str``, ``repr`` and ``format`` all yield a placeholder. The value is reachable
    only through :meth:`reveal`, which is easy to grep for in review.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str):
            raise TypeError("Secret requires a str")
        self._value = value
        register_secret(value)

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"Secret({REDACTED})"

    def __str__(self) -> str:
        return REDACTED

    def __format__(self, spec: str) -> str:
        return REDACTED

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Secret):
            return hmac.compare_digest(self._value, other._value)
        if isinstance(other, str):
            return hmac.compare_digest(self._value, other)
        return NotImplemented

    def __hash__(self) -> int:
        # Hashing the value would let a dict key leak it under comparison; hash the
        # class instead so Secrets are usable in sets without being distinguishable.
        return hash(Secret)

    def __bool__(self) -> bool:
        return bool(self._value)

    def __reduce__(self) -> Any:
        raise TypeError("Secret objects must not be pickled")
