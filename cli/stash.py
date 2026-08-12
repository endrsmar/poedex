"""`poedex stash` — the tab list, one tab, and the crawl nobody starts by accident.

Four subcommands, and the split between them is a split about **cost**:

    poedex stash              the tab list. Zero item requests.
    poedex stash tab N        one tab, judged. One request, or none if it is cached.
    poedex stash plan         what a full refresh would cost, right now.
    poedex stash crawl        the cold crawl. Minutes. Refuses without --yes.

That ordering is SPEC §6.6's, in a command: *lazy per-tab fetch on open is the primary
path*, and a crawl is a thing the player asks for after being told what it costs.

Three things the output is built to get right:

* **A tab nobody has read is not an empty tab.** It prints ``—`` and is counted in the
  footer, exactly as an ``unpriceable`` row is in `poedex appraise`. A zero there
  would be the failure mode this phase was warned about.
* **A map tab says *not supported yet*.** research-notes §7 and Phase 10's own
  sampling: five map tabs across two leagues answered with no items, and GGG's own
  stash API models them as parents whose children are fetched one at a time. The zero
  is far more likely to mean *not traversed* than *empty*, so it is not printed as a
  value.
* **The cost is computed, not quoted.** ``~30 min`` is what a *cold* Standard stash
  costs. After the remove-only tabs are cached it is under a minute, and a warning
  that keeps saying half an hour is a warning people stop reading.
"""

from __future__ import annotations

import textwrap

from cli.appraise import RULE, render_appraisal, render_summary, use_colour
from cli.value import format_chaos, prepare_league
from modules.appraisal.backend.api import (
    AppraisalApi,
    StashDigest,
    Strictness,
    TabAppraisal,
    TabSummary,
)
from modules.poeapi.backend.api import CrawlPlan, PoeApi
from modules.prices.backend.api import PricesApi

MAX_NAME = 30
LABEL = 12
"""Every line in this report is ``label`` then content at column 12, matching
`poedex appraise`. Wrapped continuations hang to the same column."""


def _wrap(label: str, text: str) -> list[str]:
    """``label`` then ``text``, wrapped to the terminal's rule and hanging-indented.

    The map-tab explanation is a paragraph, not a phrase — it has to say what was
    measured and why the zero is not trusted — and a paragraph printed as one 400
    character line is a paragraph nobody reads.
    """
    body = textwrap.wrap(text, width=len(RULE) - LABEL) or [""]
    pad = " " * LABEL
    return [f"{label:<{LABEL}}{body[0]}"] + [f"{pad}{line}" for line in body[1:]]


def _age(summary: TabSummary) -> str:
    """How old this tab's copy is, in the words the row needs.

    ``never`` is a first-class answer, and ``cached`` — with no age — is the
    remove-only tab: it cannot gain items, so its copy is not "old", it is *done*.
    """
    if not summary.cached:
        return "never"
    if summary.permanent:
        return "permanent"
    seconds = summary.age_seconds or 0.0
    if seconds < 90:
        return f"{seconds:.0f}s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m ago"
    if seconds < 172_800:
        return f"{seconds / 3600:.0f}h ago"
    return f"{seconds / 86_400:.0f}d ago"


def _shape(summary: TabSummary) -> str:
    layout = summary.to_json()
    if layout["grid"]:
        return f"{layout['cols']}x{layout['rows']}"
    return "special"


