"""LAN pairing (SPEC §4.1), against a real socket on loopback.

These are the checks that make the pairing window a credential intake rather than an
open port. Every one of them corresponds to a sentence in SPEC §4.1 or §8:

* single-use code, and a wrong one is refused;
* three wrong codes close the window rather than allowing a fourth;
* a short timeout, and it closes the socket;
* non-RFC1918 sources are refused, before the body is read;
* the value never reaches a log record, an event payload, or a method result.

**What these cannot check** is the flow end to end: a browser on a PC, on a real
LAN, reaching a Deck. Nobody here has one. `docs/deck-checklist.md` items 6-8 are
that, written so it takes ten minutes.

Nothing here touches the network: the server binds `127.0.0.1` and the client is
`asyncio.open_connection` to the same place.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket

import pytest

from modules.credentials.backend.api import CredentialError
from modules.credentials.backend.module import CredentialsModule
from modules.credentials.backend.pairing import (
    MAX_ATTEMPTS,
    MAX_BODY_BYTES,
    PairingServer,
    PairingState,
    is_allowed_source,
    local_addresses,
)
from modules.credentials.backend.store import SessionStore, normalize_session_id

VALUE = "0123456789abcdef0123456789abcdef"
OTHER_VALUE = "fedcba9876543210fedcba9876543210"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Recorder:
    """Stands in for ``CredentialsApi.set``, and validates like it does."""

    def __init__(self) -> None:
        self.stored: list[str] = []

    async def __call__(self, value: str) -> dict[str, str]:
        # `normalize_session_id` is what the real `set` runs first, and rejecting a
        # paste accident is part of what the pairing page has to do.
        self.stored.append(normalize_session_id(value))
        return {"state": "set"}


@pytest.fixture
async def server():
    made: list[PairingServer] = []

    def build(store, **kwargs) -> PairingServer:
        pair = PairingServer(store, port=free_port(), host="127.0.0.1", **kwargs)
        made.append(pair)
        return pair

    yield build
    for pair in made:
        await pair.close()


async def request(port: int, raw: bytes) -> tuple[int, str]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(raw)
    await writer.drain()
    payload = await reader.read()
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    text = payload.decode("utf-8", "replace")
    status = int(text.split(" ", 2)[1])
    return status, text


async def get(port: int, path: str = "/") -> tuple[int, str]:
    return await request(port, f"GET {path} HTTP/1.1\r\nHost: deck\r\n\r\n".encode())


async def post_pair(port: int, code: str, session: str) -> tuple[int, str]:
    body = f"code={code}&session={session}".encode()
    head = (
        f"POST /pair HTTP/1.1\r\nHost: deck\r\n"
        f"Content-Type: application/x-www-form-urlencoded\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    ).encode()
    return await request(port, head + body)


# -- the source allow-list ------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    ["10.0.0.5", "172.16.4.1", "172.31.255.254", "192.168.1.50", "127.0.0.1", "::1", "fd00::1"],
)
def test_private_and_loopback_sources_are_allowed(address: str):
    assert is_allowed_source(address)


@pytest.mark.parametrize(
    "address",
    [
        "8.8.8.8",
        "1.1.1.1",
        "172.32.0.1",  # just outside 172.16/12 — the classic off-by-one
        "172.15.255.255",
        "193.168.1.1",  # a typo for 192.168 that is a routable Spanish ISP
        "2001:4860:4860::8888",
        "169.254.10.1",  # link-local: not RFC1918, and not how a browser reaches us
        "",
        "not-an-address",
    ],
)
def test_public_sources_are_refused(address: str):
    assert not is_allowed_source(address)


def test_an_ipv4_mapped_public_address_does_not_sneak_through():
    """``::ffff:8.8.8.8`` is 8.8.8.8 wearing an IPv6 hat.

    A dual-stack listener reports peers in this form, so a check that only looked at
    the IPv6 networks would accept the whole internet.
    """
    assert not is_allowed_source("::ffff:8.8.8.8")
    assert is_allowed_source("::ffff:192.168.1.5")


def test_local_addresses_never_offers_loopback_as_the_pairing_url():
    # It may legitimately find nothing (a container with no private address), which
    # is why this is a property of whatever it finds rather than a count.
    assert all(not address.startswith("127.") for address in local_addresses())


# -- the happy path -------------------------------------------------------------


async def test_a_correct_code_stores_the_credential_and_closes_the_listener(server):
    recorder = Recorder()
    pair = server(recorder)
    status = await pair.open()
    assert status.state == PairingState.WAITING
    assert status.code is not None and len(status.code) == 6 and status.code.isdigit()

    code, port = status.code, pair.port
    http_status, body = await post_pair(port, code, VALUE)
    assert http_status == 200
    assert "Paired" in body
    assert recorder.stored == [VALUE]

    # SPEC §4.1: *closes the listener immediately*. Not on the next tick, not when the
    # window would have expired.
    assert not pair.listening
    assert pair.status().state == PairingState.PAIRED
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)


async def test_the_form_page_is_served_and_asks_for_both_things(server):
    pair = server(Recorder())
    await pair.open()
    status, body = await get(pair.port)
    assert status == 200
    assert "POESESSID" in body
    assert "name='code'" in body
    # No JavaScript. This page runs in whatever browser somebody has open, and a
    # credential intake is the wrong place to discover that a script did not.
    assert "<script" not in body.lower()


async def test_nothing_else_is_routed(server):
    pair = server(Recorder())
    await pair.open()
    assert (await get(pair.port, "/session.json"))[0] == 404
    assert (await get(pair.port, "/../../session.json"))[0] == 404


# -- refusals -------------------------------------------------------------------


async def test_a_wrong_code_is_refused_and_stores_nothing(server):
    recorder = Recorder()
    pair = server(recorder)
    status = await pair.open()
    wrong = "000000" if status.code != "000000" else "111111"

    http_status, body = await post_pair(pair.port, wrong, VALUE)
    assert http_status == 403
    assert recorder.stored == []
    # Still open — one typo does not cost the window.
    assert pair.listening
    assert "not right" in body
    assert pair.status().attempts_left == MAX_ATTEMPTS - 1


async def test_three_wrong_codes_close_the_window(server):
    recorder = Recorder()
    pair = server(recorder)
    status = await pair.open()
    wrong = "000000" if status.code != "000000" else "111111"

    for _ in range(MAX_ATTEMPTS):
        await post_pair(pair.port, wrong, VALUE)

    assert not pair.listening
    assert pair.status().state == PairingState.REFUSED
    assert recorder.stored == []


async def test_a_code_is_single_use(server):
    """The same code cannot pair twice, because the window is gone with the first."""
    recorder = Recorder()
    pair = server(recorder)
    status = await pair.open()
    code, port = status.code, pair.port
    assert (await post_pair(port, code, VALUE))[0] == 200
    with pytest.raises(OSError):
        await post_pair(port, code, VALUE)
    assert recorder.stored == [VALUE]


async def test_a_public_source_is_refused_before_the_body_is_read(server, monkeypatch):
    """A routable peer gets 403 and its request is never parsed.

    The peer address is forced rather than faked at the socket level: binding a
    routable address is not something a test may do, and the property under test is
    the decision, not the routing.
    """
    recorder = Recorder()
    pair = server(recorder)
    status = await pair.open()

    import modules.credentials.backend.pairing as pairing_module

    monkeypatch.setattr(pairing_module, "is_allowed_source", lambda host: False)
    http_status, body = await post_pair(pair.port, status.code or "", VALUE)
    assert http_status == 403
    assert "forbidden" in body
    assert recorder.stored == []


async def test_an_oversized_body_is_dropped(server):
    pair = server(Recorder())
    await pair.open()
    head = (
        f"POST /pair HTTP/1.1\r\nHost: deck\r\nContent-Length: {MAX_BODY_BYTES + 1}\r\n\r\n"
    ).encode()
    assert (await request(pair.port, head + b"x" * (MAX_BODY_BYTES + 1)))[0] == 400


async def test_a_value_that_is_not_a_poesessid_is_refused_without_echoing_it(server):
    recorder = Recorder()
    pair = server(recorder)
    status = await pair.open()
    http_status, body = await post_pair(pair.port, status.code or "", "short")
    assert http_status == 400
    assert recorder.stored == []
    assert "short" not in body
    # The window survives a paste accident; that is the whole reason to distinguish
    # a bad value from a bad code.
    assert pair.listening


# -- the window closes by itself ------------------------------------------------


async def test_the_window_times_out_and_unbinds(server):
    pair = server(Recorder(), timeout=0.05)
    await pair.open()
    port = pair.port
    for _ in range(200):
        if not pair.listening:
            break
        await asyncio.sleep(0.01)
    assert not pair.listening
    assert pair.status().state == PairingState.EXPIRED
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)


async def test_reopening_mints_a_new_code_and_retires_the_old_one(server):
    pair = server(Recorder())
    first = await pair.open()
    second = await pair.open()
    assert first.code != second.code
    assert pair.listening


async def test_close_is_idempotent(server):
    pair = server(Recorder())
    await pair.open()
    await pair.close()
    await pair.close()
    assert not pair.listening


async def test_open_reports_a_port_that_is_already_taken(server):
    blocker = socket.socket()
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    try:
        pair = PairingServer(Recorder(), port=port, host="127.0.0.1")
        with pytest.raises(CredentialError) as caught:
            await pair.open()
        assert str(port) in str(caught.value)
    finally:
        blocker.close()


# -- the credential never leaks -------------------------------------------------


async def test_the_credential_reaches_no_log_record(server, caplog):
    """SPEC §8: never log the value.

    Set at DEBUG on the root logger, which is what `decky.logger` reconfigures with
    ``force=True`` — so this is the level the Deck's plugin log would actually be
    running at if somebody turned it up.
    """
    recorder = Recorder()
    pair = server(recorder)
    with caplog.at_level(logging.DEBUG):
        status = await pair.open()
        await post_pair(pair.port, status.code or "", VALUE)
        wrong = "000000" if status.code != "000000" else "111111"
        await pair.open()
        await post_pair(pair.port, wrong, VALUE)

    assert recorder.stored == [VALUE]
    for record in caplog.records:
        assert VALUE not in record.getMessage()
        assert VALUE not in str(record.args or "")
    assert VALUE not in caplog.text


async def test_the_status_a_panel_sees_carries_no_credential(server):
    recorder = Recorder()
    pair = server(recorder)
    seen: list[dict] = []

    async def on_change(status):
        seen.append(status.to_json())

    pair = PairingServer(recorder, port=free_port(), host="127.0.0.1", on_change=on_change)
    try:
        status = await pair.open()
        await post_pair(pair.port, status.code or "", VALUE)
    finally:
        await pair.close()

    assert seen, "the panel is told the window opened"
    for payload in seen:
        assert VALUE not in repr(payload)
        assert set(payload) == {
            "state",
            "code",
            "port",
            "urls",
            "expires_in",
            "attempts_left",
            "detail",
        }
    # And the code is not left lying around once the window is gone.
    assert seen[-1]["code"] is None


# -- the account name lands with the credential ---------------------------------
#
# The Deck's whole problem in one place. The form has two fields, a code and a
# credential, and `get-items` needs a third thing the form cannot ask for. So the
# pair itself resolves it: `credentials` calls the resolver `poeapi` registered,
# which reads `/api/profile`. What these pin down is the part that must not regress —
# **a pair succeeds either way**, and says which way it went.


def paired_module(tmp_path, resolver=None) -> CredentialsModule:
    """A real `credentials` behind the pairing socket, unstarted.

    Unstarted on purpose: no event bus, no settings, nothing but the store and the
    resolver. Every guard in the module already treats a missing context as "nobody
    to tell", so this exercises the pairing path itself rather than the registry's.
    """
    module = CredentialsModule(store=SessionStore(tmp_path / "config" / "session.json"))
    module.set_account_resolver(resolver)
    return module


async def test_pairing_files_the_account_the_session_belongs_to(server, tmp_path):
    """No third field, and the credential still knows whose it is."""
    module = paired_module(tmp_path, resolver=_answers("PlaceholderExile#0000"))
    pair = server(module._pair_store)
    status = await pair.open()

    http_status, body = await post_pair(pair.port, status.code or "", VALUE)
    assert http_status == 200
    assert (await module.status()).account == "PlaceholderExile#0000"
    # The one confirmation worth giving somebody who has just pasted a cookie: not
    # "done", but *whose* it turned out to be.
    assert "PlaceholderExile#0000" in body
    assert pair.status().detail == "paired as PlaceholderExile#0000"


async def test_a_failed_lookup_still_leaves_a_usable_credential(server, tmp_path):
    """A session that works is a session that works.

    The name is a convenience the tool can retry for free on its next request;
    refusing the pair over it would send a player back to a form they filled in
    correctly, and back to the PC to do it again.
    """
    module = paired_module(tmp_path, resolver=_raises(RuntimeError("the network is a lie")))
    pair = server(module._pair_store)
    status = await pair.open()

    http_status, body = await post_pair(pair.port, status.code or "", VALUE)
    assert http_status == 200
    assert pair.status().state == PairingState.PAIRED
    stored = await module.status()
    assert stored.usable is True
    assert stored.account is None
    # ...and it says so, rather than reporting an unqualified success.
    assert "could not be read" in body
    assert "not known yet" in (pair.status().detail or "")


async def test_a_lookup_that_names_nobody_is_not_an_account(server, tmp_path):
    module = paired_module(tmp_path, resolver=_answers(None))
    pair = server(module._pair_store)
    status = await pair.open()
    await post_pair(pair.port, status.code or "", VALUE)
    assert (await module.status()).account is None
    assert (await module.status()).usable is True


async def test_a_new_credential_overwrites_an_account_carried_over_from_the_old_one(
    server, tmp_path
):
    """Re-pairing with a *different* account's cookie must not keep the old name.

    ``set`` carries the previous account forward, which is right when somebody is
    refreshing an expired cookie for the same account and is exactly wrong here. The
    lookup is what settles it, and it is why the resolver runs on every pair rather
    than only when the record happens to be blank.
    """
    module = paired_module(tmp_path, resolver=_answers("FirstExile#0001"))
    pair = server(module._pair_store)
    status = await pair.open()
    await post_pair(pair.port, status.code or "", VALUE)
    assert (await module.status()).account == "FirstExile#0001"

    module.set_account_resolver(_answers("SecondExile#0002"))
    pair = server(module._pair_store)
    status = await pair.open()
    await post_pair(pair.port, status.code or "", OTHER_VALUE)
    assert (await module.status()).account == "SecondExile#0002"


async def test_the_resolver_is_never_handed_the_credential(server, tmp_path):
    """It takes no arguments, and that is the point.

    Whatever resolves the account already holds the session through `credentials`;
    passing the value across one more boundary would be one more place it exists.
    """
    seen: list[tuple] = []

    async def resolver() -> str | None:
        seen.append(())
        return "PlaceholderExile#0000"

    module = paired_module(tmp_path, resolver=resolver)
    pair = server(module._pair_store)
    status = await pair.open()
    await post_pair(pair.port, status.code or "", VALUE)
    assert seen == [()]


async def test_a_failed_lookup_puts_nothing_of_the_credential_in_the_log(
    server, tmp_path, caplog
):
    """The failure is logged as a type name. Whatever the exception said, it came
    from a request made *with* the credential, and none of it is repeated."""
    module = paired_module(tmp_path, resolver=_raises(RuntimeError(f"failed for {VALUE}")))
    pair = server(module._pair_store)
    with caplog.at_level(logging.DEBUG):
        status = await pair.open()
        _, body = await post_pair(pair.port, status.code or "", VALUE)
    assert VALUE not in caplog.text
    assert VALUE not in body


def _answers(account: str | None):
    async def resolver() -> str | None:
        return account

    return resolver


def _raises(error: Exception):
    async def resolver() -> str | None:
        raise error

    return resolver
