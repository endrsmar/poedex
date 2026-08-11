"""LAN pairing — how a 32-character credential gets onto a Deck with no keyboard.

SPEC §4.1. The QAM shows a **Pair** button; this binds ``0.0.0.0:<port>``, generates
a six-digit code, and the panel shows ``http://<deck-ip>:<port>`` and the code, both
large. The user opens that on the PC where the cookie already is, types the code,
pastes the POESESSID, and submits. The credential is written ``0600`` and **the
listener closes immediately**. Zero characters typed on the Deck.

## This is a full-account credential intake on a network, and is built like one

Every one of these is a constraint from SPEC §4.1/§8, not a nicety:

* **The socket exists only during a pairing window.** There is no long-lived
  listener; :meth:`PairingServer.open` binds and :meth:`close` unbinds, and the
  window closes on success, on cancel, on timeout, or after
  :data:`MAX_ATTEMPTS` wrong codes.
* **The code is single-use and compared in constant time.** ``secrets.randbelow``
  for the digits, ``hmac.compare_digest`` for the check. A six-digit code is 20 bits;
  what makes that enough is the attempt cap and the timeout, not the length.
* **Non-RFC1918 sources are refused before the body is read.** A request from a
  routable address is answered ``403`` and its bytes are never parsed.
* **The value is never logged, never echoed, and never re-served.** It goes straight
  into :class:`~runtime.secrets.Secret` via ``CredentialsApi.set``. The success page
  says "paired" and nothing else; there is no endpoint that reads a credential back.
* **No CORS headers, ever.** Their absence is what stops a page the user happens to
  have open from reading a response off this port.

## Two deliberate deviations, stated

1. **Loopback is allowed as well as RFC1918.** 127.0.0.0/8 is not RFC1918, so a
   literal reading of the spec would refuse ``curl localhost`` on the Deck itself and
   force every test to fake a peer address. A loopback peer is already executing code
   on the machine, so the rule it would enforce has already failed; allowing it costs
   nothing and makes the thing testable.
2. **``asyncio.start_server`` with a hand-written request reader, not ``http.server``
   and not FastAPI.** SPEC §4.1 records that Decky's stripped Python crashes the
   backend on some stdlib imports, and `fastapi`/`uvicorn` are deliberately outside
   the dependency set that has to be vendorable into ``py_modules/`` (see
   ``pyproject.toml``). The parser below handles exactly two routes and refuses
   everything else, which is the entire surface it needs.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import ipaddress
import secrets
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from modules.credentials.backend.api import CredentialError
from runtime.log import get_logger

__all__ = [
    "DEFAULT_PORT",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_ATTEMPTS",
    "MAX_BODY_BYTES",
    "PairingServer",
    "PairingState",
    "PairingStatus",
    "is_allowed_source",
    "local_addresses",
]

_log = get_logger("module.credentials.pairing")

DEFAULT_PORT = 7332
"""7331 is the web transport (IMPLEMENTATION-PLAN §6) and 1337 is Decky's own."""

DEFAULT_TIMEOUT_SECONDS = 180.0
"""Long enough to walk to the PC, short enough that a forgotten window is not a
listener. Three minutes was chosen against the task, not measured against users."""

MAX_ATTEMPTS = 3
"""Wrong codes before the window closes. 20 bits of code is only enough with a cap
on guesses, and 3 is the number every phone pairing flow has trained people on."""

MAX_BODY_BYTES = 4096
"""A POESESSID is 32 characters. Anything past this is not a paste accident."""

_ALLOWED_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        # RFC1918, which is what SPEC §4.1 names.
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        # Loopback — see deviation 1 in this module's docstring.
        "127.0.0.0/8",
        "::1/128",
        # RFC4193 unique-local, the IPv6 equivalent of RFC1918.
        "fc00::/7",
    )
)


