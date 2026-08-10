"""``poedex moddb`` — the one surface that shows how old the database is.

Offline like every other test here: the command reads the committed artifact and
starts the real registry, which starts every module, none of which touches a socket
without being asked to.
"""

from __future__ import annotations

import pytest

from cli import main as cli


def test_it_leads_with_the_version_and_the_age(capsys: pytest.CaptureFixture) -> None:
    """The headline, not a footnote.

    A mod database one league stale answers confidently and wrongly, and nothing on
    screen looks unusual. The date is the only defence, so it goes first.
    """
    assert cli.main(["moddb"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("source:     repoe-fork, Path of Exile 3.")
    assert "built:" in out and "days ago" in out
    assert "affixes" in out and "bases" in out


def test_a_base_prints_the_facts_that_replace_the_gates_constants(
    capsys: pytest.CaptureFixture,
) -> None:
    assert cli.main(["moddb", "--base", "Hubris Circlet"]) == 0
    out = capsys.readouterr().out
    assert "Hubris Circlet (Helmet)" in out
    assert "top affix:  item level 85" in out
    assert "top-tier base: yes" in out


def test_an_unknown_base_fails_rather_than_printing_nothing(
    capsys: pytest.CaptureFixture,
) -> None:
    assert cli.main(["moddb", "--base", "Chaos Orb"]) == 1
    assert "not a base that rolls affixes" in capsys.readouterr().out


def test_a_mod_prints_its_tier_and_both_stat_ids(capsys: pytest.CaptureFixture) -> None:
    code = cli.main(
        ["moddb", "--base", "Eternal Burgonet", "--mod", "+95 to maximum Life", "--ilvl", "86"]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "verdict:    exact — T4 of 10" in out
    assert "group:      IncreasedLife (prefix)" in out
    assert "ceiling:    144 on this base" in out
    assert "trade id:   explicit.stat_3299347043" in out
    assert "game stats: base_maximum_life" in out


def test_an_unattributable_mod_exits_non_zero_and_shows_its_candidates(
    capsys: pytest.CaptureFixture,
) -> None:
    """"Ambiguous" is an answer, and the command reports it as one."""
    code = cli.main(
        [
            "moddb",
            "--base",
            "Onyx Amulet",
            "--mod",
            "20% increased Rarity of Items found",
            "--ilvl",
            "86",
        ]
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "verdict:    ambiguous — tier unknown" in out
    assert "candidates:" in out
    assert "groups render this text here" in out


def test_a_mod_without_a_base_is_refused(capsys: pytest.CaptureFixture) -> None:
    """A tier without a base type is not a fact, so it is not offered."""
    assert cli.main(["moddb", "--mod", "+95 to maximum Life"]) == 1
    assert "--mod needs --base" in capsys.readouterr().out


def test_an_influence_mod_needs_the_influence_flag(capsys: pytest.CaptureFixture) -> None:
    text = "14% increased Area of Effect"
    assert cli.main(["moddb", "--base", "Hubris Circlet", "--mod", text, "--ilvl", "86"]) == 1
    assert "unknown" in capsys.readouterr().out

    code = cli.main(
        [
            "moddb",
            "--base",
            "Hubris Circlet",
            "--mod",
            text,
            "--ilvl",
            "86",
            "--influence",
            "shaper",
        ]
    )
    assert code == 0
    assert "influence:  shaper" in capsys.readouterr().out


def test_it_is_listed_in_the_command_docstring() -> None:
    assert "poedex moddb" in (cli.__doc__ or "")
