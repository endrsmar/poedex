"""Line parsing, classification and the two-line correlation.

The three tests that matter most are :func:`test_a_chat_line_is_not_a_zone_event`,
:func:`test_set_tokens_are_stripped` and the classification block — they are the
three gotchas from mapwatch's regression suite (research §4), and each one is a bug
that would look like a working tool right up until it did the wrong thing.
"""

from __future__ import annotations

import re

import pytest

from modules.gamelog.backend.api import ZoneKind, ZoneSource
from modules.gamelog.backend.parse import (
    ZoneTracker,
    classify,
    compile_patterns,
    parse_line,
    strip_set_tokens,
)

from .conftest import FakeClock, chat, entered, generating

# -- line structure ----------------------------------------------------------------


def test_a_system_line_is_parsed():
    line = parse_line(
        "2018/05/13 16:10:14 1801062 9b0 [INFO Client 1636] : You have entered The Twilight Strand."
    )
    assert line is not None
    assert line.log_time == "2018/05/13 16:10:14"
    assert line.tag == "INFO Client 1636"
    assert line.body == "You have entered The Twilight Strand."
    assert line.is_system


def test_the_padded_thread_column_is_tolerated():
    line = parse_line(
        '2018/05/13 16:10:08 1795218 d8  [INFO Client 1636] Generating level 83 area '
        '"MapWorldsGrotto" with seed 2049423767'
    )
    assert line is not None
    assert not line.is_system
    assert line.body.startswith("Generating level 83")


@pytest.mark.parametrize(
    "line",
    [
        "",
        "not a log line at all",
        "2018/05/13 16:10:14 [INFO Client] : You have entered Somewhere.",
        "****** Path of Exile client startup ******",
    ],
)
def test_junk_is_not_a_log_line(line: str):
    assert parse_line(line) is None


def test_a_crlf_line_ending_is_stripped():
    line = parse_line(entered("Lioneye's Watch") + "\r\n")
    assert line is not None and line.body.endswith(".")


# -- gotcha 1: the `] : ` anchor ----------------------------------------------------


def test_a_chat_line_is_not_a_zone_event():
    """A player in your instance can type this. Substring matching is spoofable."""
    tracker = ZoneTracker()
    spoof = chat("You have entered The Twilight Strand.")
    assert parse_line(spoof) is not None, "it is a well-formed log line, just not ours"
    assert parse_line(spoof).is_system is False
    assert tracker.feed(spoof) == []


def test_a_chat_line_cannot_forge_a_generating_line_either():
    tracker = ZoneTracker()
    assert tracker.feed(chat('Generating level 83 area "HideoutCanals" with seed 1')) == []
    assert tracker.pending_area_id is None


@pytest.mark.parametrize(
    "speaker",
    ["Spoofer", "#Spoofer", "@From Spoofer", "$Spoofer", "%Spoofer", "&Spoofer"],
)
def test_no_chat_channel_can_spoof_a_zone_event(speaker: str):
    tracker = ZoneTracker()
    assert tracker.feed(chat("You have entered Hall of Grandmasters.", speaker=speaker)) == []


def test_the_real_system_line_still_works_after_all_that():
    tracker = ZoneTracker()
    events = tracker.feed(entered("The Twilight Strand"))
    assert [e.name for e in events] == ["The Twilight Strand"]


# -- gotcha 2: `<<set:..>>` markers --------------------------------------------------


def test_set_tokens_are_stripped():
    assert strip_set_tokens("<<set:MS>><<set:M>><<set:M>>Sie haben") == "Sie haben"
    assert strip_set_tokens("no markers here") == "no markers here"


def test_a_set_marked_english_line_still_parses():
    line = parse_line(
        "2018/05/13 16:10:14 1801062 9b0 [INFO Client 1636] "
        ": <<set:MS>><<set:M>><<set:M>>You have entered The Twilight Strand."
    )
    assert line is not None
    assert line.body == "You have entered The Twilight Strand."


def test_a_non_english_client_is_still_tracked_through_the_area_id():
    """The phrase is translated so it will not match, and it does not have to.

    The `Generating` line is language-independent, so a Russian client still
    produces a correctly classified hideout entry — a couple of seconds later,
    which the consumer's 20 s debounce absorbs.
    """
    clock = FakeClock()
    tracker = ZoneTracker(clock=clock, pair_window=2.0)
    russian = (
        "2018/05/13 16:10:14 1801062 9b0 [INFO Client 1636] "
        ": <<set:MS>><<set:M>><<set:M>>Вы вошли в область Логово."
    )
    assert tracker.feed(generating("HideoutCanals")) == []
    assert tracker.feed(russian) == [], "the translated phrase does not match, as expected"

    clock.advance(3.0)
    events = tracker.flush()
    assert [(e.kind, e.area_id, e.source) for e in events] == [
        (ZoneKind.HIDEOUT, "HideoutCanals", ZoneSource.GENERATED)
    ]


def test_a_localized_phrase_can_be_taught_through_the_settings_hook():
    patterns = compile_patterns([r"^Вы вошли в область (?P<name>.+?)\.\s*$"])
    tracker = ZoneTracker(entered_patterns=patterns)
    tracker.feed(generating("HideoutCanals"))
    events = tracker.feed(
        "2018/05/13 16:10:14 1801062 9b0 [INFO Client 1636] "
        ": <<set:MS>>Вы вошли в область Логово."
    )
    assert [(e.kind, e.area_id, e.name) for e in events] == [
        (ZoneKind.HIDEOUT, "HideoutCanals", "Логово")
    ]