def is_allowed_source(host: str) -> bool:
    """Is this peer address on a network we will accept a credential from?

    Deliberately an allow-list of literal networks rather than
    ``ipaddress.ip_address(host).is_private``: what ``is_private`` covers has changed
    between CPython releases (100.64.0.0/10 moved in 3.13), and "which addresses may
    hand us the account" is not a question whose answer should drift with the
    interpreter.
    """
    if not host:
        return False
    cleaned = host.strip("[]").split("%", 1)[0]
    try:
        address = ipaddress.ip_address(cleaned)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(address in network for network in _ALLOWED_NETWORKS)


def local_addresses() -> list[str]:
    """Every RFC1918 address of this machine, best effort.

    Used only to build the URL the panel shows. ``getaddrinfo`` on the hostname is
    what a Deck normally answers with; the UDP-connect trick is the fallback for a
    host whose name does not resolve to its LAN address, which is common enough on
    SteamOS to be worth the four lines. Neither sends a packet to the internet.
    """
    found: list[str] = []

    def add(candidate: str) -> None:
        # Loopback is *accepted* as a source (see the module docstring) but is never
        # shown: `http://127.0.0.1:7332` typed into a PC browser reaches the PC.
        if (
            candidate
            and is_allowed_source(candidate)
            and candidate not in found
            and not ipaddress.ip_address(candidate.strip("[]")).is_loopback
        ):
            found.append(candidate)

    with contextlib.suppress(OSError):
        for info in socket.getaddrinfo(socket.gethostname(), None):
            add(str(info[4][0]))

    if not found:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # Never actually sends: a connected UDP socket only picks a route, and
            # 10.255.255.255 is RFC1918 so nothing leaves the LAN even if it did.
            probe.connect(("10.255.255.255", 1))
            add(probe.getsockname()[0])
        except OSError:
            pass
        finally:
            probe.close()

    return found


class PairingState:
    """Where a pairing window is. A str-valued class rather than an enum so the
    values cross to the frontend as they are written here."""

    IDLE = "idle"
    WAITING = "waiting"
    PAIRED = "paired"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    REFUSED = "refused"
    """Too many wrong codes."""


@dataclass(frozen=True)
class PairingStatus:
    """What the panel is allowed to know. **Never carries the credential.**"""

    state: str
    code: str | None = None
    port: int | None = None
    urls: tuple[str, ...] = ()
    expires_in: float | None = None
    attempts_left: int | None = None
    detail: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "code": self.code,
            "port": self.port,
            "urls": list(self.urls),
            "expires_in": None if self.expires_in is None else round(self.expires_in, 1),
            "attempts_left": self.attempts_left,
            "detail": self.detail,
        }