def render_tab_row(summary: TabSummary) -> str:
    """One tab: index, name, kind, shape, freshness, contents, value."""
    row = summary.to_json()
    name = summary.tab.name or "(unnamed)"
    if len(name) > MAX_NAME:
        name = name[: MAX_NAME - 1] + "…"
    flags = "".join(
        [
            "R" if summary.tab.remove_only else " ",
            "H" if summary.tab.hidden else " ",
            "!" if summary.highlighted else " ",
        ]
    )
    if not summary.supported:
        contents, value = "not supported yet", "     ?"
    elif not summary.known:
        # Never read. Not zero, and not empty — the two are different facts and only
        # one of them is worth walking to the stash for.
        contents, value = "not read yet", "     —"
    else:
        units = f", {summary.units} unit(s)" if summary.units != row["item_count"] else ""
        extra = []
        if summary.unpriceable_count:
            extra.append(f"{summary.unpriceable_count} unpriced")
        if summary.highlighted:
            extra.append(f"{summary.highlighted} to check")
        contents = f"{row['item_count']} item(s){units} · {row['composition']}" + (
            " · " + ", ".join(extra) if extra else ""
        )
        # `≥` for the same reason the bag total carries it: a tab holding a removed
        # item or an unchecked rare has money in it that this figure does not.
        floor = "≥" if summary.unpriceable_count or summary.highlighted else " "
        value = f"{floor}{format_chaos(summary.total_chaos):>5}c"
    return (
        f"  {summary.tab.index:>3} {flags} {name:<{MAX_NAME}} "
        f"{row['kind']:<11} {_shape(summary):<8} {_age(summary):>10}  {value}  {contents}"
    )


def render_digest(digest: StashDigest) -> str:
    lines = [
        f"  idx     {'name':<{MAX_NAME}} {'kind':<11} {'shape':<8} {'fetched':>10}  "
        f"{'value':>7}  contents",
        RULE,
    ]
    lines.extend(render_tab_row(summary) for summary in digest.ranked())
    return "\n".join(lines)


def render_cost(plan: CrawlPlan) -> str:
    """What a full refresh costs, and what it costs you *while* it runs.

    The second half is the part that matters on a Steam Deck: `get-items` and
    `get-stash-items` share one bucket, so a crawl is not a background task — it is
    the tool declining to sync your backpack for half an hour.
    """
    lines = _wrap("to refresh:", plan.warning)
    lines += [
        f"            {plan.cached_tabs}/{plan.total_tabs} tab(s) cached, "
        f"{plan.permanent_tabs} of them permanently (remove-only)"
    ]
    if plan.unsupported_tabs:
        lines.append(f"            {plan.unsupported_tabs} tab(s) cannot be read at all")
    return "\n".join(lines)


def render_digest_summary(digest: StashDigest) -> str:
    money = [f"{format_chaos(digest.total_chaos)} chaos"]
    if digest.total_divine is not None:
        money.append(f"{digest.total_divine:,.2f} divine")
    prefix = "≥ " if digest.total_is_floor else ""
    lines = [RULE, f"{'stash:':<{LABEL}}{prefix}" + "  ·  ".join(money)]
    if digest.unread:
        lines += _wrap(
            "",
            f"{len(digest.unread)} tab(s) have never been read — their value is "
            "unknown, not zero. 'poedex stash tab N' reads one",
        )
    if digest.unsupported:
        lines += _wrap(
            "",
            f"{len(digest.unsupported)} tab(s) cannot be read: "
            f"{digest.unsupported[0].tab.unsupported_reason}",
        )
    if digest.highlighted:
        lines += _wrap(
            "highlight:",
            f"{digest.highlighted} item(s) worth asking about across the tabs that "
            "have been read — 'poedex price <uid> --tab N' asks about one",
        )
    lines.append(f"{'gate:':<{LABEL}}{digest.strictness.value} (stash)")
    return "\n".join(lines)


async def cmd_stash_list(
    appraisal: AppraisalApi,
    prices: PricesApi,
    *,
    league: str | None = None,
    refresh: bool = False,
) -> int:
    """The tab list. Costs at most one request — the tab list itself."""
    digest = await appraisal.stash_digest(league, refresh=refresh)
    # Never a bare league name. Every chaos figure below is denominated in one
    # economy, and the player is the only one who can tell whether it is the right
    # one — so the output says where the name came from.
    choice = await prepare_league(prices, digest.league, override=league)
    print(f"{'league:':<{LABEL}}{choice.describe()}")
    print(render_cost(digest.cost))
    print()
    print(render_digest(digest))
    print()
    print(render_digest_summary(digest))
    print("flags:      R remove-only · H hidden · ! has highlighted items")
    return 0


