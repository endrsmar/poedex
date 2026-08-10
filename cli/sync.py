"""`poedex sync` — fetch the bag and print the normalized model.

Phase 2's exit criterion (IMPLEMENTATION-PLAN §5). Deliberately a dump rather than a
verdict: there is no pricing yet, and the point of looking at this output is to see
whether normalization got the *structure* right — categories, stacks, sockets, notes
— before anything is built on top of it.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from textwrap import indent
from typing import Any

from modules.net.backend.api import LimitSnapshot, describe_limits
from modules.poeapi.backend.api import ItemSet, NormalizedItem, PoeApi, Source

MAX_NAME = 38


def render_item(item: NormalizedItem) -> str:
    """One line per item. The columns are the fields Phase 3 will price on."""
    name = item.name if len(item.name) <= MAX_NAME else item.name[: MAX_NAME - 1] + "…"
    stack = f"x{item.stack_size}" if item.stack_size > 1 else ""
    grid = f"({item.grid.x},{item.grid.y})"
    sockets = ""
    if item.sockets.count:
        sockets = f"{item.sockets.count}S/{item.sockets.links}L"
    flags = "".join(
        (
            "C" if item.corrupted else "",
            "F" if item.fractured else "",
            "S" if item.synthesised else "",
            "?" if not item.identified else "",
        )
    )
    note = f'  note="{item.note}"' if item.note else ""
    influences = f"  [{','.join(item.influences)}]" if item.influences else ""
    return (
        f"  {grid:>8} {name:<{MAX_NAME}} {item.rarity.value:<10} "
        f"{item.category:<10} {stack:>8} {sockets:<7} {flags:<4}"
        f"{influences}{note}"
    )


def render_set(result: ItemSet, *, sources: Iterable[Source]) -> str:
    lines: list[str] = []
    for source in sources:
        items = result.by_source(source)
        if not items:
            continue
        lines.append(f"{source.value} — {len(items)} item(s)")
        lines.extend(render_item(item) for item in sorted(items, key=_grid_order))
        lines.append("")
    return "\n".join(lines)


def _grid_order(item: NormalizedItem) -> tuple[int, int, str]:
    return item.grid.y, item.grid.x, item.name


def render_freshness(result: ItemSet) -> str:
    meta = result.meta
    if meta.stale:
        state = "STALE"
    elif meta.from_cache:
        state = "cached"
    else:
        state = "fresh"
    parts = [f"{state}, fetched {meta.fetched_at.isoformat(timespec='seconds')}"]
    if meta.retry_after:
        parts.append(f"refresh available in {meta.retry_after:.0f}s")
    if meta.note:
        parts.append(meta.note)
    return " · ".join(parts)


def render_categories(result: ItemSet, source: Source) -> str:
    counts: dict[str, int] = {}
    for item in result.by_source(source):
        counts[item.category] = counts.get(item.category, 0) + 1
    if not counts:
        return "  (empty)"
    return "  " + "  ".join(f"{name}={count}" for name, count in sorted(counts.items()))


async def cmd_sync(
    poeapi: PoeApi,
    *,
    character: str | None,
    refresh: bool,
    equipment: bool,
) -> int:
    result = await poeapi.get_items(character, refresh=refresh)
    sources = [Source.BAG, Source.EQUIPMENT] if equipment else [Source.BAG]

    print(f"character:  {result.character}")
    print(f"freshness:  {render_freshness(result)}")
    print(f"hash:       {result.content_hash[:16]}")
    print()
    body = render_set(result, sources=sources)
    print(body if body else "bag — 0 item(s)\n")
    print("categories:")
    print(render_categories(result, Source.BAG))
    print()
    print("rate limits:")
    print(indent(render_limits(poeapi.limits()), "  "))
    if result.meta.stale:
        print("\nthis is cached data; nothing was fetched", file=sys.stderr)
        return 1
    return 0


def render_limits(snapshots: Iterable[dict[str, Any]]) -> str:
    """`PoeApi.limits()` hands back JSON; `describe_limits` wants the dataclass.

    Rebuilding it here keeps the CLI on the JSON contract a transport would also
    use, rather than quietly depending on the in-process object.
    """
    return describe_limits(LimitSnapshot(**entry) for entry in snapshots)
