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

## Eval results with real models (28 Oct 2026)
| Provider | Model | First run | After prompt fix (3 runs) | Median latency | Cost / message |
|---|---|---|---|---|---|
| Meta Model API | `muse-spark-1.3` | 34/36 (94%) | 36/36 every run | ~1.5s | ~$0.0046 |
| Baseten | `meta-models/muse-spark-open-1026` | 34/36 (94%) | 36/36 every run | ~1.7s | ~$0.0046 (same list price) |
| Fake (rules) | `fake-rules-1` | 28/36 (78%) | — | 0s | $0 |

- Misses on the first run: both models asked "12k each or 12k total?" for "5 crates of eggs 12k". Baseten also asked "cash or credit?" for a sale with no customer. The prompt now says: one amount after a quantity means the total unless the trader says "each"/"per", and a sale with no customer and no promise to pay later is cash.
- About 2,900 input tokens per call (system prompt + context + tool schemas) and about 130 output tokens (including hidden reasoning), so cost is dominated by input. Cached-input pricing ($0.15/M on Baseten) could cut it a lot later.
- Baseten price comes from baseten.co/pricing: Muse Spark Open costs $1.25 in / $4.25 out per 1M tokens, the same as the Meta API.

## Part-payments (28 Oct 2026)
- "He give me 60k, he go balance me 70k Friday" is ONE credit sale: `amount_kobo` = full price, `paid_now_kobo` = paid on the spot. Owing = amount − paid_now. One entry means one receipt, and Correct / Fix it / Undo act on the whole sale.
- The model passes the numbers as said (`amount_paid_naira` plus `amount_naira` or `balance_naira`), and `tools.sale_amounts` does the arithmetic. If the trader states all three and they don't add up, OwoBook asks.
- Money paid on the spot counts as "collected" in sales summaries. Paying everything on the spot turns the sale into a cash sale.
- Muse Spark often sends the string `"null"` for optional fields it doesn't use. `ToolArgs` drops those before validation. This also prevents creating a customer called "Null".
- Prompt rule: when the trader answers a clarifying question, record straight away; don't ask them to confirm.
- Evals: 39 cases (3 new part-payment cases). Both providers scored 39/39 on two runs. Muse Spark once recorded only one of the payments in "two-payments" (1 in about 6 runs).

## WhatsApp Cloud API (checked 28 Oct 2026)
- **Graph API v26.0** is current (released 29 Jul 2026). v21.0, as written in CLAUDE.md, is removed on 21 Jan 2027, so the default is now `v26.0`.
- Signature: `X-Hub-Signature-256: sha256=<HMAC-SHA256(raw body, app secret)>`. Verification GET: `hub.mode=subscribe`, `hub.verify_token`, and we reply with `hub.challenge`.
- Meta retries failed deliveries for up to 36 hours and may send duplicates, so we dedupe on the `wamid`. The dedupe check runs *before* downloading or transcribing audio, so a retry costs nothing.
- Audio webhook: `messages[].audio = {id, mime_type: "audio/ogg; codecs=opus", sha256, voice: bool, url?}`. `url` is being rolled out gradually, so we always use `GET /{media-id}` then `GET url`, both with the Bearer token. The URL expires after 5 minutes, and audio is capped at 16 MB.
- Reply buttons: ≤ 3 buttons, title ≤ 20 chars (unique), id ≤ 256, body ≤ 1024. Longer receipts go out as text followed by a short button message.
- Tapping a reply button sends `type: "interactive"` with `interactive.button_reply.id`. A template quick-reply sends `type: "button"` with `button.payload`. Both are handled.
- Read receipt plus typing indicator: `{"status": "read", "message_id", "typing_indicator": {"type": "text"}}`. Best effort; failures are ignored.
- Without `WA_APP_SECRET`, signatures are checked only when `APP_ENV=production`. The server logs a warning at startup.
- The database is committed *before* replies are sent, so a failed send never loses a record. Each trader has an asyncio lock, so their messages are processed in order.

## Reminders (Phase 4, 28 Oct 2026)
- APScheduler `AsyncIOScheduler` runs inside the web server: a cron job at `REMINDER_HOUR` (08:00), timezone Africa/Lagos, with `coalesce`, `max_instances=1` and a 1-hour `misfire_grace_time`. Turn it off with `ENABLE_SCHEDULER=false`.
- If the host sleeps (free tiers), an external cron can call `POST /tasks/reminders` with header `X-Task-Token: $TASK_TOKEN`. Without a token configured, the endpoint always returns 403.
- Who gets a reminder: customers with a balance whose earliest unpaid due date is today or earlier (FIFO, so a part-paid old debt still counts). Due-today debts come first, then the longest overdue. At most 5 forwardable drafts; the rest are summarised.
- The trader gets one heads-up, then one plain-text draft per customer, so each can be forwarded on its own. Drafts follow the trader's language (English or Pidgin) and name the shop and items.
- 24h window: `last_inbound_at` < 24h ago → free-form. Otherwise, if `WA_REMINDER_TEMPLATE` is set → send the template with 3 body params (trader name, number of customers, total). Otherwise log and skip, unlogged, so it goes out once a template exists. A template quick-reply containing "remind" sends the drafts.
- Once a day: sent reminders are logged in `messages_log` with type `reminder`, and the job skips traders already reminded since Lagos midnight. Use `--force` to resend.
- `scripts/run_reminders.py --dry-run` and the simulator's `/reminders` show the messages without sending anything.
- `scripts/seed_demo.py` builds a week relative to today: Mama Bisi due today (part-paid), Alhaji Musa and Iya Tola overdue, Chidi and Emeka due later (Emeka part-paid), Blessing cleared, 8 cash sales, 2 expenses.