def test_an_uncompilable_user_pattern_is_dropped_not_fatal():
    patterns = compile_patterns(["(unclosed", "", 7, r"^Entered (?P<name>.+)$"])  # type: ignore[list-item]
    assert len(patterns) == 2  # the built-in English one plus the valid addition


def test_a_user_pattern_with_a_bare_group_works():
    tracker = ZoneTracker(entered_patterns=compile_patterns([r"^Entrou em (.+?)\.$"]))
    events = tracker.feed(
        "2018/05/13 16:10:14 1801062 9b0 [INFO Client 1636] : Entrou em Praia Lioneye."
    )
    assert [e.name for e in events] == ["Praia Lioneye"]


# -- gotcha 3: classify on the id, never the display name -----------------------------


@pytest.mark.parametrize(
    ("area_id", "expected"),
    [
        ("HideoutCanals", ZoneKind.HIDEOUT),
        ("HarvestHideout", ZoneKind.HIDEOUT),
        ("HideoutGuild", ZoneKind.HIDEOUT),
        ("1_town", ZoneKind.TOWN),
        ("10_town", ZoneKind.TOWN),
        ("HeistHub", ZoneKind.TOWN),
        ("MapWorldsGrotto", ZoneKind.MAP),
        ("MapAtziri1", ZoneKind.MAP),
        ("1_1_1", ZoneKind.OTHER),
        ("Delve_Main", ZoneKind.OTHER),
        ("SanctumArena", ZoneKind.OTHER),
    ],
)
def test_classification_is_keyed_on_the_area_id(area_id: str, expected: ZoneKind):
    assert classify(area_id) == expected


def test_a_user_themed_hideout_name_does_not_beat_its_id():
    """"Canal Hideout" renamed to "Grotto" must not become a map."""
    assert classify("HideoutCanals", "Grotto") == ZoneKind.HIDEOUT
    assert classify("MapWorldsGrotto", "My Cozy Hideout") == ZoneKind.MAP


def test_a_translated_name_cannot_misclassify_a_map():
    assert classify("MapWorldsGrotto", "Grotte") == ZoneKind.MAP


def test_the_name_is_only_a_fallback_when_there_is_no_id():
    assert classify(None, "Canal Hideout") == ZoneKind.HIDEOUT
    assert classify(None, "The Twilight Strand") == ZoneKind.OTHER
    assert classify(None, None) == ZoneKind.OTHER


# -- correlation ---------------------------------------------------------------------


def test_the_two_lines_are_paired_into_one_event():
    tracker = ZoneTracker()
    assert tracker.feed(generating("MapWorldsGrotto", level=83)) == []
    events = tracker.feed(entered("Grotto"))
    assert len(events) == 1
    event = events[0]
    assert (event.kind, event.area_id, event.name, event.level) == (
        ZoneKind.MAP,
        "MapWorldsGrotto",
        "Grotto",
        83,
    )
    assert event.source is ZoneSource.PAIRED
    assert event.log_time == "2018/05/13 16:10:14"


def test_re_entering_an_existing_instance_still_reports():
    """No `Generating` line — the open question in IMPLEMENTATION-PLAN §7."""
    tracker = ZoneTracker()
    events = tracker.feed(entered("Canal Hideout"))
    assert [(e.kind, e.area_id, e.source) for e in events] == [
        (ZoneKind.HIDEOUT, None, ZoneSource.ENTERED)
    ]


def test_a_generated_area_is_emitted_alone_once_the_window_passes():
    clock = FakeClock()
    tracker = ZoneTracker(clock=clock, pair_window=2.0)
    tracker.feed(generating("HideoutCanals"))
    clock.advance(1.0)
    assert tracker.flush() == [], "still inside the pairing window"
    clock.advance(1.5)
    assert [e.area_id for e in tracker.flush()] == ["HideoutCanals"]
    assert tracker.flush() == [], "and only once"


def test_two_generations_with_no_confirmation_both_report():
    tracker = ZoneTracker()
    assert tracker.feed(generating("MapWorldsGrotto")) == []
    events = tracker.feed(generating("HideoutCanals"))
    assert [e.area_id for e in events] == ["MapWorldsGrotto"]
    assert tracker.pending_area_id == "HideoutCanals"


def test_drain_reports_a_pending_area_immediately():
    tracker = ZoneTracker()
    tracker.feed(generating("HideoutCanals"))
    assert [e.area_id for e in tracker.drain()] == ["HideoutCanals"]
    assert tracker.drain() == []


def test_unrelated_lines_do_not_disturb_a_pending_pairing():
    tracker = ZoneTracker()
    tracker.feed(generating("MapWorldsGrotto"))
    tracker.feed("2018/05/13 16:10:14 1801062 9b0 [INFO Client 1636] Connecting to instance server")
    tracker.feed(chat("gl hf"))
    events = tracker.feed(entered("Grotto"))
    assert [e.source for e in events] == [ZoneSource.PAIRED]


def test_the_default_pattern_set_is_english_only():
    """A guessed translation is worse than none: the id fallback already covers it."""
    from modules.gamelog.backend.parse import ENTERED_PATTERNS

    assert len(ENTERED_PATTERNS) == 1
    assert ENTERED_PATTERNS[0].pattern.startswith("^You have entered ")
    assert isinstance(ENTERED_PATTERNS[0], re.Pattern)