class PairingServer:
    """One pairing window at a time.

    ``store`` is called with the pasted value and is expected to be
    ``CredentialsApi.set``; this class never touches the filesystem, so the ``0600``
    write and the redaction stay in the one place that already does them right.
    """

    def __init__(
        self,
        store: Callable[[str], Awaitable[Any]],
        *,
        port: int = DEFAULT_PORT,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        host: str = "0.0.0.0",
        on_change: Callable[[PairingStatus], Awaitable[None]] | None = None,
    ) -> None:
        self._store = store
        self._port = port
        self._timeout = timeout
        self._host = host
        self._on_change = on_change

        self._server: asyncio.AbstractServer | None = None
        self._code: str | None = None
        self._deadline: float | None = None
        self._attempts = 0
        self._state = PairingState.IDLE
        self._detail: str | None = None
        self._expiry_task: asyncio.Task[None] | None = None

    # -- lifecycle -------------------------------------------------------------

    async def open(self) -> PairingStatus:
        """Bind, mint a code, and start the clock. Re-opening cancels the old window."""
        await self.close(PairingState.CANCELLED, quiet=True)
        self._code = f"{secrets.randbelow(1_000_000):06d}"
        self._attempts = 0
        self._detail = None
        self._state = PairingState.WAITING
        try:
            self._server = await asyncio.start_server(self._handle, self._host, self._port)
        except OSError as exc:
            self._state = PairingState.IDLE
            self._code = None
            raise CredentialError(
                f"could not open the pairing port {self._port} ({type(exc).__name__}) — "
                f"something else may already be listening on it"
            ) from None
        self._deadline = time.monotonic() + self._timeout
        self._expiry_task = asyncio.get_running_loop().create_task(self._expire_after())
        # The code is not secret from the user and *is* on screen; the credential is
        # never logged at any level. Neither appears here.
        _log.info("pairing window open on port %s for %.0fs", self._port, self._timeout)
        return await self._publish()

    async def close(
        self, state: str = PairingState.CANCELLED, *, quiet: bool = False, drain: bool = True
    ) -> PairingStatus:
        """Unbind and forget the code. Idempotent.

        ``drain=False`` is for closing from *inside* a request handler, which is the
        normal case: a successful pair closes the window while it is still writing
        the "Paired." page. `AbstractServer.wait_closed()` waits for every handler
        task to finish (CPython 3.12.1+), so awaiting it from a handler waits for
        itself and the coroutine never returns. The same applies to
        :meth:`_expire_after`, which cancels a task it is running as.

        What matters for SPEC §4.1's *"closes the listener immediately"* is
        ``server.close()``, which stops accepting on the spot. Draining is only about
        connections already open.
        """
        current = asyncio.current_task()
        if self._expiry_task is not None:
            task, self._expiry_task = self._expiry_task, None
            if task is not current:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if self._server is not None:
            server, self._server = self._server, None
            server.close()
            if drain:
                with contextlib.suppress(Exception):
                    await server.wait_closed()
            if not quiet:
                _log.info("pairing window closed: %s", state)
        self._code = None
        self._deadline = None
        if self._state == PairingState.WAITING:
            self._state = state
        if quiet:
            return self.status()
        return await self._publish()

    @property
    def listening(self) -> bool:
        return self._server is not None

    @property
    def port(self) -> int:
        return self._port

    def status(self) -> PairingStatus:
        remaining = None if self._deadline is None else max(0.0, self._deadline - time.monotonic())
        return PairingStatus(
            state=self._state,
            code=self._code if self._state == PairingState.WAITING else None,
            port=self._port if self._state == PairingState.WAITING else None,
            urls=(
                tuple(f"http://{address}:{self._port}" for address in local_addresses())
                if self._state == PairingState.WAITING
                else ()
            ),
            expires_in=remaining,
            attempts_left=(
                max(0, MAX_ATTEMPTS - self._attempts)
                if self._state == PairingState.WAITING
                else None
            ),
            detail=self._detail,
        )

    async def _publish(self) -> PairingStatus:
        status = self.status()
        if self._on_change is not None:
            await self._on_change(status)
        return status

    async def _expire_after(self) -> None:
        """Close the window when the clock runs out.

        Deliberately a *loop* over short sleeps against ``time.monotonic()`` rather
        than one ``asyncio.sleep(timeout)``. Nothing in Decky Loader handles
        suspend/resume (research-notes §5), so a single long sleep across a lid close
        can overshoot by hours — and for this timer overshooting means the credential
        intake socket stays open while the Deck is in a bag. Waking often and
        comparing monotonic deltas makes a suspend *shorten* the window, never
        lengthen it.
        """
        while self._deadline is not None:
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(remaining, 1.0))
        self._detail = "the pairing window timed out"
        await self.close(PairingState.EXPIRED, drain=False)

    # -- the two routes --------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            peer = writer.get_extra_info("peername")
            host = str(peer[0]) if peer else ""
            if not is_allowed_source(host):
                # Refused before a byte of the request is parsed. The address is
                # logged because it is the one thing worth seeing here; nothing else
                # about the request is read, so nothing else can be.
                _log.warning("pairing refused a request from a non-private address")
                await _respond(writer, 403, "text/plain; charset=utf-8", b"forbidden\n")
                # Swallow whatever the client already put on the wire, *after* the
                # refusal and without looking at it. Closing a socket with unread
                # bytes in the receive queue sends an RST, and an RST throws the 403
                # away before the client can read it — so the refusal would arrive as
                # a connection error rather than as an answer. This is TCP hygiene,
                # not parsing: the bytes are read into nothing and the decision was
                # already made two lines above.
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(reader.read(MAX_BODY_BYTES), timeout=0.5)
                return

            request = await _read_request(reader)
            if request is None:
                await _respond(writer, 400, "text/plain; charset=utf-8", b"bad request\n")
                return
            method, path, body = request

            if method == "GET" and path in {"/", "/index.html"}:
                await _respond(writer, 200, "text/html; charset=utf-8", _form_page().encode())
                return
            if method == "POST" and path == "/pair":
                await self._pair(writer, body)
                return
            await _respond(writer, 404, "text/plain; charset=utf-8", b"not found\n")
        except (OSError, asyncio.IncompleteReadError):  # pragma: no cover - client hung up
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _pair(self, writer: asyncio.StreamWriter, body: bytes) -> None:
        fields = _parse_form(body)
        code = fields.get("code", "")
        value = fields.get("session", "")

        if self._code is None or self._state != PairingState.WAITING:
            await _respond(writer, 409, "text/html; charset=utf-8", _result_page(
                "This pairing window has closed.", "Press Pair on the Deck again."
            ).encode())
            return

        # Constant time, and the value is compared *first* so a wrong code and a
        # right one take the same path. `hmac.compare_digest` over the digits is
        # cheap insurance; the attempt cap is what actually bounds the guessing.
        if not hmac.compare_digest(code.strip(), self._code):
            self._attempts += 1
            left = max(0, MAX_ATTEMPTS - self._attempts)
            _log.warning("pairing code rejected; %d attempt(s) left", left)
            if left == 0:
                self._detail = "too many wrong codes"
                await self.close(PairingState.REFUSED, drain=False)
                await _respond(writer, 403, "text/html; charset=utf-8", _result_page(
                    "Too many wrong codes.", "The window is closed. Press Pair on the Deck again."
                ).encode())
                return
            await self._publish()
            await _respond(writer, 403, "text/html; charset=utf-8", _result_page(
                "That code is not right.",
                f"{left} attempt{'' if left == 1 else 's'} left, then the window closes.",
            ).encode())
            return

        try:
            await self._store(value)
        except CredentialError as exc:
            # Built from metadata only — `normalize_session_id` never puts the value
            # in a message, which is what makes echoing this back safe.
            self._attempts += 1
            _log.warning("pairing rejected a value: %s", type(exc).__name__)
            await _respond(writer, 400, "text/html; charset=utf-8", _result_page(
                "That does not look like a POESESSID.", str(exc)
            ).encode())
            return

        # Success. The listener goes away *before* the response is even flushed to
        # the client, because SPEC §4.1 says immediately and there is nothing left
        # for it to serve.
        self._state = PairingState.PAIRED
        self._detail = None
        await self.close(PairingState.PAIRED, drain=False)
        await _respond(writer, 200, "text/html; charset=utf-8", _result_page(
            "Paired.", "You can close this tab. The Deck has it."
        ).encode())


