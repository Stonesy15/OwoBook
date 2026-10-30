"""The agent loop: give the model the trader's message plus context, run its tool calls,
and return the WhatsApp replies composed from the tool results."""

import json
import logging
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from owobook import ledger
from owobook.agent.tools import TOOL_SPECS, AgentContext, ToolResult, describe_entry, execute_tool
from owobook.dates import format_date, utcnow
from owobook.llm.base import LLMProvider
from owobook.models import MessageLog, SessionMode, Trader
from owobook.usage import log_usage
from owobook.whatsapp import formatting as fmt
from owobook.whatsapp.formatting import Reply

log = logging.getLogger(__name__)

MAX_STEPS = 5
HISTORY_MESSAGES = 6
HISTORY_WINDOW = timedelta(minutes=20)
SYSTEM_PROMPT = (Path(__file__).resolve().parent.parent / "prompts" / "system.md").read_text(encoding="utf-8")


def build_context(ctx: AgentContext) -> str:
    """Per-message facts for the model: who the trader is, today's date, customers, recent entries."""
    db, trader, today = ctx.db, ctx.trader, ctx.today
    lines = [
        f"TODAY: {format_date(today, today)} {today.year} ({today.isoformat()}), Africa/Lagos",
        f"TRADER: name={trader.name or 'unknown'}; shop={trader.shop_name or 'unknown'}",
        f"REPLY_LANGUAGE: {ctx.lang}",
    ]
    if not trader.name:
        lines.append("NOTE: We don't know the trader's name yet. If they say it, call save_profile.")
    customers = ledger.list_customers(db, trader)[:200]
    lines.append("KNOWN_CUSTOMERS: " + (" | ".join(c.display_name for c in customers) or "(none yet)"))
    recent = ledger.recent_entries(db, trader, limit=5)
    if recent:
        lines.append("RECENT_ENTRIES (newest first):")
        lines += [f"- {describe_entry(e, today)}" for e in recent]
    sess = ledger.get_session(db, trader)
    if sess.mode == SessionMode.EDITING and sess.editing_entry_id:
        entry = ledger.get_entry(db, trader, sess.editing_entry_id)
        lines += [
            f"EDITING_ENTRY_ID: {entry.id}",
            f"The trader tapped 'Fix it' on {describe_entry(entry, today)}. Their message is a correction: "
            f"call update_entry(entry_id={entry.id}, ...) with only the changed fields.",
        ]
    return "\n".join(lines)


def recent_history(db: Session, trader: Trader, before_id: int | None) -> list[dict[str, str]]:
    """The last few text messages of this conversation (for follow-ups to a clarifying question)."""
    query = (
        select(MessageLog)
        .where(
            MessageLog.trader_id == trader.id,
            MessageLog.created_at >= utcnow() - HISTORY_WINDOW,
            MessageLog.type.in_(["text", "audio", "image"]),
        )
        .order_by(MessageLog.id.desc())
        .limit(HISTORY_MESSAGES)
    )
    if before_id:
        query = query.where(MessageLog.id < before_id)
    rows = list(db.scalars(query))[::-1]
    return [{"role": "user" if r.direction == "in" else "assistant", "content": r.content or ""} for r in rows]


def _assistant_message(content: str | None, tool_calls) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": content or "",
        "tool_calls": [
            {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
            for tc in tool_calls
        ],
    }


def build_messages(ctx: AgentContext, text: str, history: list[dict[str, str]] | None = None) -> list[dict[str, Any]]:
    """The exact messages the model sees for one trader message (also used by evals)."""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": build_context(ctx)},
        *(history or []),
        {"role": "user", "content": text},
    ]


async def run_agent(
    ctx: AgentContext, provider: LLMProvider, text: str, history: list[dict[str, str]] | None = None
) -> list[Reply]:
    """Run up to MAX_STEPS model calls. Returns the replies to send."""
    messages = build_messages(ctx, text, history)
    replies: list[Reply] = []

    for step in range(MAX_STEPS):
        resp = await provider.chat(messages, tools=TOOL_SPECS)
        log_usage(
            ctx.db,
            trader_id=ctx.trader.id,
            provider=resp.provider or provider.name,
            model=resp.model or provider.model,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
        )
        if not resp.tool_calls:
            if resp.content and resp.content.strip():
                replies.append(Reply(resp.content.strip()))
            break

        messages.append(_assistant_message(resp.content, resp.tool_calls))
        results: list[ToolResult] = []
        for call in resp.tool_calls:
            log.info("tool %s %s", call.name, call.arguments)
            result = execute_tool(ctx, call.name, call.arguments)
            results.append(result)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result.data, default=str)})
            if result.reply and not result.is_error:
                replies.append(result.reply)

        # Our code already wrote the answer from the database — no need for another model call,
        # unless a tool failed and the model must fix it or ask the trader.
        if any(r.ends_turn for r in results) or not any(r.is_error for r in results):
            break
    else:
        log.warning("agent hit MAX_STEPS for trader %s", ctx.trader.id)

    if any(r.image for r in replies):  # the picture receipt is already here: drop the "say receipt" hint
        hint = "\n\n" + fmt.t(ctx.lang, "receipt_hint", name="\0").split("\0")[0]
        replies = [r if r.image else replace(r, text=r.text.split(hint)[0]) for r in replies]
    return replies or [Reply(fmt.t(ctx.lang, "fallback"))]
