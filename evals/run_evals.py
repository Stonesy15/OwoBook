"""Run the eval cases through the real extraction path and print accuracy.

Each case is one trader message. We build the exact prompt + context the live bot uses
(with a demo trader whose customers are seeded), make ONE model call, and check the tool
calls it chose against what we expect. Nothing is written to your real database.

    uv run python evals/run_evals.py                      # provider = EXTRACTION_PROVIDER from .env
    uv run python evals/run_evals.py --provider meta
    uv run python evals/run_evals.py --provider fake      # sanity-check the runner itself
    uv run python evals/run_evals.py --only credit-basic,two-sales -v
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from owobook import ledger  # noqa: E402
from owobook.agent.loop import build_messages  # noqa: E402
from owobook.agent.tools import TOOL_SPECS, AgentContext, ToolInputError, sale_amounts  # noqa: E402
from owobook.config import get_settings  # noqa: E402
from owobook.dates import resolve_due_date, today_local  # noqa: E402
from owobook.db import Base, make_engine  # noqa: E402
from owobook.llm import LLMError, get_provider  # noqa: E402
from owobook.usage import estimate_cost  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

CUSTOMERS = ["Mama Bisi", "Chidi", "Alhaji Musa", "Iya Tola", "Emeka", "Blessing"]
RECORDING_TOOLS = {"record_sale", "record_payment", "record_expense", "update_entry", "delete_entry"}
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"


def load_cases(only: set[str] | None) -> list[dict]:
    cases = [json.loads(line) for line in (ROOT / "evals" / "cases.jsonl").read_text().splitlines() if line.strip()]
    return [c for c in cases if not only or c["id"] in only]


def check_call(expected: dict, args: dict, today: date) -> list[str]:
    """Return a list of mismatches between one expected call and the model's arguments."""
    problems = []
    if "customer" in expected:
        got = ledger.normalize_name(args.get("customer_name") or "")
        if got != ledger.normalize_name(expected["customer"]):
            problems.append(f"customer {args.get('customer_name')!r} ≠ {expected['customer']!r}")
    if "paid" in expected and args.get("paid") != expected["paid"]:
        problems.append(f"paid {args.get('paid')} ≠ {expected['paid']}")
    if "amount" in expected or "paid_now" in expected:
        try:  # the same code path the bot uses: totals and part-payments are computed in code
            kobo, paid_now = sale_amounts(args.get("amount_naira"), args.get("unit_price_naira"), args.get("quantity"),
                                          args.get("amount_paid_naira"), args.get("balance_naira"))
        except ToolInputError:
            kobo, paid_now = None, 0
        if "amount" in expected and kobo != expected["amount"] * 100:
            shown = f"₦{kobo / 100:,.0f}" if kobo else "none"
            problems.append(f"amount {shown} ≠ ₦{expected['amount']:,}")
        if paid_now != expected.get("paid_now", 0) * 100:
            problems.append(f"paid_now ₦{paid_now / 100:,.0f} ≠ ₦{expected.get('paid_now', 0):,}")
    if "quantity" in expected:
        try:
            ok = float(args.get("quantity")) == float(expected["quantity"])
        except (TypeError, ValueError):
            ok = False
        if not ok:
            problems.append(f"quantity {args.get('quantity')} ≠ {expected['quantity']}")
    if "due" in expected:
        want = resolve_due_date(expected["due"], today)
        got_text = args.get("due_date")
        got = resolve_due_date(got_text, today) if got_text else None
        if got != want:
            problems.append(f"due {got_text!r}→{got} ≠ {want}")
    if "phone" in expected:
        got_phone = ledger.normalize_phone(str(args.get("phone") or ""))
        if got_phone != expected["phone"]:
            problems.append(f"phone {args.get('phone')!r}→{got_phone} ≠ {expected['phone']}")
    for key in ("period", "overdue_only"):
        if key in expected and args.get(key, False if key == "overdue_only" else None) != expected[key]:
            problems.append(f"{key} {args.get(key)!r} ≠ {expected[key]!r}")
    return problems


