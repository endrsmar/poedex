"""The `credentials` core module: storage, permissions, state, redaction."""

from __future__ import annotations

import json
import logging
import stat
from datetime import timedelta
from pathlib import Path

import pytest

from modules.credentials.backend.api import (
    CREDENTIAL_CHANGED,
    CredentialsApi,
    CredentialState,
    InvalidCredentialError,
)
from modules.credentials.backend.module import CredentialsModule
from modules.credentials.backend.store import (
    CredentialError,
    SessionRecord,
    SessionStore,
    default_session_path,
    normalize_session_id,
    utcnow,
)
from runtime.events import Event
from runtime.log import RedactingFilter
from runtime.registry import Registry
from runtime.secrets import REDACTED, Secret

VALUE = "0123456789abcdef0123456789abcdef"
OTHER = "fedcba9876543210fedcba9876543210"


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "config" / "session.json")


@pytest.fixture
async def module(store: SessionStore, registry: Registry):
    """A started `credentials` module wired to a throwaway store."""
    instance = CredentialsModule(store=store)
    registry.register(instance)
    await registry.start_all()
    yield instance
    await registry.stop_all()


# -- the store -----------------------------------------------------------------


def test_default_location_is_config_poedex_session_json(monkeypatch: pytest.MonkeyPatch):
    """SPEC §8 / plan §5: ``~/.config/poedex/session.json``."""
    monkeypatch.delenv("POEDEX_CONFIG_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert default_session_path() == Path("~/.config/poedex/session.json").expanduser()


def test_round_trip(store: SessionStore):
    now = utcnow()
    store.save(SessionRecord(session_id=Secret(VALUE), account="Exile", added_at=now))
    loaded = store.load()
    assert loaded is not None
    assert loaded.session_id.reveal() == VALUE
    assert loaded.account == "Exile"
    assert loaded.added_at == now


def test_nothing_stored_reads_as_none(store: SessionStore):
    assert store.load() is None
    assert store.exists() is False
    assert store.clear() is False


def test_file_is_0600_and_directory_is_0700(store: SessionStore):
    store.save(SessionRecord(session_id=Secret(VALUE), added_at=utcnow()))
    assert store.permissions() == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700


def test_loose_permissions_are_repaired_on_read(store: SessionStore):
    store.save(SessionRecord(session_id=Secret(VALUE), added_at=utcnow()))
    store.path.chmod(0o644)
    store.path.parent.chmod(0o755)

    store.load()

    assert store.permissions() == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700


def test_clear_removes_the_file(store: SessionStore):
    store.save(SessionRecord(session_id=Secret(VALUE), added_at=utcnow()))
    assert store.clear() is True
    assert not store.path.exists()
    assert store.load() is None


def test_a_corrupt_file_raises_a_metadata_only_error(store: SessionStore):
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{not json")
    with pytest.raises(CredentialError) as excinfo:
        store.load()
    assert "unreadable" in str(excinfo.value)
    assert store.load_quietly() is None


def test_a_file_without_a_credential_raises(store: SessionStore):
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps({"account": "Exile"}))
    with pytest.raises(CredentialError, match="no credential"):
        store.load()


def test_unparseable_timestamps_are_ignored_not_fatal(store: SessionStore):
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps({"poesessid": VALUE, "added_at": "not a date"}))
    record = store.load()
    assert record is not None and record.added_at is None


# -- input validation ----------------------------------------------------------


def test_a_pasted_cookie_pair_is_accepted():
    assert normalize_session_id(f"POESESSID={VALUE};") == VALUE
    assert normalize_session_id(f"  {VALUE}\n") == VALUE


@pytest.mark.parametrize("bad", ["", "   ", "short", f"{VALUE} extra", "a" * 300])
def test_implausible_values_are_refused(bad: str):
    with pytest.raises(InvalidCredentialError):
        normalize_session_id(bad)


def test_a_rejection_message_never_contains_the_value():
    bad = f"{VALUE} trailing"
    with pytest.raises(InvalidCredentialError) as excinfo:
        normalize_session_id(bad)
    assert VALUE not in str(excinfo.value)
    assert VALUE not in repr(excinfo.value)


# -- redaction -----------------------------------------------------------------


def test_the_record_repr_hides_the_value():
    record = SessionRecord(session_id=Secret(VALUE), account="Exile", added_at=utcnow())
    assert VALUE not in repr(record)
    assert REDACTED in repr(record)


def test_the_value_is_redacted_from_logs_once_loaded(
    store: SessionStore, caplog: pytest.LogCaptureFixture
):
    store.save(SessionRecord(session_id=Secret(VALUE), added_at=utcnow()))
    store.load()  # registers the value with the redactor

    caplog.set_level(logging.DEBUG)
    caplog.handler.addFilter(RedactingFilter())
    logging.getLogger("poedex.test").addFilter(RedactingFilter())
    logging.getLogger("poedex.test").error("leaking %s", VALUE)

    assert VALUE not in caplog.text


def test_only_the_session_file_ever_contains_the_value(store: SessionStore, tmp_path: Path):
    store.save(SessionRecord(session_id=Secret(VALUE), account="Exile", added_at=utcnow()))
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert files, "expected at least the session file"
    for path in files:
        content = path.read_text(errors="ignore")
        if path == store.path:
            assert VALUE in content
        else:
            assert VALUE not in content, f"{path} contains the credential"


