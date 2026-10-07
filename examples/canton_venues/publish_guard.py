"""Publish guard: a venue's card and page are only overwritten when their figures are plausible.

The collector redraws every venue's page each tick and its card every few ticks. A bad read (a
venue answering with a partial pool list, a volume route that returns a day's worth twice, a source
that silently stops refreshing) would otherwise go straight onto a card people share. Before a
venue's page and card are overwritten, every headline figure is compared with the last one we
published for it:

- a figure that moves more than ``JUMP_UP`` times up or under ``JUMP_DOWN`` of its last published
  value, or that was there and is now missing, holds the venue on its last good card and page;
- the new level is published once it has been seen ``CONFIRM_TICKS`` checks in a row (each within
  ``CONFIRM_BAND`` of the first) and at least ``JUMP_WINDOW_S`` has passed since the last good
  publish, so a real move goes out within minutes and a one-tick glitch never does;
- a venue whose data has not been read successfully for ``STALE_AFTER_S`` is held until it is.

The state is ``venues/published.json``: per venue, the figures and headline now on its card. The
daily cross-check (crosscheck.py) compares those figures with outside sources. No LLM anywhere.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("venues.guard")

JUMP_UP = 3.0             # more than 3x the published value ...
JUMP_DOWN = 1 / 3         # ... or under a third of it ...
JUMP_WINDOW_S = 15 * 60   # ... within 15 minutes of the last good publish is implausible
CONFIRM_TICKS = 3         # a new level seen this many checks in a row is real
CONFIRM_BAND = 0.25       # "the same level": within 25% of the first sighting
STALE_AFTER_S = 40 * 60   # no successful read of a venue for this long: hold its card


def card_figures(f: dict, head: dict, tiles: list[dict]) -> dict[str, float]:
    """The numbers a venue's card shows, by name: what the guard watches and the cross-check checks."""
    shown = {t.get("m") for t in tiles}
    out = {name: f.get(name) for m, name in (("spot_volume", "spot_volume"), ("perp_volume", "perp_volume"),
                                             ("tvl", "tvl"), ("oi", "open_interest")) if m in shown}
    if "depth" in shown and f.get("deepest"):
        out["depth"] = f["deepest"].get("usd")
    # the headline's own figure, keyed by its rule: a lead that changes rule is not a jump. A best-price
    # edge moves by multiples in normal trading, so it is not watched.
    if isinstance(head.get("value"), (int, float)) and head.get("rule") != "best_quote":
        out[f"headline:{head['rule']}"] = head["value"]
    return {k: (float(v) if v is not None else None) for k, v in out.items()}


def _jumped(prev: float, cur: float | None) -> bool:
    if cur is None or cur <= 0:
        return True
    return cur > prev * JUMP_UP or cur < prev * JUMP_DOWN


def _same_level(a: dict, b: dict) -> bool:
    for k in set(a) | set(b):
        x, y = a.get(k), b.get(k)
        if (x is None) != (y is None):
            return False
        if x is not None and y is not None and abs(x - y) > CONFIRM_BAND * max(abs(x), abs(y), 1e-9):
            return False
    return True


class PublishGuard:
    """Holds a venue on its last good card and page while its new figures look implausible."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            self.state = json.loads(path.read_text())
        except (OSError, ValueError):
            self.state = {}
        self.state.setdefault("venues", {})

    def check(self, slug: str, figures: dict[str, float | None], now: int, age_s: float | None = None) -> str | None:
        """None when the venue may be published now, else why it is held (for the log)."""
        if age_s is not None and age_s > STALE_AFTER_S:
            return f"data is {int(age_s // 60)} min old (limit {STALE_AFTER_S // 60})"
        prev = self.state["venues"].get(slug)
        if not prev:
            return None
        odd = {}
        for name, was in (prev.get("figures") or {}).items():
            if name.startswith("headline:") and name not in figures:
                continue  # the lead moved to another rule: nothing to compare
            if was and was > 0 and _jumped(was, figures.get(name)):
                odd[name] = (was, figures.get(name))
        if not odd:
            prev.pop("pending", None)
            return None
        seen = {k: v[1] for k, v in odd.items()}
        pend = prev.get("pending")
        if pend and set(pend["figures"]) == set(seen) and _same_level(pend["figures"], seen):
            pend["n"] += 1
        else:
            pend = prev["pending"] = {"figures": seen, "n": 1, "since": now}
        if pend["n"] >= CONFIRM_TICKS and now - prev["t"] >= JUMP_WINDOW_S:
            log.info("%s: new level confirmed after %d checks: %s", slug, pend["n"], _describe(odd))
            return None
        return f"{_describe(odd)} (seen {pend['n']} of {CONFIRM_TICKS})"

    def accept(self, slug: str, figures: dict, head: dict, now: int) -> None:
        self.state["venues"][slug] = {"t": now, "figures": figures,
                                      "head": {k: head.get(k) for k in ("rule", "title", "sub")}}

    def last_head(self, slug: str) -> dict | None:
        return (self.state["venues"].get(slug) or {}).get("head")

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.state, separators=(",", ":")))
        os.replace(tmp, self.path)


def _describe(odd: dict) -> str:
    parts = []
    for name, (was, now) in sorted(odd.items()):
        parts.append(f"{name} missing (was {was:,.0f})" if not now else f"{name} {was:,.0f} -> {now:,.0f}")
    return ", ".join(parts)
