"""Storage, settings, methods and the redacting logger."""

from __future__ import annotations

import logging
import stat
from pathlib import Path

import pytest

from runtime.errors import (
    MethodError,
    SettingsError,
    StorageError,
    UnknownMethodError,
    UnknownSettingError,
)
from runtime.log import RedactingFilter, get_logger, install_redaction
from runtime.methods import MethodRegistry
from runtime.secrets import REDACTED, Secret, redact, register_secret
from runtime.settings import SettingsStore
from runtime.storage import FILE_MODE, StorageRoot, cache_dir, config_dir

# -- storage -------------------------------------------------------------------


def test_namespaces_do_not_collide(tmp_path: Path):
    root = StorageRoot(tmp_path)
    a, b = root.namespace("prices"), root.namespace("appraisal")
    a.write_json("cache", {"who": "prices"})
    b.write_json("cache", {"who": "appraisal"})
    assert a.read_json("cache") == {"who": "prices"}
    assert b.read_json("cache") == {"who": "appraisal"}
    assert a.directory != b.directory


def test_round_trip_and_defaults(tmp_path: Path):
    store = StorageRoot(tmp_path).namespace("m")
    assert store.read_json("missing") is None
    assert store.read_json("missing", default=[]) == []
    store.write_json("items", [1, 2, 3])
    assert store.read_json("items") == [1, 2, 3]
    assert store.keys() == ["items"]
    assert store.delete("items") is True
    assert store.delete("items") is False


def test_files_are_owner_only(tmp_path: Path):
    store = StorageRoot(tmp_path).namespace("m")
    store.write_bytes("blob", b"x")
    assert stat.S_IMODE(store.path("blob").stat().st_mode) == FILE_MODE
    assert stat.S_IMODE(store.directory.stat().st_mode) == 0o700


@pytest.mark.parametrize("key", ["../escape", "sub/dir", "..", "", "with space"])
def test_keys_cannot_escape_the_namespace(tmp_path: Path, key: str):
    store = StorageRoot(tmp_path).namespace("m")
    with pytest.raises(StorageError):
        store.path(key)


def test_corrupt_json_is_reported_not_swallowed(tmp_path: Path):
    store = StorageRoot(tmp_path).namespace("m")
    store.write_bytes("bad", b"{not json")
    with pytest.raises(StorageError, match="not valid JSON"):
        store.read_json("bad")


def test_write_is_atomic_and_leaves_no_temp_files(tmp_path: Path):
    store = StorageRoot(tmp_path).namespace("m")
    store.write_json("a", {"v": 1})
    store.write_json("a", {"v": 2})
    assert store.read_json("a") == {"v": 2}
    assert [p.name for p in store.directory.iterdir()] == ["a"]


