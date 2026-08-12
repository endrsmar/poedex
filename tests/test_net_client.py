"""The `net` client: refusal, redaction, the User-Agent, and the auth path.

Every request here goes through an ``httpx.MockTransport``. Nothing opens a socket;
there is no code path in this file that could reach pathofexile.com even if the
limiter allowed it.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from modules.credentials.backend.api import CredentialState
from modules.credentials.backend.module import CredentialsModule
from modules.credentials.backend.store import SessionStore
from modules.net.backend.api import (
    AuthRejected,
    HttpStatusError,
    NetApi,
    NetworkError,
    RateLimited,
)
from modules.net.backend.client import NetClient, build_user_agent, prepare_logging
from modules.net.backend.module import NetModule
from modules.net.backend.ratelimit import RateLimiter
from runtime.registry import Registry
from runtime.secrets import REDACTED, clear_secrets
from tests.conftest import (
    FIXTURES,
    FakeClock,
    FakeCredentials,
    headers,
)
from tests.conftest import (
    SESSION_VALUE as VALUE,
)


def make_client(handler, *, credentials=None, clock=None) -> NetClient:
    return NetClient(
        limiter=RateLimiter(clock=clock or FakeClock()),
        user_agent=build_user_agent("0.1.0", "someone@example.com"),
        credentials=credentials if credentials is not None else FakeCredentials(),
        transport=httpx.MockTransport(handler),
    )


def ok(payload=None, header_file="headers-items-authenticated.json"):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=payload if payload is not None else {"ok": True}, headers=headers(header_file)
        )

    return handler


# -- the User-Agent ------------------------------------------------------------


def test_user_agent_carries_contact_details():
    """SPEC §4.2: GGG requires them on every call."""
    agent = build_user_agent("0.1.0", "someone@example.com")
    assert agent.startswith("PoEDex/0.1.0")
    assert "github.com/endrsmar/poedex" in agent
    assert "contact: someone@example.com" in agent


def test_user_agent_still_identifies_the_project_without_an_email():
    agent = build_user_agent("0.1.0", "")
    assert "github.com/endrsmar/poedex" in agent
    assert "contact:" not in agent


async def test_the_user_agent_is_actually_sent():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers.get("user-agent")
        return httpx.Response(200, json={}, headers=headers("headers-items-authenticated.json"))

    client = make_client(handler)
    try:
        await client.get("/x")
    finally:
        await client.aclose()
    assert seen["ua"].startswith("PoEDex/")


# -- the credential ------------------------------------------------------------


async def test_the_credential_goes_in_the_cookie_header_and_nowhere_else():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["cookie"] = request.headers.get("cookie")
        seen["url"] = str(request.url)
        return httpx.Response(200, json={}, headers=headers("headers-items-authenticated.json"))

    client = make_client(handler)
    try:
        await client.get("/character-window/get-items", params={"character": "Someone"})
    finally:
        await client.aclose()
    assert seen["cookie"] == f"POESESSID={VALUE}"
    assert VALUE not in seen["url"]


async def test_no_credential_is_an_auth_error_not_a_request():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        calls.append(request)
        return httpx.Response(200, json={})

    client = make_client(handler, credentials=FakeCredentials(None))
    try:
        with pytest.raises(AuthRejected):
            await client.get("/x")
    finally:
        await client.aclose()
    assert calls == []


async def test_an_unauthenticated_request_sends_no_cookie():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["cookie"] = request.headers.get("cookie")
        return httpx.Response(200, json={}, headers=headers("headers-items-anonymous.json"))

    client = make_client(handler)
    try:
        await client.get("/x", authenticated=False)
    finally:
        await client.aclose()
    assert seen["cookie"] is None


async def test_the_credential_never_reaches_a_log_record(caplog: pytest.LogCaptureFixture):
    prepare_logging()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            text=f"internal error, cookie was POESESSID={VALUE}",
            headers=headers("headers-items-authenticated.json"),
        )

    client = make_client(handler)
    try:
        with caplog.at_level(logging.DEBUG), pytest.raises(HttpStatusError) as excinfo:
            await client.get("/x")
    finally:
        await client.aclose()
        clear_secrets()

    assert VALUE not in str(excinfo.value)
    assert REDACTED in str(excinfo.value)
    assert VALUE not in caplog.text


def test_httpx_logging_is_silenced_before_the_client_exists():
    """CLAUDE.md: httpx DEBUG records contain full request URLs."""
    logging.getLogger("httpx").setLevel(logging.DEBUG)
    logging.getLogger("httpcore").setLevel(logging.DEBUG)
    prepare_logging()
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


async def test_set_cookie_is_stripped_from_the_response_headers():
    def handler(request: httpx.Request) -> httpx.Response:
        response_headers = dict(headers("headers-items-authenticated.json"))
        response_headers["Set-Cookie"] = f"POESESSID={VALUE}; Path=/"
        return httpx.Response(200, json={}, headers=response_headers)

    client = make_client(handler)
    try:
        response = await client.get("/x")
    finally:
        await client.aclose()
    assert not any(key.lower() == "set-cookie" for key in response.headers)


# -- refusal -------------------------------------------------------------------


async def test_the_second_request_before_a_policy_is_learned_is_refused():
    """Seeding: one request buys the policy, the rest wait for it."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={}, headers={})  # no rate-limit headers at all

    client = make_client(handler)
    try:
        await client.get("/x")
        with pytest.raises(RateLimited) as excinfo:
            await client.get("/x")
    finally:
        await client.aclose()
    assert len(calls) == 1
    assert excinfo.value.retry_after > 0
    assert excinfo.value.server_rejected is False


