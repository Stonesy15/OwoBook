# OwoBook — Dev notes

Decisions and findings that aren't obvious from the code. API findings get added here as each client is built (CLAUDE.md §14).

## Environment
- macOS had only the system Python 3.9. We use **uv** (installed to `~/.local/bin`) with Python 3.12 pinned in `.python-version`.
  - `uv sync` installs everything into `.venv`; run things with `uv run ...`.
- ffmpeg is not installed yet — needed in Phase 2 only if Muse Voice Transcribe rejects WhatsApp's OGG/Opus.

## Database
- `DATABASE_URL` drives both the app and Alembic (`alembic/env.py` ignores `alembic.ini`'s URL).
- `postgres://` / `postgresql://` URLs (Supabase, Render) are rewritten to `postgresql+psycopg://` in `config.py`.
- SQLite migrations use Alembic batch mode.
- Enum-like columns (`type`, `status`, `source`, `mode`, `language_pref`) are plain strings backed by `StrEnum`s in `models.py`. That's simpler to migrate than native DB enums.

## Date rules (dates.py)
- "Friday" = next occurrence **strictly after** today (said on a Friday → next week's Friday).
- "next Friday" = the upcoming Friday (same as "Friday"). People in Lagos use it both ways; the receipt always shows the resolved date, and the trader can tap **Fix it**.
- "Friday next week" = Friday of the next calendar week (weeks start Monday).
- "next tomorrow" (Pidgin) = day after tomorrow.
- Numeric dates are **day-first** (5/11 = 5 November). A date given without a year that has already passed rolls to next year.
- Summary periods use Lagos midnights, converted to UTC for querying.

## Money rules (money.py)
- `parse_amount` refuses anything with unknown words ("3 bags rice" → None), so the agent asks instead of guessing.
- `#` and `N` prefixes are treated as naira (common Nigerian shorthand).

## Agent design (Phase 1)
- `agent/handler.py` is the single entry point for every inbound message (simulator now, WhatsApp webhook in Phase 3). It handles dedupe, onboarding, button taps, editing mode, logging, and the friendly error message.
- **Replies come from tool results, not model text.** After a tool succeeds, the loop stops without another model call. That saves a round trip (latency and cost) and guarantees that numbers come from the DB. The loop only continues when a tool returns an error the model must fix or ask about.
- Each agent run is wrapped in a DB savepoint: if anything crashes mid-way, half-written entries are rolled back and the trader gets "please try again".
- The model gets a short recent history (last 6 text messages, last 20 minutes) so it can follow up on a clarifying question. Ledger data never comes from history — always from tools.
- Close-but-different customer names ("Iya Bisi" vs "Mama Bisi", typos) make the tool refuse with a question. `new_customer=true` is set only after the trader confirms.
- A payment from a name not in the book is refused (asks who), so typos don't create phantom customers.
- Fix it → editing mode lasts for exactly one message, then returns to normal.
- If a correction changes only the quantity, the total is recomputed from the saved price per unit ("no, na 3 cartons" at ₦6,500 each → ₦19,500).
- A first message that already contains a number is processed as well as getting the onboarding reply, so it isn't lost.
- `FakeProvider` is a regex stand-in for Muse Spark, good enough for the simulator and tests. It is not the product's intelligence.
- Usage is logged by the agent loop for every model call (all providers), so cost reporting works the same everywhere.
