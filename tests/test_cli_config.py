"""``poedex config`` — the command that makes "set the X setting" followable.

Three messages told the user to set a setting and there was no command to set one:
``net``'s missing contact address, ``poeapi.account``, ``poeapi.league``. Settings
lived in a JSON file nobody had been shown, under keys nobody could discover. These
tests pin the four operations, the validation they inherit from the schema registry,
and the one thing that must stay impossible: **reaching the credential**.

The type round-trips run against a synthetic module rather than a real one, so that
all six supported types are covered and so that a product setting changing its
default cannot quietly stop testing ``config``. The credential tests run against the
real CLI, because that is the surface a leak would happen on.
"""

from __future__ import annotations

import getpass
import json
import stat
from pathlib import Path
from typing import Any

import pytest

from cli import main as cli
from cli.config import cmd_config, format_value, parse_value
from runtime.errors import SettingsError
from runtime.settings import SettingsStore
from runtime.storage import config_dir

VALUE = "0123456789abcdef0123456789abcdef"
"""A syntactically valid POESESSID that has never been a real one."""

SCHEMA: dict[str, Any] = {
    "text": {"type": "str", "default": "", "label": "Text", "description": "Some words."},
    "count": {"type": "int", "default": 3, "min": 1, "max": 10, "label": "Count"},
    "ratio": {"type": "float", "default": 1.5, "min": 0.0, "max": 10.0, "label": "Ratio"},
    "enabled": {"type": "bool", "default": True, "label": "Enabled"},
    "names": {"type": "list", "default": [], "label": "Names"},
    "mapping": {"type": "dict", "default": {}, "label": "Mapping"},
    "mode": {"type": "str", "default": "fast", "choices": ["fast", "slow"], "label": "Mode"},
}


@pytest.fixture
async def configured(registry, fake_module):
    """A started registry whose one module declares every supported setting type."""
    registry.register(fake_module("demo", schema=SCHEMA))
    await registry.start_all()
    yield registry
    await registry.stop_all()


async def run(registry, *argv: str) -> int:
    """``poedex config <argv>`` against a registry, through the real dispatch."""
    action, *rest = argv
    key = rest[0] if rest else None
    value = rest[1] if len(rest) > 1 else None
    return await cmd_config(
        registry, action=action, key=key, value=value, verbose="--verbose" in argv
    )


# -- round trip, one per type ---------------------------------------------------


@pytest.mark.parametrize(
    ("key", "typed", "stored"),
    [
        ("text", "you@example.com", "you@example.com"),
        ("text", "", ""),
        ("count", "7", 7),
        ("ratio", "2.5", 2.5),
        ("enabled", "false", False),
        ("enabled", "no", False),
        ("enabled", "1", True),
        ("names", '["Sadist Garb", "Vaal Regalia"]', ["Sadist Garb", "Vaal Regalia"]),
        ("mapping", '{"a": 1}', {"a": 1}),
    ],
)
async def test_a_value_round_trips_through_the_file(
    configured, tmp_path: Path, capsys, key: str, typed: str, stored: Any
):
    """argv has no types; the schema does. And it has to survive the process.

    Re-reading through a second store is the point: a value only "set" in memory is
    the failure the whole command exists to fix.
    """
    assert await run(configured, "set", f"demo.{key}", typed) == 0
    assert configured.settings.get("demo", key) == stored

    reopened = SettingsStore(tmp_path / "config" / "settings.json")
    reopened.register("demo", SCHEMA)
    assert reopened.get("demo", key) == stored

    capsys.readouterr()
    assert await run(configured, "get", f"demo.{key}") == 0
    assert format_value(stored) in capsys.readouterr().out


async def test_bool_rejects_anything_that_is_not_a_boolean(configured, capsys):
    assert await run(configured, "set", "demo.enabled", "maybe") == 1
    assert "not a boolean" in capsys.readouterr().err


async def test_int_rejects_a_float(configured, capsys):
    assert await run(configured, "set", "demo.count", "7.5") == 1
    assert "not a whole number" in capsys.readouterr().err


async def test_a_list_that_is_not_json_says_how_to_write_one(configured, capsys):
    assert await run(configured, "set", "demo.names", "Sadist Garb") == 1
    err = capsys.readouterr().err
    assert "not valid JSON" in err
    assert "written as JSON" in err


async def test_a_json_value_of_the_wrong_shape_is_refused(configured, capsys):
    """Valid JSON is not the same as the declared type."""
    assert await run(configured, "set", "demo.names", '{"a": 1}') == 1
    assert "expected list, got dict" in capsys.readouterr().err
    assert configured.settings.get("demo", "names") == []


# -- the schema's constraints are the CLI's constraints -------------------------


async def test_below_the_minimum_is_refused(configured, capsys):
    assert await run(configured, "set", "demo.count", "0") == 1
    assert "below minimum 1" in capsys.readouterr().err
    assert configured.settings.get("demo", "count") == 3


