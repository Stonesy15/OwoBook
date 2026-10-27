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