async def cmd_stash_plan(
    appraisal: AppraisalApi, prices: PricesApi, *, league: str | None = None
) -> int:
    digest = await appraisal.stash_digest(league)
    # Never a bare league name, the same rule `cmd_stash_list` above already follows.
    # `league_choice` rather than `prepare_league`: this command's whole promise is
    # that it costs nothing, and loading tables to print a provenance would break it.
    print(f"league:     {prices.league_choice(digest.league, explicit=league).describe()}")
    print(render_cost(digest.cost))
    print(
        "\n".join(
            _wrap(
                "",
                "nothing crawls by itself — run 'poedex stash crawl --yes' to spend "
                "this, or open tabs one at a time with 'poedex stash tab N'",
            )
        )
    )
    return 0


async def cmd_stash_tab(
    appraisal: AppraisalApi,
    prices: PricesApi,
    tab_index: int,
    *,
    league: str | None = None,
    strictness: str | None = None,
    refresh: bool = False,
    show_all: bool = False,
    colour: bool | None = None,
) -> int:
    """One tab, judged at stash strictness. The primary path (SPEC §6.6)."""
    result: TabAppraisal = await appraisal.appraise_tab(
        tab_index,
        league=league,
        strictness=Strictness(strictness) if strictness else None,
        refresh=refresh,
    )
    summary = result.summary
    painted = use_colour() if colour is None else colour

    choice = await prepare_league(prices, result.appraisal.league, override=league)
    print(f"tab:        {tab_index} — {summary.tab.name or '(unnamed)'} [{summary.tab.type}]")
    print(f"{'league:':<{LABEL}}{choice.describe()}")
    print(f"layout:     {_shape(summary)}" + ("  (remove-only)" if summary.permanent else ""))
    print(f"fetched:    {_age(summary)}")
    if not result.supported:
        # The one case where there is nothing to print and something to say.
        print()
        print("\n".join(_wrap("unsupported:", result.unsupported or "")))
        return 1
    print(
        f"contents:   {summary.item_count} item(s), {summary.units} unit(s) · "
        f"{summary.composition.value}"
    )
    print(
        f"highlight:  {result.appraisal.strictness.value} (stash) — "
        f"{summary.highlighted} row(s) worth asking about"
    )
    print()
    print(render_appraisal(result.appraisal, show_all=show_all, colour=painted))
    print()
    print(render_summary(result.appraisal, colour=painted, label="tab"))
    print(
        f"{'check:':<{LABEL}}'poedex price <uid> --tab {tab_index}' asks the market "
        "about one of them"
    )
    return 0


async def cmd_stash_crawl(
    poeapi: PoeApi,
    appraisal: AppraisalApi,
    *,
    league: str | None = None,
    yes: bool = False,
    resume: bool = True,
    limit: int | None = None,
) -> int:
    """The cold crawl: user-initiated, resumable, and it states its cost first.

    Without ``--yes`` this prints the cost and stops. That refusal is the feature —
    a crawl spends the account's whole item budget for as long as it runs, and the
    thing it pauses is the player's own inventory syncing.
    """
    digest = await appraisal.stash_digest(league)
    print(f"league:     {digest.league}")
    print(render_cost(digest.cost))
    progress = await poeapi.crawl_progress(digest.league)
    if progress is not None and progress.done:
        print(
            f"resume:     {len(progress.done)} tab(s) already done"
            + (f", {len(progress.failed)} failed" if progress.failed else "")
        )
    if not yes:
        print()
        print("nothing was fetched. Re-run with --yes to spend the above.")
        return 1

    print()
    spent = 0
    items = 0
    async for step in poeapi.crawl_stash(digest.league, resume=resume, limit=limit):
        spent += step.spent
        if step.error:
            print(f"  [{step.index}/{step.total}] tab {step.tab.index} failed: {step.error}")
            continue
        count = len(step.items.items) if step.items else 0
        items += count
        source = "cached" if step.from_cache else "fetched"
        print(
            f"  [{step.index}/{step.total}] tab {step.tab.index} "
            f"{step.tab.name or '(unnamed)':<20} {count:>4} item(s)  {source}"
        )
    print()
    print(f"crawl:      {spent} request(s) spent, {items} item(s) read")
    print("            'poedex stash' now has values for every tab it reached")
    return 0