def score(case: dict, resp, today: date) -> list[str]:
    """Empty list = pass."""
    expected = case["expect"]
    calls = [c for c in resp.tool_calls if c.name != "save_profile"]
    # A clarifying question in plain text is as good as the ask_clarification tool.
    if expected and expected[0]["tool"] == "ask_clarification" and len(expected) == 1:
        if any(c.name == "ask_clarification" for c in calls) or (not calls and (resp.content or "").strip()):
            return []
        return [f"expected a question, got {[c.name for c in calls]}"]
    if not expected:
        bad = [c.name for c in calls if c.name in RECORDING_TOOLS]
        return [f"expected no recording, got {bad}"] if bad else []

    problems = []
    remaining = list(calls)
    for exp in expected:
        candidates = [c for c in remaining if c.name == exp["tool"]]
        if not candidates:
            problems.append(f"missing {exp['tool']} (got {[c.name for c in calls]})")
            continue
        # pick the candidate with the fewest mismatches
        best = min(candidates, key=lambda c: len(check_call(exp, c.arguments, today)))
        remaining.remove(best)
        problems += [f"{exp['tool']}: {p}" for p in check_call(exp, best.arguments, today)]
    extra = [c.name for c in remaining if c.name in RECORDING_TOOLS]
    if extra:
        problems.append(f"unexpected extra calls {extra}")
    return problems


async def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--provider", default=settings.extraction_provider, choices=["meta", "baseten", "fake"])
    parser.add_argument("--only", help="comma-separated case ids")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--verbose", "-v", action="store_true", help="print the model's tool calls")
    args = parser.parse_args()

    try:
        provider = get_provider(args.provider)
    except LLMError as exc:
        sys.exit(f"{RED}{exc}{RESET}")
    cases = load_cases(set(args.only.split(",")) if args.only else None)

    # In-memory DB with a demo trader and their customers, so the context matches real use.
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = Session(engine)
    trader, _ = ledger.get_or_create_trader(db, "2348000000099")
    trader.name, trader.shop_name, trader.onboarded = "Hosea", "Hosea Provisions", True
    for name in CUSTOMERS:
        ledger.resolve_customer(db, trader, name, create=True)
    db.commit()
    today = today_local()

    print(f"{BOLD}OwoBook evals{RESET} — {len(cases)} cases, provider {provider.name} ({provider.model}), today {today}")
    sem = asyncio.Semaphore(args.concurrency)

    async def run(case):
        lang = "pidgin" if any(w in case["text"].lower() for w in ("dey", "don", "go pay", "collect", "abeg")) else "english"
        ctx = AgentContext(db=db, trader=trader, lang=lang, today=today, raw_text=case["text"])
        messages = build_messages(ctx, case["text"])
        async with sem:
            started = time.monotonic()
            try:
                resp = await provider.chat(messages, tools=TOOL_SPECS)
            except LLMError as exc:
                return case, None, [f"API error: {exc}"], time.monotonic() - started
        return case, resp, score(case, resp, today), time.monotonic() - started

    results = await asyncio.gather(*(run(c) for c in cases))

    passed, total_cost, latencies = 0, 0.0, []
    for case, resp, problems, secs in results:
        latencies.append(secs)
        if resp:
            total_cost += estimate_cost(resp.model, resp.usage.input_tokens, resp.usage.output_tokens)
        mark = f"{GREEN}✓{RESET}" if not problems else f"{RED}✗{RESET}"
        passed += not problems
        print(f"{mark} {case['id']:<24} {DIM}{secs:4.1f}s{RESET}  {case['text']}")
        for p in problems:
            print(f"      {RED}{p}{RESET}")
        if args.verbose and resp:
            for c in resp.tool_calls:
                print(f"      {DIM}{c.name} {json.dumps(c.arguments, ensure_ascii=False)}{RESET}")
            if resp.content:
                print(f"      {DIM}text: {resp.content[:200]!r}{RESET}")

    n = len(results)
    latencies.sort()
    print(f"\n{BOLD}Accuracy: {passed}/{n} = {passed / n:.0%}{RESET}")
    print(f"Latency: median {latencies[n // 2]:.1f}s, worst {latencies[-1]:.1f}s")
    print(f"Est. cost: ${total_cost:.4f} total, ${total_cost / n:.5f} per message (one model call each)")
    db.close()


if __name__ == "__main__":
    asyncio.run(main())