# -- module state machine ------------------------------------------------------


async def test_state_is_never_set_before_anything_is_stored(module: CredentialsModule):
    status = await module.status()
    assert status.state is CredentialState.NEVER_SET
    assert status.usable is False
    assert status.account is None
    assert await module.session_id() is None


async def test_setting_a_credential_yields_state_set(module: CredentialsModule):
    status = await module.set(VALUE, account="Exile")
    assert status.state is CredentialState.SET
    assert status.usable is True
    assert status.account == "Exile"
    assert status.added_at is not None
    assert await module.session_id() == VALUE


async def test_mark_ok_moves_to_known_good(module: CredentialsModule):
    await module.set(VALUE)
    status = await module.mark_ok(account="Exile")
    assert status.state is CredentialState.OK
    assert status.last_ok_at is not None
    assert status.account == "Exile"


async def test_mark_rejected_moves_to_rejected(module: CredentialsModule):
    await module.set(VALUE)
    await module.mark_ok()
    status = await module.mark_rejected("401 from /character-window")
    assert status.state is CredentialState.REJECTED
    assert status.usable is False
    assert status.note == "401 from /character-window"


async def test_a_new_credential_clears_a_previous_rejection(module: CredentialsModule):
    await module.set(VALUE)
    await module.mark_rejected("expired")
    status = await module.set(OTHER)
    assert status.state is CredentialState.SET
    assert status.note is None
    assert await module.session_id() == OTHER


async def test_a_new_credential_keeps_the_known_account(module: CredentialsModule):
    await module.set(VALUE, account="Exile")
    status = await module.set(OTHER)
    assert status.account == "Exile"


async def test_clear_returns_to_never_set(module: CredentialsModule):
    await module.set(VALUE)
    status = await module.clear()
    assert status.state is CredentialState.NEVER_SET
    assert await module.session_id() is None


async def test_marking_without_a_credential_is_a_no_op(module: CredentialsModule):
    assert (await module.mark_ok()).state is CredentialState.NEVER_SET
    assert (await module.mark_rejected()).state is CredentialState.NEVER_SET


async def test_stale_flag_follows_the_setting(module: CredentialsModule, store: SessionStore):
    await module.set(VALUE)
    await module.mark_ok()
    assert (await module.status()).stale is False

    aged = store.load()
    assert aged is not None
    store.save(
        SessionRecord(
            session_id=aged.session_id,
            account=aged.account,
            added_at=aged.added_at,
            last_ok_at=utcnow() - timedelta(days=90),
        )
    )
    status = await module.status()
    assert status.state is CredentialState.OK
    assert status.stale is True

    module._ctx.settings.set("session_max_age_days", 365)
    assert (await module.status()).stale is False


# -- module wiring -------------------------------------------------------------


async def test_status_json_is_serializable_and_holds_no_secret(module: CredentialsModule):
    await module.set(VALUE, account="Exile")
    payload = await module.status_json()
    text = json.dumps(payload)
    assert VALUE not in text
    assert payload["state"] == "set"
    assert payload["account"] == "Exile"


async def test_session_id_is_not_exposed_over_the_method_registry(
    module: CredentialsModule, registry: Registry
):
    """The credential must not be retrievable across a transport boundary."""
    assert "session_id" not in module.methods()
    assert registry.methods.names() == [
        "credentials.clear",
        "credentials.mark_ok",
        "credentials.mark_rejected",
        "credentials.set",
        "credentials.status",
    ]


async def test_the_set_method_is_reachable_by_its_namespaced_name(
    module: CredentialsModule, registry: Registry
):
    payload = await registry.methods.call("credentials.set", VALUE, "Exile")
    assert payload["state"] == "set"
    assert payload["account"] == "Exile"


async def test_changes_emit_credential_changed(module: CredentialsModule, registry: Registry):
    seen: list[Event] = []
    registry.events.subscribe(CREDENTIAL_CHANGED, seen.append)

    await module.set(VALUE)
    await module.mark_ok()
    await module.clear()

    assert [e.payload["state"] for e in seen] == ["set", "ok", "never_set"]
    assert all(e.source == "credentials" for e in seen)
    assert all(VALUE not in json.dumps(e.payload) for e in seen)


async def test_the_module_satisfies_its_declared_api(module: CredentialsModule):
    assert isinstance(module, CredentialsApi)
    assert module.provides is CredentialsApi
    assert module.kind == "core"
    assert module.requires == []


async def test_a_dependent_module_resolves_the_typed_api(registry: Registry, fake_module, store):
    """Phase 1 exit criterion, end to end: a second module gets `CredentialsApi`."""
    registry.register(CredentialsModule(store=store))
    dependent = fake_module("consumer", requires=["credentials"], wants=[CredentialsApi])
    registry.register(dependent)

    await registry.start_all()

    api = dependent.resolved[CredentialsApi]
    assert isinstance(api, CredentialsApi)
    await api.set(VALUE, "Exile")
    assert (await api.status()).account == "Exile"
    assert await api.session_id() == VALUE

    await registry.stop_all()


async def test_settings_schema_is_registered_with_the_store(
    module: CredentialsModule, registry: Registry
):
    schema = registry.settings.schema("credentials")
    assert "session_max_age_days" in schema
    assert registry.settings.get("credentials", "session_max_age_days") == 14
