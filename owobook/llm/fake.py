"""FakeProvider: a deterministic, rule-based stand-in for Muse Spark.

Used by tests and by the simulator before real API keys are set up. It understands
common English/Pidgin phrasings with regexes and emits the same tool calls the real
model would. It is deliberately simple — the real intelligence comes from Muse Spark.

Tests can also pass a script: FakeProvider(script=[LLMResponse(...), ...]).
"""

import json
import re
from itertools import count
from typing import Any

from owobook.llm.base import LLMResponse, ToolCall, Usage
from owobook.money import parse_amount
from owobook.vision import PHOTO_MARKER

_UNITS = (
    r"bags?|crates?|cartons?|tins?|pieces?|pcs|kg|kilos?|tubers?|baskets?|bottles?|packs?|"
    r"dozens?|paints?|sachets?|rolls?|yards?|bundles?|bunches?|litres?|liters?|gallons?|mudu|"
    r"derica|sacks?|boxes|box|plates?|bowls?"
)
_NUM_WORDS = (
    r"one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|"
    r"sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fourty|fifty|sixty|seventy|eighty|"
    r"ninety|hundred|and|a|half"
)
_AMOUNT_RE = re.compile(
    rf"(?:[₦#]\s?\d[\d,]*(?:\.\d+)?(?:\s*(?:k|m)\b)?"  # ₦45,000 / #45k
    rf"|\bn\d[\d,]*(?:\.\d+)?(?:\s*(?:k|m)\b)?"  # N45000
    rf"|\b\d[\d,]*(?:\.\d+)?\s*(?:k|m|thousand|million)\b(?:\s+naira)?"  # 45k / 45 thousand
    rf"|\b\d[\d,]*(?:\.\d+)?\s+naira\b"  # 500 naira
    rf"|\b(?:(?:{_NUM_WORDS})[\s-]+)+(?:thousand|million|k)\b(?:\s+naira)?"  # fifteen thousand
    rf"|\b\d{{1,3}}(?:,\d{{3}})+\b|\b\d{{4,}}\b)",  # 45,000 / 45000
    re.I,
)
_QTY_RE = re.compile(rf"\b(\d+(?:\.\d+)?)\s+({_UNITS})\b(?:\s+(?:of\s+)?([a-z][a-z ]*?))?(?=\s*(?:[,.]|\bfor\b|\bat\b|\bto\b|\bshe\b|\bhe\b|\band\b|\bcash\b|\bgo\b|\bwill\b|\bon\b|\d|[₦#]|$))", re.I)
_DUE_RE = re.compile(
    r"(next tomorrow|day after tomorrow|tomorrow|tmrw|today|tonight|end of (?:the )?month|month end|next month|"
    r"next week|(?:in )?(?:\d+|a|one|two|three|four|five|six|seven) (?:days?|weeks?)(?: time)?|"
    r"(?:next )?(?:mon|tues|wednes|thurs|fri|satur|sun)day(?: next week)?|weekend|"
    r"\d{1,2}/\d{1,2}(?:/\d{2,4})?|\d{1,2}(?:st|nd|rd|th)? (?:of )?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*)",
    re.I,
)
_CREDIT_CUES = re.compile(
    r"\b(collect(?:ed)?|carr(?:y|ied)|took|take|owe|owing|go pay|will pay|go balance|credit|"
    r"she go|he go|dem go|pay (?:me )?(?:on|by|next|tomorrow|later)|never pay|neva pay)\b",
    re.I,
)
_TAKE_VERBS = r"collect(?:ed)?|carr(?:y|ied)|took|take|buy|bought|get|got"
_FILLER_PREFIX = re.compile(r"^(?:abeg|so|ehen|oya|okay|ok|today|my customer|customer|and|then|i sell to|sold to|i sold to)\s+", re.I)


def _clean_name(raw: str) -> str | None:
    name = raw.strip(" ,.!?")
    for _ in range(3):
        name = _FILLER_PREFIX.sub("", name).strip(" ,.!?")
    if not name or len(name) > 40 or re.search(r"\d", name):
        return None
    if name.lower() in {"i", "she", "he", "dem", "they", "we", "you", "e", "somebody", "person"}:
        return None
    return name


def _known_customer_in(text: str, known: list[str]) -> str | None:
    low = f" {text.lower()} "
    for name in sorted(known, key=len, reverse=True):
        if f" {name.lower()} " in low:
            return name
    return None


