#!/usr/bin/env python3
"""Full external-integration check against the reference DEX hosted testnet.

Exercises every surface an outside integrator has on
https://testnet-dex.bitdynamics.cc: the AMM, the order book, RFQ, and
liquidity provision. Trading calls go through the venue adapter; order,
RFQ and liquidity use the deployment's hosted routes, which is the only
path a party without its own wallet has.

Read-only by default. `--execute` submits real testnet transactions.

    PYTHONPATH=src python3 scripts/dexref_testnet_report.py
    PYTHONPATH=src python3 scripts/dexref_testnet_report.py --execute

Parties come from DEXREF_PARTY_A / DEXREF_PARTY_B when set, otherwise the
script allocates them from the public faucet. The faucet allows three
parties per IP per day, so reuse them across runs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from decimal import Decimal

import httpx

from cantonvenues import DexRefAdapter, HostedPartySwapRoute, Instrument

DEFAULT_BASE = "https://testnet-dex.bitdynamics.cc/api"
BASE_PAIR = ("dBTC", "dUSD")

QUOTE_LEVELS = [
    ("Bid", "70000", "0.0001"),
    ("Bid", "69000", "0.0001"),
    ("Ask", "99000", "0.0001"),
    ("Ask", "98000", "0.0001"),
]


class Report:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.rows: list[tuple[str, bool, str]] = []

    def check(self, label: str, condition: object, detail: str = "") -> bool:
        cond = bool(condition)
        if cond:
            self.passed += 1
            print(f"  PASS  {label}" + (f"  [{detail}]" if detail else ""))
        else:
            self.failed += 1
            print(f"  FAIL  {label}" + (f"  [{detail}]" if detail else ""))
        self.rows.append((label, cond, detail))
        return cond


def head(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


async def allocate_party(client: httpx.AsyncClient, base: str) -> str:
    r = await client.post(f"{base}/v1/testnet/party", json={})
    if r.status_code == 429:
        raise SystemExit(
            "faucet quota reached (three parties per IP per day); "
            "set DEXREF_PARTY_A / DEXREF_PARTY_B to reuse earlier ones"
        )
    r.raise_for_status()
    return r.json()["partyId"]


async def balances(client: httpx.AsyncClient, base: str, party: str) -> dict[str, dict[str, Decimal]]:
    r = await client.get(f"{base}/v1/balances", params={"owner": party})
    r.raise_for_status()
    return {
        b["instrumentId"]: {k: Decimal(b[k]) for k in ("total", "available", "locked")}
        for b in r.json()
    }


def show(tag: str, bal: dict[str, dict[str, Decimal]]) -> None:
    print(f"  {tag}")
    for instrument, v in sorted(bal.items()):
        print(
            f"    {instrument:14} total {v['total']:>18} "
            f"available {v['available']:>18} locked {v['locked']:>16}"
        )


async def place_order(
    client: httpx.AsyncClient, base: str, party: str, side: str, price: str, qty: str
) -> dict:
    r = await client.post(
        f"{base}/v1/testnet/order",
        json={
            "party": party,
            "baseInstrumentId": BASE_PAIR[0],
            "quoteInstrumentId": BASE_PAIR[1],
            "side": side,
            "limitPrice": price,
            "quantity": qty,
        },
    )
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"text": r.text}
    body["_status"] = r.status_code
    return body


async def read_surface(client: httpx.AsyncClient, base: str, rep: Report) -> None:
    head("1. READ SURFACE")

    instruments = (await client.get(f"{base}/v1/instruments")).json()
    print(f"  instruments: {json.dumps(instruments)[:300]}")
    rep.check(
        "every instrument reports its scale",
        all(isinstance(i.get("decimals"), int) for i in instruments),
        ", ".join(f"{i['instrumentId']}={i.get('decimals')}" for i in instruments),
    )

    r = await client.get(f"{base}/v1/rfq")
    rep.check("GET /v1/rfq refuses an unscoped read", r.status_code == 400, str(r.status_code))
    r = await client.get(f"{base}/v1/trades")
    rep.check("GET /v1/trades refuses an unscoped read", r.status_code == 400, str(r.status_code))

    r = await client.get(f"{base}/v1/orders/book", params={"pair": "dBTC/dUSD"})
    rep.check("book takes ?pair=", r.status_code == 200, str(r.status_code))
    r = await client.get(f"{base}/v1/orders/book", params={"base": "dBTC", "quote": "dUSD"})
    rep.check("book still takes ?base=&quote=", r.status_code == 200, str(r.status_code))

    swaps = (await client.get(f"{base}/v1/swaps")).json()
    strings = [s for s in swaps if isinstance(s.get("inputAmount"), str)]
    rep.check(
        "every swap row serves amounts as decimal strings",
        len(strings) == len(swaps),
        f"{len(strings)}/{len(swaps)} rows",
    )
    scaled = [s for s in swaps if len(str(s["outputAmount"]).split(".")[-1]) == 10]
    rep.check(
        "every swap row keeps the ledger scale",
        len(scaled) == len(swaps),
        f"{len(scaled)}/{len(swaps)} rows at 10 dp",
    )


async def quoting(
    client: httpx.AsyncClient, base: str, party: str, rep: Report
) -> list[tuple[str, str, str, str]]:
    head("2. MULTI-LEVEL TWO-SIDED QUOTING FROM ONE PARTY")

    before = await balances(client, base, party)
    show("before", before)

    placed: list[tuple[str, str, str, str]] = []
    for side, price, qty in QUOTE_LEVELS:
        body = await place_order(client, base, party, side, price, qty)
        cid = body.get("orderCid") or ""
        if body["_status"] == 200 and cid:
            placed.append((side, price, qty, cid))
        rep.check(f"{side} {qty} @ {price}", body.get("status") == "Funded", str(body.get("status"))[:60])

    resting = await balances(client, base, party)
    show("with the quotes resting", resting)

    want_quote = sum(Decimal(p) * Decimal(q) for s, p, q in QUOTE_LEVELS if s == "Bid")
    want_base = sum(Decimal(q) for s, p, q in QUOTE_LEVELS if s == "Ask")
    got_quote = resting[BASE_PAIR[1]]["locked"] - before[BASE_PAIR[1]]["locked"]
    got_base = resting[BASE_PAIR[0]]["locked"] - before[BASE_PAIR[0]]["locked"]

    rep.check("quote locked == sum of bid notionals", got_quote == want_quote, f"{got_quote} vs {want_quote}")
    rep.check("base locked == sum of ask sizes", got_base == want_base, f"{got_base} vs {want_base}")
    rep.check(
        "locking moves nothing out of the balance",
        resting[BASE_PAIR[1]]["total"] == before[BASE_PAIR[1]]["total"],
    )

    book = (await client.get(f"{base}/v1/orders/book", params={"pair": "dBTC/dUSD"})).json()
    bids = {Decimal(lvl["price"]) for lvl in book["bids"]}
    asks = {Decimal(lvl["price"]) for lvl in book["asks"]}
    for side, price, _qty, _cid in placed:
        rep.check(f"{side} @ {price} visible in the book", Decimal(price) in (bids if side == "Bid" else asks))

    return placed


async def crossing(
    client: httpx.AsyncClient,
    base: str,
    maker: str,
    taker: str,
    resting: list[tuple[str, str]],
    rep: Report,
) -> list[tuple[str, str]]:
    head("3. A CROSSED BOOK, SETTLED FROM OUTSIDE")

    before_maker = await balances(client, base, maker)
    before_taker = await balances(client, base, taker)

    cids: list[tuple[str, str]] = list(resting)
    m = await place_order(client, base, maker, "Bid", "90000", "0.0002")
    if m.get("orderCid"):
        cids.append((maker, m["orderCid"]))
    rep.check("maker bid funded", m.get("status") == "Funded", str(m.get("status"))[:60])

    t = await place_order(client, base, taker, "Ask", "89000", "0.0001")
    if t.get("orderCid"):
        cids.append((taker, t["orderCid"]))
    rep.check("taker ask funded", t.get("status") == "Funded", str(t.get("status"))[:60])

    await asyncio.sleep(6)

    preview = await client.get(f"{base}/v1/orders/matches", params={"pair": "dBTC/dUSD"})
    matches = preview.json().get("matches", []) if preview.status_code == 200 else []
    print(f"  GET /v1/orders/matches -> {preview.status_code}, {len(matches)} crossing pairs")
    rep.check("the matcher sees the cross", len(matches) > 0, f"{len(matches)} pairs")

    # The hosted trigger takes no party, no cid and no amount: it runs the
    # operator's own matcher over one listed pair and clears whatever crosses.
    hosted = await client.post(
        f"{base}/v1/testnet/match", json={"base": BASE_PAIR[0], "quote": BASE_PAIR[1]}
    )
    receipt = hosted.json() if hosted.headers.get("content-type", "").startswith("application/json") else {}
    print(f"  POST /v1/testnet/match -> {hosted.status_code} {json.dumps(receipt)[:400]}")

    rep.check(
        "an external party can settle a crossed book",
        hosted.status_code in (200, 207),
        f"{hosted.status_code}, settled {receipt.get('settled')}, failed {receipt.get('failed')}",
    )
    settled = [o for o in receipt.get("matches", []) if not o.get("errorCode")]
    rep.check(
        "at least one crossed pair settled",
        receipt.get("settled", 0) > 0,
        f"settled {receipt.get('settled')} of {len(receipt.get('matches', []))}",
    )

    # Attribute every settled leg to the party that owns the order. The matcher
    # clears the whole crossed book, which includes the levels section 2 left
    # resting, so the expected balance move is a sum over legs, not one fill.
    owner_of = {cid: owner for owner, cid in cids}
    ours = [o for o in settled if o.get("buyCid") in owner_of or o.get("sellCid") in owner_of]
    rep.check("our own orders are among the settled matches", bool(ours), f"{len(ours)} of {len(settled)}")

    expected: dict[str, dict[str, Decimal]] = {
        p: {BASE_PAIR[0]: Decimal(0), BASE_PAIR[1]: Decimal(0)} for p in (maker, taker)
    }
    for o in ours:
        qty, price = Decimal(str(o["quantity"])), Decimal(str(o["price"]))
        buyer, seller = owner_of.get(o.get("buyCid", "")), owner_of.get(o.get("sellCid", ""))
        print(f"  leg: {qty} dBTC at {price} = {qty * price} dUSD  buyer={'ours' if buyer else 'other'} seller={'ours' if seller else 'other'}")
        if buyer in expected:
            expected[buyer][BASE_PAIR[0]] += qty
            expected[buyer][BASE_PAIR[1]] -= qty * price
        if seller in expected:
            expected[seller][BASE_PAIR[0]] -= qty
            expected[seller][BASE_PAIR[1]] += qty * price

    await asyncio.sleep(4)
    after_maker = await balances(client, base, maker)
    after_taker = await balances(client, base, taker)
    show("maker after the match", after_maker)
    show("taker after the match", after_taker)

    for label, party, before_bal, after_bal in (
        ("buyer", maker, before_maker, after_maker),
        ("seller", taker, before_taker, after_taker),
    ):
        for instrument in BASE_PAIR:
            moved = after_bal[instrument]["total"] - before_bal[instrument]["total"]
            rep.check(
                f"the {label}'s {instrument} moved by exactly the settled legs",
                moved == expected[party][instrument],
                f"{moved} vs {expected[party][instrument]}",
            )

    for o in ours:
        for owner, cid, remainder in (
            (owner_of.get(o.get("buyCid", "")), o.get("buyCid"), o.get("buyRemainderCid")),
            (owner_of.get(o.get("sellCid", "")), o.get("sellCid"), o.get("sellRemainderCid")),
        ):
            if owner is None:
                continue
            if (owner, cid) in cids:
                cids.remove((owner, cid))
            if remainder:
                cids.append((owner, remainder))
                owner_of[remainder] = owner

    trades = (await client.get(f"{base}/v1/trades", params={"trader": maker})).json()
    rep.check("the fill is visible to the buyer under ?trader=", isinstance(trades, list) and bool(trades), f"{len(trades) if isinstance(trades, list) else trades} rows")

    # A matcher run that clears everything it can leaves an uncrossed book.
    # Anything still crossing is a level the public book publishes and the
    # matcher will not fill, which is what a market-data client quotes off.
    book = (await client.get(f"{base}/v1/orders/book", params={"pair": "dBTC/dUSD"})).json()
    left = (await client.get(f"{base}/v1/orders/matches", params={"pair": "dBTC/dUSD"})).json()
    best_bid = max((Decimal(lvl["price"]) for lvl in book["bids"]), default=None)
    best_ask = min((Decimal(lvl["price"]) for lvl in book["asks"]), default=None)
    rep.check(
        "the book is uncrossed once the matcher has run",
        best_bid is None or best_ask is None or best_bid < best_ask,
        f"best bid {best_bid}, best ask {best_ask}, matcher now sees {len(left.get('matches', []))} pairs",
    )

    return cids


async def amm(client: httpx.AsyncClient, base: str, party: str, rep: Report) -> str:
    head("4. AMM SWAP THROUGH THE ADAPTER, WITH QUOTES RESTING")

    # Baseline read here, not carried in: every order placed since the quoting
    # section moved `locked`, and the point of the check is what the swap does.
    resting = await balances(client, base, party)
    show("before the swap", resting)

    adapter = DexRefAdapter(base_url=base, trader_party=party, swap_route=HostedPartySwapRoute())
    async with adapter:
        pools = await adapter.pools()
        pool = pools[0]
        admin = pool.token_a.admin
        base_i = Instrument(admin=admin, id=BASE_PAIR[0])
        quote_i = Instrument(admin=admin, id=BASE_PAIR[1])
        sell = Decimal("0.005")
        q = await adapter.quote(sell, base_i, quote_i)
        print(f"  quote  : {sell} dBTC -> {q.returned_amount} dUSD at {q.trade_price}")
        result = await adapter.swap(sell, base_i, quote_i)
        settled_at = time.monotonic()
        print(f"  swapped: {result.input_amount} -> {result.output_amount}")

    after = await balances(client, base, party)
    show("after the swap", after)
    rep.check(
        "the swap left the resting quotes locked",
        after[BASE_PAIR[1]]["locked"] == resting[BASE_PAIR[1]]["locked"],
        str(after[BASE_PAIR[1]]["locked"]),
    )
    moved = after[BASE_PAIR[1]]["available"] - resting[BASE_PAIR[1]]["available"]
    rep.check("the ledger moved what the adapter returned", moved == result.output_amount, f"{moved}")

    row = None
    while time.monotonic() - settled_at < 60:
        rows = (await client.get(f"{base}/v1/swaps", params={"limit": 5})).json()
        candidate = max(rows, key=lambda r: r["id"])
        if Decimal(str(candidate["outputAmount"])) == result.output_amount:
            row = candidate
            break
        await asyncio.sleep(0.5)
    lag = time.monotonic() - settled_at

    if row is None:
        rep.check("the fill reaches /v1/swaps", False, f"not indexed within {lag:.1f}s")
    else:
        print(f"  feed id={row['id']} outputAmount={row['outputAmount']!r} after {lag:.1f}s")
        rep.check("the fill reaches /v1/swaps exactly", True, f"{lag:.1f}s behind settlement")

    return pool.raw["contractId"] if isinstance(getattr(pool, "raw", None), dict) else ""


async def rfq(client: httpx.AsyncClient, base: str, party: str, rep: Report) -> None:
    head("5. RFQ LIFECYCLE")

    r = await client.post(
        f"{base}/v1/testnet/rfq", json={"party": party, "pair": "dBTC/dUSD", "side": "RFQ_Buy", "size": "0.001"}
    )
    if r.status_code != 200:
        rep.check("RFQ created", False, f"{r.status_code} {r.text[:120]}")
        return
    body = r.json()
    quotes = body.get("quotes", [])
    rep.check("RFQ returns live dealer quotes", bool(quotes), f"{len(quotes)} quotes")
    if not quotes:
        return

    best = min(quotes, key=lambda q: Decimal(str(q.get("price", "9" * 12))))
    acc = await client.post(
        f"{base}/v1/testnet/rfq/accept",
        json={
            "party": party,
            "rfqCid": body.get("rfqCid", ""),
            "acceptedQuoteCid": best.get("quoteCid") or best.get("contractId", ""),
        },
    )
    if acc.status_code != 200:
        rep.check("RFQ accepted", False, f"{acc.status_code} {acc.text[:120]}")
        return

    accepted = acc.json()
    receipt = accepted.get("receipt", {})
    rep.check(
        "acceptance returns a best-execution receipt",
        bool(receipt.get("policyVersion")),
        f"rank {receipt.get('acceptedRank')} of {receipt.get('consideredCount')}, "
        f"policy {receipt.get('policyVersion')}",
    )

    mine = (await client.get(f"{base}/v1/trades", params={"trader": party})).json()
    row = next((t for t in mine if t.get("tradeCid") == accepted.get("tradeCid")), None)
    if row is None:
        rep.check("the trade is visible under ?trader=", False, f"{len(mine)} rows")
    else:
        rep.check("the buyer is labelled as the trader", row.get("trader") == party)
        rep.check("counterparty is populated", bool(row.get("counterparty")), str(row.get("counterparty"))[:30])

    r = await client.post(
        f"{base}/v1/testnet/rfq", json={"party": party, "pair": "dBTC/dUSD", "side": "RFQ_Sell", "size": "0.001"}
    )
    if r.status_code == 200:
        cid = r.json().get("rfqCid", "")
        first = await client.post(f"{base}/v1/testnet/rfq/cancel", json={"party": party, "rfqCid": cid})
        second = await client.post(f"{base}/v1/testnet/rfq/cancel", json={"party": party, "rfqCid": cid})
        rep.check("RFQ cancel works", first.status_code == 200, str(first.status_code))
        rep.check("cancelling twice is a clean 400", second.status_code == 400, str(second.status_code))


async def liquidity(client: httpx.AsyncClient, base: str, party: str, pool_cid: str, rep: Report) -> None:
    head("6. LIQUIDITY PROVISION")

    if not pool_cid:
        pool_cid = (await client.get(f"{base}/v1/pools")).json()[0]["contractId"]

    before = await balances(client, base, party)
    add = await client.post(
        f"{base}/v1/testnet/liquidity",
        json={
            "party": party,
            "poolCid": pool_cid,
            "action": "add",
            "baseAmount": "0.001",
            "quoteAmount": "200",
        },
    )
    print(f"  add -> {add.status_code} {add.text[:220]}")
    if not rep.check("add liquidity accepted", add.status_code == 200, str(add.status_code)):
        return

    after_add = await balances(client, base, party)
    show("after adding", after_add)
    lp_ids = [i for i in after_add if i.endswith("-LP")]
    if not lp_ids:
        rep.check("the LP token is credited as a holding", False, "no LP instrument in balances")
        return

    lp_id = lp_ids[0]
    lp_amount = after_add[lp_id]["available"]
    rep.check("the LP token is credited as a holding", lp_amount > 0, f"{lp_id} = {lp_amount}")

    spent_base = before[BASE_PAIR[0]]["total"] - after_add[BASE_PAIR[0]]["total"]
    spent_quote = before[BASE_PAIR[1]]["total"] - after_add[BASE_PAIR[1]]["total"]
    print(f"  deposit taken: {spent_base} dBTC, {spent_quote} dUSD")
    rep.check(
        "the off-ratio excess is refunded, not absorbed",
        spent_base <= Decimal("0.001") and spent_quote < Decimal("200"),
        f"asked 0.001 / 200, took {spent_base} / {spent_quote}",
    )
    body = add.json()
    rep.check(
        "the receipt reports the settled quote amount, not the request",
        Decimal(str(body.get("quoteAmount", "0"))) == spent_quote,
        f"receipt says {body.get('quoteAmount')}, ledger moved {spent_quote}",
    )
    rep.check(
        "the receipt reports the settled base amount",
        Decimal(str(body.get("baseAmount", "0"))) == spent_base,
        f"receipt says {body.get('baseAmount')}, ledger moved {spent_base}",
    )
    rep.check(
        "the receipt reports the off-ratio refund",
        Decimal(str(body.get("quoteRefunded", "-1"))) == Decimal("200") - spent_quote
        and Decimal(str(body.get("baseRefunded", "-1"))) == Decimal("0.001") - spent_base,
        f"base {body.get('baseRefunded')}, quote {body.get('quoteRefunded')}",
    )
    rep.check(
        "the receipt reports the LP amount actually minted",
        Decimal(str(body.get("lpAmount", "0"))) == lp_amount,
        f"receipt says {body.get('lpAmount')}, credited {lp_amount}",
    )

    rem = await client.post(
        f"{base}/v1/testnet/liquidity",
        json={"party": party, "poolCid": pool_cid, "action": "remove", "lpAmount": str(lp_amount)},
    )
    print(f"  remove -> {rem.status_code} {rem.text[:220]}")
    if not rep.check("remove liquidity accepted", rem.status_code == 200, str(rem.status_code)):
        return

    after_remove = await balances(client, base, party)
    show("after removing", after_remove)
    back_base = after_remove[BASE_PAIR[0]]["total"] - after_add[BASE_PAIR[0]]["total"]
    back_quote = after_remove[BASE_PAIR[1]]["total"] - after_add[BASE_PAIR[1]]["total"]
    print(f"  round trip: out {spent_base} / {spent_quote}, back {back_base} / {back_quote}")
    rep.check("the LP position is fully redeemed", after_remove.get(lp_id, {}).get("total", Decimal(0)) == 0)

    # The indexer classifies every pool rotation as swap, add_liquidity,
    # remove_liquidity or state_change. /v1/swaps used to select kind = 'swap'
    # unconditionally, so the two rotations just written were invisible over
    # HTTP; ?kind= now serves them.
    # The indexer lands a row a few seconds behind settlement, so poll for the
    # withdrawal rather than reading once and calling it missing.
    deadline = time.monotonic() + 30
    while True:
        removes = (await client.get(f"{base}/v1/swaps", params={"kind": "remove_liquidity"})).json()
        if any(Decimal(str(e.get("quoteDelta", "0"))) == -back_quote for e in removes):
            break
        if time.monotonic() > deadline:
            break
        await asyncio.sleep(1)
    adds = (await client.get(f"{base}/v1/swaps", params={"kind": "add_liquidity"})).json()
    rep.check(
        "add_liquidity events are readable over HTTP",
        bool(adds) and all(e.get("kind") == "add_liquidity" for e in adds),
        f"{len(adds)} rows",
    )
    rep.check(
        "remove_liquidity events are readable over HTTP",
        bool(removes) and all(e.get("kind") == "remove_liquidity" for e in removes),
        f"{len(removes)} rows",
    )
    rep.check(
        "the deposit just settled is in the feed",
        any(Decimal(str(e.get("quoteDelta", "0"))) == spent_quote for e in adds),
        f"looking for quoteDelta {spent_quote}",
    )
    rep.check(
        "the withdrawal just settled is in the feed",
        any(Decimal(str(e.get("quoteDelta", "0"))) == -back_quote for e in removes),
        f"looking for quoteDelta {-back_quote}",
    )
    bad = await client.get(f"{base}/v1/swaps", params={"kind": "nonsense"})
    rep.check(
        "an unknown kind is refused, not silently ignored",
        bad.status_code == 400,
        f"{bad.status_code} {bad.text[:90]}",
    )


async def cleanup(
    client: httpx.AsyncClient, base: str, party: str, orders: list[tuple[str, str]], rep: Report
) -> None:
    head("7. CANCEL AND RECONCILE")
    for owner, cid in orders:
        r = await client.post(f"{base}/v1/testnet/order/cancel", json={"party": owner, "orderCid": cid})
        # A settled order is gone from the book; refusing to cancel it is right.
        gone = r.status_code == 400 and "inactive" in r.text
        rep.check(f"cancel {cid[:12]}…", r.status_code == 200 or gone, str(r.status_code))

    final = await balances(client, base, party)
    show("final", final)
    rep.check("nothing left locked in the quote instrument", final[BASE_PAIR[1]]["locked"] == 0)
    rep.check("nothing left locked in the base instrument", final[BASE_PAIR[0]]["locked"] == 0)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.getenv("DEXREF_BASE_URL", DEFAULT_BASE))
    parser.add_argument("--execute", action="store_true", help="submit real testnet transactions")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    rep = Report()
    async with httpx.AsyncClient(timeout=180.0) as client:
        print(f"\nbase: {base}")
        await read_surface(client, base, rep)

        if not args.execute:
            head(f"READ-ONLY: {rep.passed} passed, {rep.failed} failed")
            print("  pass --execute to run the order, swap, RFQ and liquidity paths")
            return 1 if rep.failed else 0

        party_a = os.getenv("DEXREF_PARTY_A") or await allocate_party(client, base)
        party_b = os.getenv("DEXREF_PARTY_B") or await allocate_party(client, base)
        print(f"\nparty A: {party_a}")
        print(f"party B: {party_b}")

        placed = await quoting(client, base, party_a, rep)
        resting = [(party_a, cid) for _s, _p, _q, cid in placed]
        crossed = await crossing(client, base, party_a, party_b, resting, rep)
        pool_cid = await amm(client, base, party_a, rep)
        await rfq(client, base, party_a, rep)
        await liquidity(client, base, party_a, pool_cid, rep)
        await cleanup(client, base, party_a, crossed, rep)

        head(f"RESULT: {rep.passed} passed, {rep.failed} failed")
        for label, ok, detail in rep.rows:
            if not ok:
                print(f"  FAILED: {label}  [{detail}]")
    return 1 if rep.failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