async def test_a_refusal_never_sends_the_request():
    clock = FakeClock()
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={},
            headers={
                "X-Rate-Limit-Policy": "p",
                "X-Rate-Limit-Rules": "Account",
                "X-Rate-Limit-Account": "2:60",
                "X-Rate-Limit-Account-State": "1:60:0",
            },
        )

    client = make_client(handler, clock=clock)
    try:
        await client.get("/x")
        with pytest.raises(RateLimited):
            await client.get("/x")
    finally:
        await client.aclose()
    assert len(calls) == 1


async def test_a_server_429_is_reported_as_such():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={}, headers=headers("headers-items-restricted.json"))

    client = make_client(handler)
    try:
        with pytest.raises(RateLimited) as excinfo:
            await client.get("/x")
    finally:
        await client.aclose()
    assert excinfo.value.server_rejected is True
    assert excinfo.value.retry_after >= 60


async def test_refusal_carries_the_bucket_that_refused():
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={}, headers=headers("headers-items-authenticated.json"))

    client = make_client(handler, clock=clock)
    try:
        for _ in range(40):
            try:
                await client.get("/x")
            except RateLimited as exc:
                assert exc.policy == "backend-item-request-limit"
                assert exc.rule == "Account"
                assert exc.period == 60
                break
        else:  # pragma: no cover
            pytest.fail("the limiter never refused")
    finally:
        await client.aclose()


# -- status mapping ------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
async def test_401_and_403_are_auth_rejections(status: int):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "denied"})

    client = make_client(handler)
    try:
        with pytest.raises(AuthRejected):
            await client.get("/x")
    finally:
        await client.aclose()


async def test_a_login_redirect_is_an_auth_rejection():
    """An expired POESESSID 302s to the login page instead of returning 401."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://www.pathofexile.com/login"})

    client = make_client(handler)
    try:
        with pytest.raises(AuthRejected):
            await client.get("/x")
    finally:
        await client.aclose()


async def test_other_redirects_are_not_followed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/somewhere-else"})

    client = make_client(handler)
    try:
        with pytest.raises(HttpStatusError) as excinfo:
            await client.get("/x")
    finally:
        await client.aclose()
    assert excinfo.value.status == 302


async def test_a_transport_error_is_a_network_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    client = make_client(handler)
    try:
        with pytest.raises(NetworkError):
            await client.get("/x")
    finally:
        await client.aclose()


async def test_a_failed_request_still_counts_against_the_budget():
    """It may well have reached GGG's counter before the connection died."""
    clock = FakeClock()
    limiter = RateLimiter(clock=clock)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("gone")

    client = NetClient(
        limiter=limiter,
        user_agent="test",
        credentials=FakeCredentials(),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(NetworkError):
            await client.get("/x")
        assert limiter.seed_bucket("/x").count(clock.t) == 1
    finally:
        await client.aclose()


async def test_a_non_json_body_is_a_net_error_not_a_crash():
    from modules.net.backend.api import NetError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text="<html>maintenance</html>", headers={"content-type": "text/html"}
        )

    client = make_client(handler)
    try:
        with pytest.raises(NetError):
            await client.get_json("/x")
    finally:
        await client.aclose()


async def test_get_json_decodes_a_fixture():
    payload = json.loads((FIXTURES / "get-characters.json").read_text("utf-8"))
    client = make_client(ok(payload))
    try:
        decoded = await client.get_json("/character-window/get-characters")
    finally:
        await client.aclose()
    assert decoded[1]["name"] == "PlaceholderWarden"


# -- the module ----------------------------------------------------------------


@pytest.fixture
async def net_module(tmp_path, registry: Registry):
    """A started `net` wired to a mock transport and a real `credentials`."""
    credentials = CredentialsModule(store=SessionStore(tmp_path / "config" / "session.json"))
    client = make_client(ok(), credentials=FakeCredentials())
    module = NetModule(client=client)
    registry.register(credentials)
    registry.register(module)
    await registry.start_all()
    yield module
    await registry.stop_all()
    await client.aclose()


async def test_net_provides_its_protocol(net_module: NetModule, registry: Registry):
    assert isinstance(registry.api(NetApi), NetModule)


async def test_net_is_core_and_only_requires_credentials():
    assert NetModule.kind == "core"
    assert NetModule.requires == ["credentials"]


async def test_net_does_not_expose_get_over_the_method_registry(net_module: NetModule):
    """An arbitrary-URL fetch reachable from the CEF console is a credential proxy."""
    assert "get" not in net_module.methods()
    assert set(net_module.methods()) == {"limits"}


async def test_limits_json_is_serializable(net_module: NetModule):
    await net_module.get("/x")
    payload = await net_module.limits_json()
    json.dumps(payload)
    assert any(entry["policy"] == "backend-item-request-limit" for entry in payload)


async def test_credentials_state_is_untouched_by_net(net_module: NetModule, registry: Registry):
    """`net` reports; deciding what a 401 means belongs to `poeapi`."""
    from modules.credentials.backend.api import CredentialsApi

    credentials = registry.api(CredentialsApi)
    assert (await credentials.status()).state is CredentialState.NEVER_SET
