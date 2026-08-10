"""Logging with redaction applied at the record, not at the call site.

CLAUDE.md: ``decky.logger`` reconfigures the root logger with ``force=True``, so any
handler we attach a filter to can be replaced underneath us. :func:`get_logger`
therefore attaches the filter to the *logger*, which survives handler churn, and
:func:`install_redaction` additionally covers root handlers so third-party libraries
(httpx, later) are scrubbed too.
"""

from __future__ import annotations

import logging

from runtime.secrets import redact

ROOT_LOGGER_NAME = "poedex"


class RedactingFilter(logging.Filter):
    """Substitute known secrets out of the message, arguments and traceback."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - broken %-format in a caller
            message = str(record.msg)
        record.msg = redact(message)
        record.args = ()

        if record.exc_info:
            if not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            record.exc_text = redact(record.exc_text)
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)

        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


_FILTER = RedactingFilter()


def get_logger(name: str) -> logging.Logger:
    """Return a redacting logger. ``name`` is namespaced under ``poedex``."""
    full = name if name == ROOT_LOGGER_NAME or name.startswith(ROOT_LOGGER_NAME + ".") \
        else f"{ROOT_LOGGER_NAME}.{name}"
    logger = logging.getLogger(full)
    if _FILTER not in logger.filters:
        logger.addFilter(_FILTER)
    return logger


def install_redaction(logger: logging.Logger | None = None) -> None:
    """Attach the redacting filter to every handler of ``logger`` (root by default).

    A filter on a logger only sees records logged through that logger; records that
    propagate up from children are filtered by the *handler*. Call this once at
    process start so nothing reaches a stream unredacted.
    """
    target = logger or logging.getLogger()
    if _FILTER not in target.filters:
        target.addFilter(_FILTER)
    handlers = list(target.handlers)
    # With no handlers configured, logging falls back to `lastResort`, which writes
    # WARNING and above to stderr. Cover it too, or the fallback path is the leak.
    if logging.lastResort is not None:
        handlers.append(logging.lastResort)
    for handler in handlers:
        if _FILTER not in handler.filters:
            handler.addFilter(_FILTER)


def silence_noisy_loggers() -> None:
    """Stop libraries that log URLs and headers from doing so at DEBUG (SPEC §8)."""
    for name in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(name).setLevel(logging.WARNING)