# -- the smallest HTTP that does the job ----------------------------------------


async def _read_request(reader: asyncio.StreamReader) -> tuple[str, str, bytes] | None:
    """Method, path and body, or ``None`` for anything we will not parse."""
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10.0)
    except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
        return None
    try:
        text = head.decode("latin-1")
    except UnicodeDecodeError:  # pragma: no cover - latin-1 decodes anything
        return None
    lines = text.split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) < 2:
        return None
    method, target = parts[0].upper(), parts[1]
    path = target.split("?", 1)[0]

    length = 0
    for line in lines[1:]:
        name, _, raw = line.partition(":")
        if name.strip().lower() == "content-length":
            try:
                length = int(raw.strip())
            except ValueError:
                return None
    if length < 0 or length > MAX_BODY_BYTES:
        return None
    body = b""
    if length:
        try:
            body = await asyncio.wait_for(reader.readexactly(length), timeout=10.0)
        except (TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            return None
    return method, path, body


def _parse_form(body: bytes) -> dict[str, str]:
    """``application/x-www-form-urlencoded``, by hand.

    ``urllib.parse.parse_qs`` would do this, but it keeps every value in a list that
    then has to be indexed, and a credential that lands in a list is a credential in
    one more place. This builds one dict of strings and nothing else holds a copy.
    """
    from urllib.parse import unquote_plus

    fields: dict[str, str] = {}
    for pair in body.decode("utf-8", "replace").split("&"):
        if not pair:
            continue
        name, _, raw = pair.partition("=")
        fields[unquote_plus(name)] = unquote_plus(raw)
    return fields


async def _respond(
    writer: asyncio.StreamWriter, status: int, content_type: str, body: bytes
) -> None:
    reason = {
        200: "OK",
        400: "Bad Request",
        403: "Forbidden",
        404: "Not Found",
        409: "Conflict",
    }.get(status, "OK")
    head = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        # No CORS headers, ever — their absence is what stops another origin reading
        # a response off this port. The rest is belt: nothing here should be cached,
        # embedded, or sent anywhere as a referrer.
        "Cache-Control: no-store\r\n"
        "Referrer-Policy: no-referrer\r\n"
        "X-Content-Type-Options: nosniff\r\n"
        "X-Frame-Options: DENY\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("latin-1")
    writer.write(head + body)
    with contextlib.suppress(ConnectionError):
        await writer.drain()


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


_STYLE = (
    "body{background:#171d25;color:#c7d5e0;font:16px/1.5 system-ui,sans-serif;"
    "margin:0;padding:2rem;display:flex;justify-content:center}"
    "main{max-width:32rem;width:100%}h1{font-size:1.3rem;margin:0 0 .25rem}"
    "p{color:#8b9aa8;margin:.25rem 0 1.25rem}label{display:block;margin:1rem 0 .25rem;"
    "font:600 .8rem/1 system-ui;letter-spacing:.08em;text-transform:uppercase;color:#667582}"
    "input{width:100%;box-sizing:border-box;padding:.7rem;font:16px/1 ui-monospace,monospace;"
    "background:#10161d;color:#e8eff5;border:1px solid #323d4a;border-radius:4px}"
    "button{margin-top:1.5rem;width:100%;padding:.8rem;font:600 1rem/1 system-ui;"
    "background:#58a0d8;color:#0e1a12;border:0;border-radius:4px}"
    "code{color:#c9a13f}"
)


def _form_page() -> str:
    """The paste target.

    A plain form POST, no JavaScript. This page runs on somebody else's machine in
    whatever browser they have open, and a credential intake is the wrong place to
    find out that a script did not run.
    """
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='referrer' content='no-referrer'>"
        "<title>Pair PoEDex</title><style>" + _STYLE + "</style></head><body><main>"
        "<h1>Pair PoEDex with your Deck</h1>"
        "<p>Open your browser's dev tools on <code>pathofexile.com</code>, find the "
        "<code>POESESSID</code> cookie, and paste its value here. It is sent to your "
        "Deck on your own network and stored there; it does not leave your house.</p>"
        "<form method='post' action='/pair' autocomplete='off'>"
        "<label for='code'>The six digits on the Deck</label>"
        "<input id='code' name='code' inputmode='numeric' pattern='[0-9]*' maxlength='6' "
        "autocomplete='off' required autofocus>"
        "<label for='session'>POESESSID</label>"
        "<input id='session' name='session' type='password' autocomplete='off' "
        "spellcheck='false' required>"
        "<button type='submit'>Send it to the Deck</button>"
        "</form></main></body></html>"
    )


def _result_page(headline: str, detail: str) -> str:
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='referrer' content='no-referrer'>"
        "<title>Pair PoEDex</title><style>" + _STYLE + "</style></head><body><main>"
        f"<h1>{_escape(headline)}</h1><p>{_escape(detail)}</p>"
        "</main></body></html>"
    )
