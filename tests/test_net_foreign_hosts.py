"""`net` against a host that is not the PoE API.

Phase 3 needed poe.ninja and the trade API, and gave `net` three new obligations.
All three are safety properties rather than features, so they get their own file:

* the account credential must never leave the PoE API host;
* a foreign host must get rate-limit buckets of its own, so pricing cannot spend
  GGG's budget and a GGG restriction cannot stop pricing;
* a ``304`` must come back as a success, or conditional requests are pointless.
"""

from __future__ import annotations

import httpx
import pytest

from modules.net.backend.api import HttpStatusError, NetError, RateLimited
from modules.net.backend.client import NetClient, build_user_agent
from modules.net.backend.ratelimit import (
    DEFAULT_COURTESY_MAX_HITS,
    DEFAULT_COURTESY_PERIOD,
    RateLimiter,
)
from tests.conftest import FakeClock, FakeCredentials, headers

FOREIGN = "https://poe.ninja/poe1/api/economy/leagues"


class Recorder:
    """A transport that answers everything and remembers what it was asked."""

    def __init__(self, status: int = 200, response_headers: dict | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.status = status
        self.headers = response_headers or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status == 304:
            return httpx.Response(304, headers=self.headers)
        return httpx.Response(self.status, json={"ok": True}, headers=self.headers)


def make_client(recorder: Recorder, *, clock=None, **kwargs) -> NetClient:
    return NetClient(
        limiter=RateLimiter(clock=clock or FakeClock()),
        user_agent=build_user_agent("0.1.0", "someone@example.com"),
        credentials=FakeCredentials(),
        transport=httpx.MockTransport(recorder),
        **kwargs,
    )


# -- the credential --------------------------------------------------------------


async def test_the_credential_never_leaves_the_poe_api_host():
    """POESESSID is a full-account website credential (SPEC §8), not an API key."""
    recorder = Recorder()
    client = make_client(recorder)
    await client.get(FOREIGN, authenticated=False)
    assert "cookie" not in {k.lower() for k in recorder.requests[0].headers}
    await client.aclose()


async def test_authenticated_true_does_not_override_the_host_rule(caplog):
    """A caller with a bug must not be able to leak the credential by asking."""
    recorder = Recorder()
    client = make_client(recorder)
    with caplog.at_level("WARNING"):
        await client.get(FOREIGN, authenticated=True)
    assert "cookie" not in {k.lower() for k in recorder.requests[0].headers}
    # Not silent: the request went out, without the credential, and said so.
    assert any("poe.ninja" in record.getMessage() for record in caplog.records)
    await client.aclose()


async def test_the_credential_still_reaches_the_api_host():
    recorder = Recorder(response_headers=headers("headers-items-authenticated.json"))
    client = make_client(recorder)
    await client.get("/character-window/get-items")
    assert "cookie" in {k.lower() for k in recorder.requests[0].headers}
    await client.aclose()


async def test_a_relative_path_containing_a_url_is_still_relative():
    """The failure this guards is silent: a substring match on ``://`` would make an
    ordinary GGG path look foreign and drop the credential from it."""
    recorder = Recorder(response_headers=headers("headers-items-authenticated.json"))
    client = make_client(recorder)
    await client.get("/character-window/get-items", params={"next": "https://poe.ninja/x"})
    assert "cookie" in {k.lower() for k in recorder.requests[0].headers}
    await client.aclose()


async def test_an_absolute_url_to_the_api_host_is_not_foreign():
    recorder = Recorder(response_headers=headers("headers-items-authenticated.json"))
    client = make_client(recorder)
    await client.get("https://www.pathofexile.com/character-window/get-items")
    assert "cookie" in {k.lower() for k in recorder.requests[0].headers}
    await client.aclose()


# -- buckets ----------------------------------------------------------------------


async def test_a_foreign_host_gets_a_policy_of_its_own():
    recorder = Recorder()
    client = make_client(recorder)
    await client.get(FOREIGN, authenticated=False, route="poe.ninja")
    snapshots = client.limits()
    assert [s.policy for s in snapshots] == ["host:poe.ninja"]
    assert snapshots[0].max_hits == DEFAULT_COURTESY_MAX_HITS
    assert snapshots[0].period == DEFAULT_COURTESY_PERIOD
    # No margin: the number is ours, not a server's, so there is nothing to sit below.
    assert snapshots[0].effective_max == DEFAULT_COURTESY_MAX_HITS
    await client.aclose()


async def test_two_foreign_hosts_do_not_share_a_bucket():
    recorder = Recorder()
    client = make_client(recorder)
    await client.get("https://poe.ninja/a", authenticated=False, route="a")
    await client.get("https://example.invalid/b", authenticated=False, route="b")
    assert {s.policy for s in client.limits()} == {"host:poe.ninja", "host:example.invalid"}
    assert all(s.hits == 1 for s in client.limits())
    await client.aclose()


async def test_a_foreign_host_cannot_spend_the_ggg_budget():
    recorder = Recorder(response_headers=headers("headers-items-authenticated.json"))
    client = make_client(recorder)
    await client.get("/character-window/get-items", route="items")
    for index in range(5):
        await client.get(f"https://poe.ninja/{index}", authenticated=False, route="ninja")
    ggg = [s for s in client.limits() if s.policy == "backend-item-request-limit"]
    ninja = [s for s in client.limits() if s.policy == "host:poe.ninja"]
    assert ggg and all(s.hits <= 2 for s in ggg)
    assert ninja and ninja[0].hits == 5
    await client.aclose()


async def test_the_courtesy_budget_is_enforced():
    client = make_client(Recorder(), foreign_max_hits=2, foreign_period=60)
    await client.get("https://poe.ninja/1", authenticated=False, route="ninja")
    await client.get("https://poe.ninja/2", authenticated=False, route="ninja")
    with pytest.raises(RateLimited) as excinfo:
        await client.get("https://poe.ninja/3", authenticated=False, route="ninja")
    assert excinfo.value.retry_after > 0
    assert excinfo.value.server_rejected is False
    await client.aclose()


async def test_a_foreign_host_cannot_claim_a_ggg_policy():
    """The pin. A third-party host that sends ``X-Rate-Limit-Policy:
    backend-item-request-limit`` must not be merged into GGG's buckets — that would
    hand it a budget that is not its own and let it move our count of GGG's."""
    recorder = Recorder(response_headers=headers("headers-items-authenticated.json"))
    client = make_client(recorder)
    await client.get(FOREIGN, authenticated=False, route="ninja")
    policies = {s.policy for s in client.limits()}
    assert policies == {"host:poe.ninja"}


async def test_a_foreign_500_backs_off_only_that_host():
    recorder = Recorder(status=500)
    clock = FakeClock()
    client = make_client(recorder, clock=clock)
    with pytest.raises(HttpStatusError):
        await client.get(FOREIGN, authenticated=False, route="ninja")
    assert client.retry_after("ninja") > 0
    # The GGG route is untouched by somebody else's outage.
    assert client.retry_after("/character-window/get-items") == 0
    await client.aclose()


# -- conditional requests -----------------------------------------------------------


async def test_a_304_is_a_success_with_no_body():
    recorder = Recorder(status=304, response_headers={"etag": "W/abc"})
    client = make_client(recorder)
    response = await client.get(
        FOREIGN, authenticated=False, headers={"If-None-Match": "W/abc"}
    )
    assert response.not_modified
    assert response.status == 304
    assert response.content == b""
    assert response.etag == "W/abc"
    assert recorder.requests[0].headers["if-none-match"] == "W/abc"
    await client.aclose()


async def test_a_200_is_not_not_modified():
    recorder = Recorder(response_headers={"ETag": "W/xyz"})
    client = make_client(recorder)
    response = await client.get(FOREIGN, authenticated=False)
    assert not response.not_modified
    # Header lookup is case-insensitive, because servers are.
    assert response.etag == "W/xyz"
    await client.aclose()


async def test_a_response_without_an_etag_has_none():
    recorder = Recorder()
    client = make_client(recorder)
    assert (await client.get(FOREIGN, authenticated=False)).etag is None
    await client.aclose()


# -- POST -----------------------------------------------------------------------------


async def test_post_json_sends_a_body_and_decodes_the_answer():
    recorder = Recorder()
    client = make_client(recorder)
    result = await client.post_json(
        "/api/trade/search/Standard",
        json={"query": {"name": "Tabula Rasa"}},
        authenticated=False,
        route="trade:search",
    )
    assert result == {"ok": True}
    request = recorder.requests[0]
    assert request.method == "POST"
    assert b"Tabula Rasa" in request.content
    await client.aclose()


async def test_a_non_json_answer_is_an_error_not_a_crash():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>maintenance</html>",
                              headers={"content-type": "text/html"})

    client = make_client(handler)  # type: ignore[arg-type]
    with pytest.raises(NetError, match="expected JSON"):
        await client.get_json(FOREIGN, authenticated=False)
    await client.aclose()