def test_env_overrides_are_honoured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("POEDEX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("POEDEX_CACHE_DIR", str(tmp_path / "cch"))
    assert config_dir() == tmp_path / "cfg"
    assert cache_dir() == tmp_path / "cch"


# -- settings ------------------------------------------------------------------


SCHEMA = {
    "league": {"type": "str", "default": "Standard"},
    "threshold": {"type": "int", "default": 20, "min": 0, "max": 1000},
    "mode": {"type": "str", "default": "median", "choices": ["median", "min"]},
}


def test_defaults_come_from_the_schema(tmp_path: Path):
    store = SettingsStore(tmp_path / "settings.json")
    view = store.register("prices", SCHEMA)
    assert view.all() == {"league": "Standard", "threshold": 20, "mode": "median"}


def test_values_persist_across_instances(tmp_path: Path):
    path = tmp_path / "settings.json"
    first = SettingsStore(path)
    first.register("prices", SCHEMA).set("threshold", 40)

    second = SettingsStore(path)
    assert second.register("prices", SCHEMA).get("threshold") == 40


def test_settings_file_is_owner_only(tmp_path: Path):
    path = tmp_path / "sub" / "settings.json"
    store = SettingsStore(path)
    store.register("prices", SCHEMA).set("threshold", 1)
    assert stat.S_IMODE(path.stat().st_mode) == FILE_MODE


@pytest.mark.parametrize(
    "key,value",
    [("threshold", "twenty"), ("threshold", 5000), ("mode", "average"), ("threshold", True)],
)
def test_invalid_values_are_refused(tmp_path: Path, key, value):
    view = SettingsStore(tmp_path / "s.json").register("prices", SCHEMA)
    with pytest.raises(SettingsError):
        view.set(key, value)


def test_unknown_keys_are_refused(tmp_path: Path):
    view = SettingsStore(tmp_path / "s.json").register("prices", SCHEMA)
    with pytest.raises(UnknownSettingError):
        view.set("colour", "blue")
    with pytest.raises(UnknownSettingError):
        view.get("colour")
    assert view.get("colour", "fallback") == "fallback"


@pytest.mark.parametrize(
    "schema",
    [
        {"x": {"type": "int"}},  # no default
        {"x": {"default": 1}},  # no type
        {"x": {"type": "complex", "default": 1}},  # unsupported type
        {"x": {"type": "int", "default": "no"}},  # default fails its own type
        {"x": {"type": "int", "default": 1, "colour": "red"}},  # unknown spec key
    ],
)
def test_bad_schemas_are_rejected(tmp_path: Path, schema):
    with pytest.raises(SettingsError):
        SettingsStore(tmp_path / "s.json").register("m", schema)


def test_removed_settings_do_not_survive_in_the_file(tmp_path: Path):
    path = tmp_path / "s.json"
    SettingsStore(path).register("m", {"gone": {"type": "int", "default": 1}}).set("gone", 5)
    store = SettingsStore(path)
    store.register("m", {"kept": {"type": "int", "default": 2}})
    store.save()
    assert "gone" not in path.read_text()


def test_view_is_a_mapping(tmp_path: Path):
    view = SettingsStore(tmp_path / "s.json").register("prices", SCHEMA)
    assert dict(view) == {"league": "Standard", "threshold": 20, "mode": "median"}
    with pytest.raises(KeyError):
        view["nope"]


# -- methods -------------------------------------------------------------------


async def test_methods_are_namespaced():
    registry = MethodRegistry()

    async def status():
        return "ok"

    assert registry.register("credentials", "status", status) == "credentials.status"
    assert registry.names() == ["credentials.status"]
    assert await registry.call("credentials.status") == "ok"


async def test_two_modules_may_share_a_method_name():
    registry = MethodRegistry()

    async def one():
        return 1

    async def two():
        return 2

    registry.register("a", "get", one)
    registry.register("b", "get", two)
    assert await registry.call("a.get") == 1
    assert await registry.call("b.get") == 2


def test_synchronous_methods_are_refused():
    with pytest.raises(MethodError, match="async def"):
        MethodRegistry().register("m", "sync", lambda: None)


def test_private_names_are_refused():
    async def hidden():
        return None

    with pytest.raises(MethodError, match="must not start"):
        MethodRegistry().register("m", "_hidden", hidden)


def test_duplicate_registration_is_refused():
    async def fn():
        return None

    registry = MethodRegistry()
    registry.register("m", "go", fn)
    with pytest.raises(MethodError, match="already registered"):
        registry.register("m", "go", fn)


def test_unknown_method_raises():
    with pytest.raises(UnknownMethodError):
        MethodRegistry().get("nope.gone")


def test_unregister_module_removes_only_its_methods():
    async def fn():
        return None

    registry = MethodRegistry()
    registry.register("a", "go", fn)
    registry.register("b", "go", fn)
    registry.unregister_module("a")
    assert registry.names() == ["b.go"]


# -- redaction -----------------------------------------------------------------


def test_secret_never_renders_itself():
    secret = Secret("0123456789abcdef0123456789abcdef")
    assert "0123456789abcdef" not in repr(secret)
    assert "0123456789abcdef" not in str(secret)
    assert "0123456789abcdef" not in f"{secret}"
    assert "0123456789abcdef" not in f"{secret!r}"
    assert secret.reveal() == "0123456789abcdef0123456789abcdef"


def test_secret_compares_without_leaking():
    assert Secret("0123456789abcdef0123456789abcdef") == "0123456789abcdef0123456789abcdef"
    assert Secret("0123456789abcdef0123456789abcdef") != "something else entirely"


def test_secret_refuses_to_pickle():
    import pickle

    with pytest.raises(TypeError):
        pickle.dumps(Secret("0123456789abcdef0123456789abcdef"))


def test_registered_secrets_are_redacted():
    register_secret("super-secret-value")
    assert redact("token=super-secret-value;") == f"token={REDACTED};"


def test_hex32_is_redacted_even_when_unregistered():
    assert redact("Cookie: POESESSID=deadbeefdeadbeefdeadbeefdeadbeef") == (
        f"Cookie: POESESSID={REDACTED}"
    )


def test_short_values_are_not_registered():
    register_secret("abc")
    assert redact("abc") == "abc"


def test_install_redaction_covers_the_last_resort_handler():
    """With no handlers configured, WARNING+ goes to stderr via `lastResort`."""
    install_redaction()
    assert any(isinstance(f, RedactingFilter) for f in logging.lastResort.filters)


def test_the_logger_redacts_messages_and_tracebacks(caplog: pytest.LogCaptureFixture):
    value = "cafebabecafebabecafebabecafebabe"
    register_secret(value)
    logger = get_logger("test.redaction")
    caplog.set_level(logging.DEBUG)
    caplog.handler.addFilter(RedactingFilter())

    logger.warning("about to use %s", value)
    try:
        raise ValueError(f"request failed with cookie {value}")
    except ValueError:
        logger.exception("boom")

    output = caplog.text + "".join(r.getMessage() for r in caplog.records)
    assert value not in output
    assert REDACTED in output
