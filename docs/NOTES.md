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

## API findings (checked against official docs, 27 Oct 2026)

### Meta Model API: chat (dev.meta.ai/docs/protocols/chat-completions)
- `POST https://api.meta.ai/v1/chat/completions`, `Authorization: Bearer <MODEL_API_KEY>`. OpenAI-compatible, as CLAUDE.md said.
- Model IDs: `muse-spark-1.3` / `1.2` / `1.1` at $1.25 in and $4.25 out per 1M tokens. `-contributor` variants cost $0.10 / $0.20 but **let Meta train on your content**. That's Hosea's call; we default to the standard model.
- **Differences from OpenAI:** Muse Spark always reasons (`reasoning_effort: "none"` → 400). We send `reasoning_effort: "low"` (`META_REASONING_EFFORT`) to keep replies fast. These are rejected with 400: `stop`, `n>1`, `logit_bias`, `logprobs`, `frequency_penalty`, `presence_penalty`. We send none of them.
- Stricter validation: every assistant `tool_calls` message must be followed by matching `tool` messages, or you get a 400. Our loop always does this.
- The docs prefer the `developer` role over `system` but accept `system`. We use `system`, because Baseten understands it too.
- Tool-call arguments can be invalid JSON (the docs warn about this). We treat that as `{}`, which fails validation, so the model gets the error and retries.
- Rate limit: 3,000 requests per minute per team for muse-spark-1.3.

### Muse Voice Transcribe (dev.meta.ai/docs/speech-to-text)
- `POST https://api.meta.ai/v1/asr/transcribe`, Bearer auth, multipart with **two parts**:
  - `request`: a JSON part `{"model","mode","audioEncoding":"WAV","languageBias":[...],"keywords":[...]}`
  - `audio`: a WAV file
- **WAV only**: mono, 16-bit PCM, 16 or 24 kHz. Maximum 10 minutes or 32 MB. WhatsApp voice notes (OGG/Opus) are converted with ffmpeg first.
  - ffmpeg comes from the `imageio-ffmpeg` package (a bundled binary), so no Homebrew is needed. The system ffmpeg is used if one is installed (e.g. in Docker).
- `mode: "PUSH_TO_TALK"` suits one voice note. `languageBias` takes language **names** (`["English"]`), not codes.
- **Nigerian Pidgin is not a listed language.** We bias to English (Pidgin is English-based) and pass keywords: Pidgin words, units, common goods, and the trader's own customer names.
- Response: `{"transcript", "audioDurationMs", "turns", "sessionId"}`, not `text`.
- $0.18 per audio hour, billed in whole seconds rounded down. A 10-second voice note costs about $0.0005.

### Baseten (docs.baseten.co/development/model-apis/overview)
- `https://inference.baseten.co/v1/chat/completions`, `Authorization: Bearer <BASETEN_API_KEY>`, OpenAI-compatible, tool calling supported.
- The Meta open-weights model is **Muse Spark Open**. Its slug isn't in the docs page text: get it from the Baseten dashboard or `GET https://inference.baseten.co/v1/models`. (A third-party site says `muse-spark-open`; unverified.)
- The price isn't in the docs either. Add it to `usage.py` once known.

## Phase 2 decisions
- **One model call does the extraction.** The agent's tool call *is* the structured extraction (`record_sale(...)` with typed args), so a separate extraction call would only add latency. `EXTRACTION_PROVIDER` picks which model the evals measure; `LLM_PROVIDER` is what the bot uses.
- With `LLM_PROVIDER=meta` and Baseten configured, Baseten is an automatic **fallback** if the Meta API fails (e.g. the account restriction).
- Voice-note replies start with `🎤 "<transcript>"`, so traders can spot mis-hearings, and the demo shows Muse Voice Transcribe at work.
- Retries: network errors, 429 and 5xx are retried with exponential backoff and jitter, honouring `Retry-After`. Other 4xx errors fail at once.
- Eval baseline: the rule-based FakeProvider scores **28/36 (78%)**.
