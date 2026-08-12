"""`poedex characters` — the roster, and which one the tool would read.

This command exists because the bug it diagnoses is invisible from every other
angle. `poedex sync` prints a character name; `poedex appraise` prints a character
name; both of them printed the *wrong* name for weeks and looked exactly like they
do when they are right. The one question neither could answer was **why that name**,
and the answer turned out to be "because GGG listed it first".

So the whole output is provenance. Every character with its league — the column that
tells a parked character from a played one, and the column whose absence made a
four-fold pricing error look reasonable — the last-played time as a date a human can
check against their own memory, and one closing sentence naming the winner *and the
rule it won under*.

Costs one `get-characters`, cached for an hour, so it is affordable to run before
anything else and to run again after changing the setting.
"""

from __future__ import annotations

from datetime import UTC, datetime

from modules.poeapi.backend.api import (
    Character,
    CharacterSelection,
    CharacterSource,
    ItemSet,
    PoeApi,
)

NAME_WIDTH = 24
LEAGUE_WIDTH = 18
CLASS_WIDTH = 14

# What each rule means, in the words somebody diagnosing this would want. Keyed by
# the enum so a new source cannot be added without a sentence to go with it.
WHY: dict[CharacterSource, str] = {
    CharacterSource.ARGUMENT: "you named it with --character",
    CharacterSource.ENVIRONMENT: "POEDEX_CHARACTER is set in this environment",
    CharacterSource.SETTING: "the poeapi.character setting is pinned to it",
    CharacterSource.CURRENT: "the API marks it as the character being played",
    CharacterSource.LAST_LOGIN: "it has the most recent lastLoginTime on the account",
    CharacterSource.FALLBACK: (
        "GUESSED — nothing marks a character as current and none carries a "
        "lastLoginTime, so this is only the first entry GGG returned"
    ),
    CharacterSource.NONE: "there is nothing to read",
}


# The same rules in one clause each, for a header line rather than a report. Every
# renderer in the CLI reads this table, so there is one place where "which character"
# is put into words and no way for two screens to phrase a guess differently.
SHORT_WHY: dict[CharacterSource, str] = {
    CharacterSource.ARGUMENT: "--character",
    CharacterSource.ENVIRONMENT: "POEDEX_CHARACTER",
    CharacterSource.SETTING: "pinned by poeapi.character",
    CharacterSource.CURRENT: "the character you are playing",
    CharacterSource.LAST_LOGIN: "most recently played",
    CharacterSource.FALLBACK: "GUESSED — run 'poedex characters'",
    CharacterSource.NONE: "no character",
}


def describe_character(result: ItemSet) -> str:
    """``PlaceholderWarden (most recently played)`` — a name and the claim behind it.

    Short by design: it is one line of a header, and the long form is one command
    away. What no header may do is print the name alone, which is what every one of
    them did while the tool was reading a character nobody had chosen.
    """
    if not result.character:
        return "unknown"
    if result.character_source is None:
        return result.character
    tail = SHORT_WHY[result.character_source]
    if result.character_played_last and result.character_played_last != result.character:
        tail += f"; you last played {result.character_played_last}"
    return f"{result.character} ({tail})"


def render_last_login(when: datetime | None, *, now: datetime | None = None) -> str:
    """``2026-08-11 21:40  (yesterday)``, or ``never played`` when GGG says nothing.

    The relative half is what makes this checkable at a glance: a player knows which
    character they were on last night and does not know which Unix second that was.
    """
    if when is None:
        return "never played"
    now = now or datetime.now(UTC)
    # Calendar days in UTC, not elapsed time. "Yesterday evening" is fourteen hours
    # ago at lunchtime and the player still calls it yesterday; a duration calls it
    # today, which is the one word that makes them stop trusting the column.
    days = (now.astimezone(UTC).date() - when.astimezone(UTC).date()).days
    if days < 0:
        # A timestamp in the future is not a thing to smooth over into "today".
        ago = "in the future?"
    elif days == 0:
        ago = "today"
    elif days == 1:
        ago = "yesterday"
    elif days < 60:
        ago = f"{days} days ago"
    else:
        ago = f"{days // 30} months ago"
    return f"{when.astimezone(UTC):%Y-%m-%d %H:%M}  ({ago})"


def render_row(character: Character, *, chosen: bool, now: datetime | None = None) -> str:
    marker = ">" if chosen else " "
    return (
        f"{marker} {character.name:<{NAME_WIDTH}} "
        f"{(character.league or 'unknown'):<{LEAGUE_WIDTH}} "
        f"{(character.class_name or '—'):<{CLASS_WIDTH}} "
        f"{character.level:>3}  "
        f"{render_last_login(character.last_login, now=now)}"
    )


def render(selection: CharacterSelection, *, now: datetime | None = None) -> str:
    """The whole report. Pure, so a test reads the sentence rather than a screen."""
    choice = selection.choice
    lines = [
        f"  {'NAME':<{NAME_WIDTH}} {'LEAGUE':<{LEAGUE_WIDTH}} "
        f"{'CLASS':<{CLASS_WIDTH}} {'LVL':>3}  LAST PLAYED"
    ]
    for character in selection.characters:
        lines.append(render_row(character, chosen=character.name == choice.name, now=now))
    if not selection.characters:
        lines.append("  (this account has no characters)")

    lines.append("")
    if choice.name is None:
        lines.append("reading: nothing — there is no character to read.")
        return "\n".join(lines)

    lines.append(f"reading: {choice.name} — {WHY[choice.source]}.")
    if choice.overriding:
        # Not a warning. Reading a character other than the one you last played is
        # an ordinary thing to want; it is only a problem when nobody says it is
        # happening, which is the entire bug.
        lines.append(f"         you last played {choice.played_last}.")
    if choice.guessed:
        lines.append(
            "         Nothing about this pick is an observation. Choose a character "
            "in the panel, or run 'poedex config set poeapi.character <name>'."
        )
    elif not choice.stated:
        lines.append(
            "         To read a different one, pick it in the panel or run "
            "'poedex config set poeapi.character <name>'."
        )
    elif choice.source is CharacterSource.SETTING:
        lines.append(
            "         'poedex config unset poeapi.character' goes back to following "
            "whoever you played last."
        )
    return "\n".join(lines)


async def cmd_characters(poeapi: PoeApi, *, refresh: bool = False) -> int:
    selection = await poeapi.character_choice(refresh=refresh)
    meta = selection.meta
    state = "STALE" if meta.stale else ("cached" if meta.from_cache else "fresh")
    print(
        f"characters — {len(selection.characters)} on this account "
        f"({state}, read {meta.fetched_at.isoformat(timespec='seconds')})"
    )
    print()
    print(render(selection))
    # A guess is not an error — the command did what was asked — but it is the one
    # outcome a script should be able to notice without parsing prose.
    return 1 if selection.choice.guessed or selection.choice.name is None else 0