async def test_above_the_maximum_is_refused(configured, capsys):
    assert await run(configured, "set", "demo.ratio", "99") == 1
    assert "above maximum 10" in capsys.readouterr().err


async def test_a_value_outside_the_choices_is_refused_and_lists_them(configured, capsys):
    assert await run(configured, "set", "demo.mode", "medium") == 1
    err = capsys.readouterr().err
    assert "not one of" in err
    assert "slow" in err


async def test_the_bounds_themselves_are_accepted(configured):
    assert await run(configured, "set", "demo.count", "1") == 0
    assert await run(configured, "set", "demo.count", "10") == 0
    assert configured.settings.get("demo", "count") == 10


def test_the_real_schemas_are_enforced_too(capsys):
    """Not just the synthetic one: `poeapi` declares a floor and it is honoured."""
    assert cli.main(["config", "set", "poeapi.characters_ttl_seconds", "5"]) == 1
    assert "below minimum 60" in capsys.readouterr().err


# -- unknown keys explain themselves --------------------------------------------


async def test_an_unknown_key_suggests_the_near_miss(configured, capsys):
    assert await run(configured, "get", "demo.txet") == 1
    err = capsys.readouterr().err
    assert "no setting 'demo.txet'" in err
    assert "demo.text" in err
    assert "poedex config list" in err


async def test_an_unknown_module_lists_the_ones_that_exist(configured, capsys):
    assert await run(configured, "get", "nosuch.thing") == 1
    err = capsys.readouterr().err
    assert "no module 'nosuch'" in err
    assert "demo" in err


async def test_a_key_without_a_module_says_how_to_write_one(configured, capsys):
    assert await run(configured, "set", "text", "x") == 1
    assert "<module>.<key>" in capsys.readouterr().err


async def test_setting_an_unknown_key_writes_nothing(configured, tmp_path: Path):
    assert await run(configured, "set", "demo.nope", "x") == 1
    path = tmp_path / "config" / "settings.json"
    assert not path.exists() or "nope" not in path.read_text("utf-8")


# -- list, get, unset -----------------------------------------------------------


async def test_list_covers_every_registered_setting(configured, capsys):
    """Complete by construction: it walks the schema registry, not a hand-kept list."""
    assert await run(configured, "list") == 0
    out = capsys.readouterr().out
    for key in SCHEMA:
        assert key in out


async def test_list_says_whether_a_value_is_stored_or_the_default(configured, capsys):
    await run(configured, "set", "demo.count", "9")
    capsys.readouterr()
    assert await run(configured, "list") == 0
    lines = {line.split()[0]: line for line in capsys.readouterr().out.splitlines() if "  " in line}
    assert "default" in lines["ratio"]
    assert "set" in lines["count"] and "(default 3)" in lines["count"]


async def test_verbose_list_prints_the_descriptions(configured, capsys):
    assert await run(configured, "list", "--verbose") == 0
    assert "Some words." in capsys.readouterr().out


async def test_get_reports_the_source_and_the_constraints(configured, capsys):
    assert await run(configured, "get", "demo.count") == 0
    out = capsys.readouterr().out
    assert "the schema default" in out
    assert "minimum 1, maximum 10" in out

    await run(configured, "set", "demo.count", "9")
    capsys.readouterr()
    await run(configured, "get", "demo.count")
    assert "stored in" in capsys.readouterr().out


async def test_unset_returns_to_the_default(configured, capsys):
    await run(configured, "set", "demo.count", "9")
    capsys.readouterr()
    assert await run(configured, "unset", "demo.count") == 0
    assert configured.settings.get("demo", "count") == 3
    assert "back to the default" in capsys.readouterr().out


async def test_unset_of_a_setting_that_was_never_set_is_not_an_error(configured, capsys):
    assert await run(configured, "unset", "demo.count") == 0
    assert "already at its default" in capsys.readouterr().out


# -- the file stays owner-only --------------------------------------------------


def test_the_settings_file_is_owner_only(capsys):
    assert cli.main(["config", "set", "net.contact", "you@example.com"]) == 0
    path = config_dir() / "settings.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_a_value_set_through_the_cli_is_what_the_module_then_reads(capsys):
    assert cli.main(["config", "set", "net.contact", "you@example.com"]) == 0
    capsys.readouterr()
    assert cli.main(["config", "get", "net.contact"]) == 0
    assert "you@example.com" in capsys.readouterr().out


# -- no path to the credential --------------------------------------------------


@pytest.fixture
def stored_credential(monkeypatch: pytest.MonkeyPatch) -> str:
    """A real POESESSID, stored through the real command."""
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": VALUE)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": VALUE)
    assert cli.main(["auth", "set", "--account", "Exile"]) == 0
    assert VALUE in (config_dir() / "session.json").read_text("utf-8"), "fixture is not testing"
    return VALUE