class FakeProvider:
    name = "fake"
    model = "fake-rules-1"

    def __init__(self, script: list[LLMResponse] | None = None):
        self.script = list(script) if script is not None else None
        self.calls: list[list[dict[str, Any]]] = []
        self._ids = count(1)

    async def chat(self, messages, tools=None, tool_choice="auto") -> LLMResponse:
        self.calls.append(messages)
        if self.script is not None:
            if not self.script:
                return LLMResponse(content="", provider=self.name, model=self.model)
            resp = self.script.pop(0)
        else:
            resp = self._respond(messages)
        resp.provider, resp.model = self.name, self.model
        chars = sum(len(str(m.get("content") or "")) for m in messages)
        resp.usage = Usage(input_tokens=chars // 4, output_tokens=20)
        return resp

    # ---- rule-based brain ------------------------------------------------------------

    def _call(self, tool: str, **args) -> LLMResponse:
        args = {k: v for k, v in args.items() if v is not None}
        return LLMResponse(content=None, tool_calls=[ToolCall(f"call_{next(self._ids)}", tool, args)])

    def _respond(self, messages: list[dict[str, Any]]) -> LLMResponse:
        last = messages[-1]
        if isinstance(last.get("content"), list):  # a photo: no eyes, so echo the caption's line (if any)
            note = next((p["text"] for p in last["content"] if p.get("type") == "text"), "")
            caption = re.search(r'caption: "(.*)"', note, re.S)
            return LLMResponse(content=caption[1] if caption else "NOT_A_RECEIPT")
        if last["role"] == "tool":
            # After a tool error, relay the tool's suggested question; otherwise nothing to add.
            try:
                data = json.loads(last["content"])
            except (TypeError, ValueError):
                data = {}
            return LLMResponse(content=data.get("question") or data.get("error") or "")

        context = "\n".join(m["content"] for m in messages if m["role"] == "system")
        known = []
        if m := re.search(r"^KNOWN_CUSTOMERS: (.*)$", context, re.M):
            known = [n.strip() for n in m[1].split("|") if n.strip()]
        editing = re.search(r"^EDITING_ENTRY_ID: (\d+)$", context, re.M)
        text = last["content"].removeprefix(PHOTO_MARKER).strip()  # photo lines: handled like text
        low = text.lower()

        amounts = [(mm.start(), mm.end(), mm.group(0)) for mm in _AMOUNT_RE.finditer(text) if parse_amount(mm.group(0))]
        qty = _QTY_RE.search(text)
        due = _DUE_RE.search(text)

        if editing:
            args: dict[str, Any] = {"entry_id": int(editing[1])}
            if amounts:
                args["amount_naira"] = amounts[-1][2]
            if qty:
                args["quantity"] = float(qty[1])
                args["unit"] = qty[2]
            if due:
                args["due_date"] = due[1]
            if name := _known_customer_in(text, known):
                args["customer_name"] = name
            if len(args) == 1:
                return self._call("ask_clarification", question="Wetin I go change? Talk the correct amount or quantity.")
            return self._call("update_entry", **args)

        # Customer number: "Tutu number na 0803 123 4567", "save Mama Bisi number 0803…", shared contact card
        phone_m = re.search(r"(\+?\d[\d\s-]{8,}\d)", text)
        if phone_m and re.search(r"\b(number|phone|line|contact card)\b", low):
            before = text[:phone_m.start()]
            before = re.sub(r"^.*contact card\)?:\s*", "", before, flags=re.I)
            before = re.sub(r"^\s*(?:abeg\s+)?(?:save|add)\s+", "", before, flags=re.I)
            before = re.sub(r"(?:'s)?\s*\b(?:phone\s+)?(?:number|phone|line)\b.*$|[,:]\s*$", "", before, flags=re.I)
            name = _known_customer_in(before, known) or _clean_name(before)
            if not name:
                return self._call("ask_clarification", question="Na whose number be this?")
            return self._call("save_customer_phone", customer_name=name, phone=phone_m[1].strip())

        # Profile: "my name is Hosea, my shop is Hosea Provisions"
        name_m = re.search(r"\b(?:my name is|my name na|i be|i am|i'm|call me)\s+([a-z][a-z .']*?)(?=[,.]|\s+and\b|\s+my\b|$)", low)
        shop_m = re.search(r"\b(?:my shop(?: name)? (?:is|na)|shop name (?:is|na)|i get shop wey dem dey call)\s+([^,.]+)", text, re.I)
        if (name_m or shop_m) and not amounts:
            return self._call(
                "save_profile",
                name=name_m[1].strip().title() if name_m else None,
                shop_name=shop_m[1].strip() if shop_m else None,
            )

        # Questions
        if re.search(r"\bwho\b.*\b(owe|owing)\b|\bdebtors?\b|\bwho never pay\b|\bwho neva pay\b", low):
            overdue = bool(re.search(r"overdue|late|don pass|due", low))
            return self._call("list_debtors", overdue_only=overdue)
        if re.search(r"\bhow much\b.*\b(sell|sold|sales|make|made)\b|\bsales\b.*\b(today|week|month)\b", low):
            period = "today"
            for pattern, p in [
                (r"yesterday", "yesterday"), (r"last week", "last_week"), (r"this week|dis week|for week", "this_week"),
                (r"last month", "last_month"), (r"this month|dis month|for month", "this_month"),
            ]:
                if re.search(pattern, low):
                    period = p
                    break
            return self._call("get_sales_summary", period=period)
        if bal := re.search(r"\bhow much\s+(?:does\s+|did\s+)?(.+?)\s+(?:dey\s+|de\s+|still\s+)?(?:owe|remain|get to pay|balance)\b", low):
            name = _known_customer_in(text, known) or _clean_name(bal[1])
            if name:
                return self._call("get_customer_balance", customer_name=name)
        if bal := re.search(r"^(.+?)(?:'s)?\s+balance\??$", low):
            name = _known_customer_in(text, known) or _clean_name(bal[1])
            if name:
                return self._call("get_customer_balance", customer_name=name)

        # Expense: "I spend 2k for transport"
        if re.search(r"\b(spend|spent|expense|i pay for|paid for|i pay transport|transport fare)\b", low) and amounts:
            start, end, amt = amounts[0]
            desc = re.sub(r"\b(i|spend|spent|expense|paid|pay|for|on|today|abeg)\b", " ", (text[:start] + text[end:]).lower())
            desc = re.sub(r"\s+", " ", desc).strip(" ,.") or "expense"
            return self._call("record_expense", description=desc, amount_naira=amt)

        # Payment: "Mama Bisi don pay 20k"
        pay = re.search(r"^(.*?)\s*\b(?:don pay|has paid|have paid|paid|pay me|don settle|settled|bring|brought|sent|send)\b", text, re.I)
        if pay and not re.search(r"\b(go|will|go come)\s+pay\b", low):
            name = _known_customer_in(pay[1], known) or _clean_name(pay[1])
            if not amounts:
                return self._call("ask_clarification", question="How much dem pay?")
            if not name:
                return self._call("ask_clarification", question="Who pay the money?")
            return self._call("record_payment", customer_name=name, amount_naira=amounts[0][2])

        # Sale
        if amounts or qty:
            is_credit = bool(_CREDIT_CUES.search(low)) and not re.search(r"\bcash\b", low)
            name = _known_customer_in(text, known)
            if not name:
                if m := re.search(rf"^(.*?)\s+\b(?:{_TAKE_VERBS})\b", text, re.I):
                    name = _clean_name(m[1])
                if not name and (m := re.search(r"\bto\s+([a-z][a-z ]+?)(?=\s*(?:[,.]|\d|[₦#]|\bfor\b|\bat\b|\bcash\b|\bon\b|$))", text, re.I)):
                    name = _clean_name(m[1])
            if not amounts:
                return self._call("ask_clarification", question="How much e cost?")
            if is_credit and not name:
                return self._call("ask_clarification", question="Who collect am on credit?")

            args = {
                "customer_name": name,
                "paid": not is_credit,
                "due_date": due[1] if (due and is_credit) else None,
            }
            if qty:
                args["quantity"] = float(qty[1])
                args["unit"] = qty[2].lower()
                if qty[3]:
                    args["item_description"] = qty[3].strip()
            elif m := re.search(rf"\b(?:{_TAKE_VERBS}|sold|sell)\s+([a-z][a-z ]+?)(?=\s*(?:[,.]|\d|[₦#]|\bfor\b|\bat\b|\bto\b|$))", text, re.I):
                args["item_description"] = m[1].strip()

            start, end, amt = amounts[0]
            after = low[end:end + 15]
            if qty and re.match(r"\s*(each|per|a |one)", after):
                args["unit_price_naira"] = amt
            else:
                args["amount_naira"] = amt
            return self._call("record_sale", **args)

        if _CREDIT_CUES.search(low):  # "Tunde collect rice" — a sale with no amount
            return self._call("ask_clarification", question="How much e cost?")
        return LLMResponse(content="")
