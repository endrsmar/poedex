"""The `poedex` CLI, with the credential never coming from argv."""

from __future__ import annotations

import getpass
import json
import stat
from pathlib import Path

import pytest

from cli import main as cli
from runtime.storage import config_dir

VALUE = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def typed(monkeypatch: pytest.MonkeyPatch):
    """Feed a value to the hidden prompt, recording that the prompt was used."""
    calls: list[str] = []

    def fake_getpass(prompt: str = "") -> str:
        calls.append(prompt)
        return typed.value

    monkeypatch.setattr(getpass, "getpass", fake_getpass)
    monkeypatch.setattr(cli.getpass, "getpass", fake_getpass)
    typed.value = VALUE
    typed.prompts = calls
    return typed


def session_file() -> Path:
    return config_dir() / "session.json"


# -- the credential never travels through argv ---------------------------------


def test_auth_set_takes_no_positional_argument(capsys: pytest.CaptureFixture):
    """argv is world-readable through /proc and lands in shell history."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["auth", "set", VALUE])
    assert excinfo.value.code == 2
    assert VALUE not in capsys.readouterr().out


def test_no_flag_anywhere_accepts_a_credential_value():
    parser = cli.build_parser()
    text = parser.format_help()
    for candidate in ("--poesessid", "--session", "--value", "--token"):
        assert candidate not in text


def test_auth_set_reads_from_the_hidden_prompt(typed, capsys: pytest.CaptureFixture):
    assert cli.main(["auth", "set"]) == 0
    assert typed.prompts, "getpass was not used"
    assert "hidden" in typed.prompts[0]

    stored = json.loads(session_file().read_text())
    assert stored["poesessid"] == VALUE
    assert VALUE not in capsys.readouterr().out


def test_stored_file_is_owner_only(typed):
    cli.main(["auth", "set"])
    assert stat.S_IMODE(session_file().stat().st_mode) == 0o600
    assert stat.S_IMODE(session_file().parent.stat().st_mode) == 0o700


def test_account_is_recorded(typed):
    cli.main(["auth", "set", "--account", "Exile"])
    assert json.loads(session_file().read_text())["account"] == "Exile"


def test_an_empty_prompt_leaves_the_credential_alone(typed, capsys: pytest.CaptureFixture):
    typed.value = ""
    assert cli.main(["auth", "set"]) == 1
    assert not session_file().exists()
    assert "nothing entered" in capsys.readouterr().err


def test_an_implausible_value_is_refused_without_echoing_it(
    typed, capsys: pytest.CaptureFixture
):
    typed.value = "too-short"
    assert cli.main(["auth", "set"]) == 1
    captured = capsys.readouterr()
    assert "refused" in captured.err
    assert "too-short" not in captured.err
    assert not session_file().exists()


def test_an_aborted_prompt_is_not_a_crash(monkeypatch: pytest.MonkeyPatch, capsys):
    def interrupted(prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr(cli.getpass, "getpass", interrupted)
    assert cli.main(["auth", "set"]) == 1
    assert "aborted" in capsys.readouterr().err


# -- status --------------------------------------------------------------------


def test_status_before_anything_is_stored(capsys: pytest.CaptureFixture):
    assert cli.main(["auth", "status"]) == 1
    out = capsys.readouterr().out
    assert "never_set" in out
    assert "poedex auth set" in out


def test_status_never_prints_the_credential(typed, capsys: pytest.CaptureFixture):
    cli.main(["auth", "set", "--account", "Exile"])
    capsys.readouterr()

    assert cli.main(["auth", "status"]) == 0
    out = capsys.readouterr().out
    assert VALUE not in out
    assert "state:      set" in out
    assert "Exile" in out


def test_clear_removes_the_credential(typed, capsys: pytest.CaptureFixture):
    cli.main(["auth", "set"])
    assert cli.main(["auth", "clear"]) == 0
    assert not session_file().exists()
    assert "never_set" in capsys.readouterr().out


# -- modules -------------------------------------------------------------------


def test_modules_lists_the_registry(capsys: pytest.CaptureFixture):
    assert cli.main(["modules"]) == 0
    out = capsys.readouterr().out
    assert "credentials" in out
    assert "core" in out
    assert "started" in out


# -- gamelog ---------------------------------------------------------------------


def test_gamelog_status_explains_a_log_it_cannot_read(capsys: pytest.CaptureFixture):
    """The autouse fixture points the watcher at a path that will never exist."""
    assert cli.main(["gamelog", "status"]) == 1
    out = capsys.readouterr().out
    assert "waiting" in out
    assert "no-such-Client.txt" in out
    assert "--path" in out, "an unusable log must come with the fix"


def test_gamelog_watch_prints_classified_zone_events(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    log = tmp_path / "Client.txt"
    log.write_text(
        '2018/05/13 16:10:08 1795218 d8  [INFO Client 1636] Generating level 83 area '
        '"MapWorldsGrotto" with seed 2049423767\n'
        "2018/05/13 16:10:14 1801062 9b0 [INFO Client 1636] : You have entered Grotto.\n"
        "2018/05/13 16:10:20 1801062 9b0 [INFO Client 1636] Spoofer: You have entered Hideout.\n",
        encoding="utf-8",
    )
    assert cli.main(["gamelog", "watch", "--path", str(log), "--from-start", "--seconds", "0"]) == 0
    out = capsys.readouterr().out
    assert "watching" in out
    assert "map      Grotto (level 83)" in out
    assert "hideout" not in out, "the chat line must not have been believed"


def test_gamelog_flags_do_not_persist_into_the_settings_file(tmp_path: Path):
    log = tmp_path / "Client.txt"
    log.write_text("", encoding="utf-8")
    cli.main(["gamelog", "watch", "--path", str(log), "--seconds", "0"])
    settings = config_dir() / "settings.json"
    assert not settings.exists() or str(log) not in settings.read_text()