def _keys_from_list(out: str) -> list[str]:
    """Every ``module.key`` ``config list`` printed, parsed off its own output.

    Deliberately read back from the command rather than from the registry: what
    matters is what a user can *reach*, and this is the reachable set.
    """
    keys: list[str] = []
    module: str | None = None
    for line in out.splitlines():
        if line and not line.startswith(" ") and line.endswith(")"):
            module = line.split()[0]
        elif module and line.startswith("  ") and not line.startswith("   "):
            token = line.strip().split()[0]
            if token.isidentifier():
                keys.append(f"{module}.{token}")
    return keys


def test_no_credential_value_is_reachable_through_any_config_command(
    stored_credential: str, capsys
):
    """The one thing this command must never do.

    Not "the credential is not listed" — *no config command reaches it*. The list is
    walked and every key it names is fetched, plus the names somebody would try by
    hand. The POESESSID is not in the settings store at all (`credentials` keeps it
    in its own file and registers one integer), so there is nothing to find; this is
    the test that says so, and that would fail the day a module registers one.
    """
    assert cli.main(["config", "list", "--verbose"]) == 0
    listed = capsys.readouterr()
    assert stored_credential not in listed.out + listed.err

    keys = _keys_from_list(listed.out)
    assert "net.contact" in keys and "credentials.session_max_age_days" in keys
    assert not any("session" in key and key.endswith(("id", "poesessid")) for key in keys)

    for key in keys:
        cli.main(["config", "get", key])
        captured = capsys.readouterr()
        assert stored_credential not in captured.out + captured.err, key

    for guess in ("credentials.poesessid", "credentials.session_id", "credentials.value"):
        assert cli.main(["config", "get", guess]) == 1
        captured = capsys.readouterr()
        assert stored_credential not in captured.out + captured.err


def test_a_hand_written_credential_key_is_not_resurrected_by_the_command(
    stored_credential: str, capsys
):
    """Someone edits the file and pastes the session id in. It stays unreachable.

    The store drops values whose key no schema declares, and `config` reads the
    schema registry rather than the file — so the key is neither listed, gettable,
    nor preserved by the next write.
    """
    path = config_dir() / "settings.json"
    path.write_text(json.dumps({"credentials": {"poesessid": stored_credential}}), "utf-8")

    assert cli.main(["config", "list"]) == 0
    captured = capsys.readouterr()
    assert stored_credential not in captured.out
    assert "poesessid" not in captured.out

    assert cli.main(["config", "get", "credentials.poesessid"]) == 1
    assert stored_credential not in capsys.readouterr().err

    assert cli.main(["config", "set", "net.contact", "you@example.com"]) == 0
    assert stored_credential not in path.read_text("utf-8")


def test_a_secret_shaped_value_in_an_ordinary_setting_is_still_redacted(capsys):
    """Belt and braces: a user pastes the session id into the wrong command.

    Every value this command prints goes through ``redact``, which substitutes out
    anything shaped like a POESESSID whether or not it was ever registered. The
    value is stored — `net.contact` is a free-text string and refusing it would be
    this command inventing a rule — but it is never echoed.
    """
    assert cli.main(["config", "set", "net.contact", VALUE]) == 0
    assert VALUE not in capsys.readouterr().out

    assert cli.main(["config", "get", "net.contact"]) == 0
    assert VALUE not in capsys.readouterr().out

    assert cli.main(["config", "list"]) == 0
    assert VALUE not in capsys.readouterr().out


# -- the messages that sent people here -----------------------------------------


def test_every_message_that_names_a_setting_names_the_command_too():
    """The pattern this fix ends: an instruction with no mechanism.

    ``net`` said "set it with the net.contact setting" and there was no way to set
    it. Anything that names a ``module.key`` at a user must also name the command,
    or it is the same dead end again.
    """
    from modules.net.backend import module as net_module
    from modules.poeapi.backend import module as poeapi_module

    sources = [
        Path(net_module.__file__).read_text("utf-8"),
        Path(poeapi_module.__file__).read_text("utf-8"),
    ]
    for name in ("net.contact", "poeapi.account", "poeapi.league", "poeapi.realm"):
        text = "".join(sources)
        assert f"poedex config set {name}" in text, name


def test_parse_value_and_format_value_are_inverses_for_the_awkward_ones():
    """``""``, ``0`` and ``false`` all print as nothing under a naive formatter."""
    for spec, value in (
        ({"type": "str"}, ""),
        ({"type": "int"}, 0),
        ({"type": "bool"}, False),
        ({"type": "list"}, []),
    ):
        printed = format_value(value)
        assert printed
        if spec["type"] != "str":
            assert parse_value("x.y", spec, printed) == value


def test_parse_value_refuses_rather_than_coercing():
    with pytest.raises(SettingsError):
        parse_value("x.y", {"type": "int"}, "twelve")
