# OwoBook — Project Brief for Claude Code

> This file is the single source of truth for the OwoBook project. Read it fully before writing any code.
> The project name is **OwoBook**. All code lives in this `OwoBook/` folder.

---

## 1. Context

OwoBook is being built for the **Meta Global AI Developer Hackathon** (10-day virtual hackathon by Meta + Cerebral Valley).

- **Build window:** 26 Oct – 4 Nov 2026. **Submission deadline: 4 Nov 2026, 9:00 PM ET** (5 Nov, 3:00 AM Lagos time).
- **Track:** Indie developer — judged on **creativity, technical ambition, and whether it works**.
- **Theme:** "Build for Your World".
- **"Use of Meta AI Tooling" is a scored criterion**, checked in Stage One judging. Meta models must power the core of the product. Non-Meta models must NOT be used for any core feature.
- The developer (Hosea) is based in **Lagos, Nigeria**. Timezone: **Africa/Lagos (UTC+1)**.

### Organizers' "anti-project" list — OwoBook must not resemble these
Basic RAG apps, Streamlit apps, dashboards as the main feature, simple image analyzers, AI-for-education chatbots, AI medical advice, etc.
Consequences for this project:
- **WhatsApp is the product.** Any web dashboard is optional, secondary, and built last.
- **Never use Streamlit.**
- Ledger questions ("who owes me money?") are answered by **querying the database through tool calls**, never by stuffing chat history into the model.
- Receipt photo reading is a **side feature** that feeds the ledger, not the headline.

---

## 2. Product overview

**One-liner:** OwoBook turns WhatsApp voice notes in Pidgin and English into a ledger for market traders, so they never lose track of who owes them.

**Problem:** Lagos market traders sell on credit daily ("pay me Friday") and track it in their heads, on paper, or in WhatsApp chats. They lose money to forgotten debts and can't see if they're profitable. Bookkeeping apps expect downloads, forms, typing and English.

**Solution:** A bookkeeper that lives inside WhatsApp. No app, no typing. The trader just talks.

### MVP features (keep scope tight — these three must work reliably)
1. **Log by voice (or text):** "Mama Bisi collect 3 bags rice, she go pay 45k Friday" → recorded as a credit sale / debt.
2. **Ask questions:** "Who owe me money?", "How much I sell this week?", "How much Mama Bisi owe?"
3. **Debt reminders:** On the due date, OwoBook messages the trader and drafts a polite reminder the trader can forward to the customer.

### Secondary features (only after MVP is solid)
- Receipt/invoice photo → ledger entry (Muse Spark vision).
- Yoruba support (stretch; Pidgin + English first).
- Minimal read-only web page showing a trader's ledger (for the demo video only; NOT Streamlit).
- Payment link in reminders (Paystack/Flutterwave test mode) — business-story demo only.

---

## 3. User experience (WhatsApp conversation design)

The trader only ever sees WhatsApp. They message **one OwoBook WhatsApp number**. Each trader is identified by their WhatsApp phone number (`wa_id`) and has a private ledger.

### Every write action gets a confirmation receipt with buttons
```
✅ Recorded

👤 Mama Bisi
🛒 3 bags rice
💰 ₦45,000 — owing
📅 Due: Fri, 30 Oct

Mama Bisi now owes you ₦45,000 in total.

[ Correct ✓ ]   [ Fix it ✏️ ]   [ Undo ↩️ ]
```
- Use WhatsApp **interactive reply buttons** (max 3 buttons; button titles ≤ 20 chars).
- **Correct** → mark entry `confirmed`.
- **Fix it** → put the trader's session into "editing entry X" mode; the next voice/text message ("no, na 4 bags, 60k") updates that entry, then send a new receipt.
- **Undo** → soft-delete the entry and confirm.

### Example interactions
- "Mama Bisi don pay 20k" → `✅ Payment recorded. Mama Bisi now owes you ₦25,000.`
- "Who owe me money?" → list of customers with balances, due dates, overdue flags, sorted by amount.
- "How much I sell today / this week?" → totals for cash sales + credit sales, and amount collected.
- "Sold 5 crates of eggs 12k cash" → cash sale, no debt.
- Ambiguous input (no amount, unclear customer) → OwoBook asks ONE short clarifying question instead of guessing.
- First message from a new number → short friendly onboarding: what OwoBook does + an example voice note to try. Ask for the trader's name/shop name once.

### Tone
Friendly, short, respectful. Never lecture. Never use long paragraphs.
**Reply language (changed 29 Oct 2026, Hosea's decision):** OwoBook replies in the trader's *chosen* language, default **English** — a Pidgin voice note gets an English reply. The trader can switch with "reply me in Pidgin / Yoruba / Hausa / Igbo" (`save_profile.language_pref`). Fixed texts live in `owobook/whatsapp/i18n.py`. Voice notes must be English/Pidgin (Muse Voice Transcribe doesn't list Yorùbá, Hausa or Igbo); text works in all five.

### Debt reminders
- A scheduled job runs daily at **08:00 Africa/Lagos**.
- For debts due today or overdue: message the **trader** (not the customer): "Mama Bisi's ₦45,000 is due today. Here's a reminder you can forward 👇" followed by a separate, polite, ready-to-forward message.
- OwoBook never messages the trader's customers directly.
- WhatsApp's **24-hour customer service window** applies: free-form messages can only be sent within 24h of the trader's last inbound message. Outside the window, a pre-approved **template message** is required. Implement: if inside the window → send free-form; if outside → send via a configurable template name if `WA_REMINDER_TEMPLATE` is set, else log and skip. Do not block on templates for the MVP.

---

## 4. Architecture

```
Trader (WhatsApp)
     │  voice note / text / image / button tap
     ▼
WhatsApp Cloud API ──webhook──▶ FastAPI server (OwoBook)
                                   │
                                   ├─ 1. Verify signature, ack 200 immediately, dedupe by message id
                                   ├─ 2. Download media (Graph API)
                                   ├─ 3. Transcribe audio  ──▶ Muse Voice Transcribe (Meta Model API)
                                   ├─ 4. Agent loop with tools ──▶ Muse Spark (Meta Model API)
                                   │        └─ optional: fast structured extraction ──▶ Meta open-weights model on Baseten
                                   ├─ 5. Tools read/write ──▶ Database (SQLite local / Supabase Postgres)
                                   └─ 6. Reply ──▶ WhatsApp Cloud API (text / interactive buttons)

Scheduler (APScheduler, 08:00 Africa/Lagos) ──▶ due/overdue debts ──▶ reminder to trader
```

### Meta tooling used (must be visible in README and demo)
| Component | Role |
|---|---|
| **Muse Voice Transcribe** (`muse-voice-transcribe-1.0`) | Voice notes → text |
| **Muse Spark** (Meta Model API) | Agent reasoning, tool calling, answering questions, vision for receipts |
| **Meta open-weights model on Baseten** | High-volume, low-cost structured extraction (and backup provider) |
| **WhatsApp Cloud API** | The entire user interface |

---

## 5. Tech stack

- **Python 3.11+**
- **FastAPI** + **uvicorn** — webhook server
- **httpx** — async HTTP client for all external APIs
- **SQLAlchemy 2.x** with `DATABASE_URL` — SQLite for local dev, Supabase Postgres for deployment (same code)
- **Alembic** — migrations (or `create_all` for MVP if time is short; prefer Alembic)
- **APScheduler** — daily reminders
- **pydantic / pydantic-settings** — config and tool schemas
- **ffmpeg** — audio conversion if the transcription endpoint doesn't accept WhatsApp's OGG/Opus directly
- **pytest** + **respx** (httpx mocking) — tests
- **ngrok** — expose local server during development
- **Render or Railway** — deployment (provide `Dockerfile` + `render.yaml`)

Do NOT use: Streamlit, LangChain-style heavy frameworks (write a small, readable agent loop instead), any non-Meta LLM for core features.

---

## 6. External APIs — what we know (VERIFY against current official docs before coding)

> APIs change. Before implementing each client, check the current official docs (dev.meta.ai docs, Meta WhatsApp Cloud API docs, Baseten docs). If something here conflicts with the docs, the docs win — note the difference in `docs/NOTES.md`.

### Meta Model API
- Base URL: `https://api.meta.ai/v1`, auth: `Authorization: Bearer <MODEL_API_KEY>`. Keys start with `LLM|`.
- OpenAI-compatible endpoints (chat completions / Responses API); also exposes Anthropic Messages format at `/v1/messages`. Use the **OpenAI-compatible chat completions with tool/function calling** unless docs recommend otherwise.
- Model id for chat: make configurable via `META_CHAT_MODEL` (e.g. `muse-spark-1.3`; check the current list via `GET /v1/models`).
- Tool/function calling is supported (see Meta cookbook "tool and function calling").
- **Muse Voice Transcribe:** model `muse-voice-transcribe-1.0`. For recorded files: `POST https://api.meta.ai/v1/asr/transcribe` (multipart upload, Bearer auth). Supports keyword/contextual biasing — use it to bias toward Pidgin words, "naira", "k" amounts, and the trader's known customer names. Check accepted audio formats; convert OGG/Opus → WAV/PCM with ffmpeg if needed.
- ⚠️ The developer's Model API account may be temporarily restricted (billing card issue). The code must therefore support switching the LLM provider via config (see §8).

### Baseten (Meta open-weights models)
- Hackathon participants get Baseten credits for Meta's open-weights models.
- Baseten Model APIs are expected to be OpenAI-compatible (verify base URL and model slug in Baseten docs). Configure via `BASETEN_API_KEY`, `BASETEN_BASE_URL`, `BASETEN_MODEL`.
- Use it for: (a) structured extraction of ledger entries from transcripts, (b) full fallback provider if Meta Model API is unavailable.

### WhatsApp Cloud API (Graph API)
- Webhook verification: `GET /webhook` with `hub.mode`, `hub.verify_token`, `hub.challenge` → return challenge if token matches `WA_VERIFY_TOKEN`.
- Inbound: `POST /webhook`. Verify `X-Hub-Signature-256` (HMAC-SHA256 of raw body with `WA_APP_SECRET`). Return 200 fast; process in a background task. Meta retries — **dedupe by message id**.
- Send messages: `POST https://graph.facebook.com/{WA_GRAPH_VERSION}/{WA_PHONE_NUMBER_ID}/messages` with Bearer `WA_ACCESS_TOKEN`. Types used: `text`, `interactive` (`button`), optionally `template`.
- Media download: `GET /{media-id}` → returns a URL → `GET` that URL with the same Bearer token.
- Inbound types to handle: `text`, `audio` (voice notes), `image`, `interactive.button_reply`. Gracefully reply to unsupported types ("I fit understand voice note, text and receipt picture for now 🙏").
- Development uses Meta's **test phone number**; recipients must be added to the allowed list in the Meta app dashboard.

---

## 7. Data model

All money stored as **integer kobo** (₦1 = 100 kobo). Never floats. All timestamps stored in UTC; display in Africa/Lagos.

- **traders**: id, wa_id (unique), name, shop_name, language_pref (`pidgin|english|yoruba|auto`), created_at, last_inbound_at (for 24h window), onboarded (bool)
- **customers**: id, trader_id, display_name, normalized_name, phone (nullable), created_at. Unique per (trader_id, normalized_name). Fuzzy-match names ("Mama Bisi" vs "mama bisi" vs "Iya Bisi" → ask if unsure).
- **entries** (the ledger): id, trader_id, customer_id (nullable for cash sales to unknown buyers), type (`cash_sale|credit_sale|payment|expense`), item_description, quantity (nullable), unit (nullable), amount_kobo, due_date (nullable), status (`pending|confirmed|deleted`), source (`voice|text|image`), raw_text (transcript), created_at, updated_at
- **messages_log**: id, trader_id, wa_message_id (unique, for dedupe), direction, type, content, created_at
- **sessions**: trader_id, mode (`normal|editing`), editing_entry_id, updated_at
- **usage_log**: id, trader_id, provider, model, input_tokens, output_tokens, audio_seconds, est_cost_usd, created_at — used to report **cost per voice note** (important for the business story).

Customer balance = sum(credit_sale) − sum(payment) for confirmed+pending (non-deleted) entries.

---

## 8. LLM provider abstraction

Create `owobook/llm/` with a small interface:
```python
class LLMProvider(Protocol):
    async def chat(self, messages, tools=None, tool_choice="auto") -> LLMResponse: ...
```
Implementations: `MetaModelAPIProvider`, `BasetenProvider`, and `FakeProvider` (deterministic, for tests).
Select via env: `LLM_PROVIDER=meta|baseten|fake` and optional `EXTRACTION_PROVIDER=baseten|meta`.
All providers must log usage to `usage_log`.

Transcription likewise behind `owobook/transcribe/` with `MetaTranscriber` and `FakeTranscriber`.

---

## 9. Agent design

A small, readable agent loop (max ~5 tool-call iterations per message):

1. Build context: system prompt + trader profile + session mode + list of the trader's existing customer names (for name matching) + today's date in Africa/Lagos + the new message (transcript/text).
2. Call Muse Spark with tools. Execute tool calls. Feed results back. Repeat until a final answer.
3. The **final reply text is composed by our code from tool results** for write actions (deterministic receipt formatting), so amounts and names shown to the trader are exactly what was saved — never trust model-written numbers in receipts.

### Tools (JSON schemas via pydantic)
- `record_sale(customer_name?, item_description, quantity?, unit?, amount_naira, paid: bool, due_date?)`
- `record_payment(customer_name, amount_naira)`
- `record_expense(description, amount_naira)` (optional for MVP)
- `get_customer_balance(customer_name)`
- `list_debtors(overdue_only?: bool)`
- `get_sales_summary(period: today|yesterday|this_week|last_week|this_month)`
- `update_entry(entry_id, fields...)` (used in editing mode)
- `delete_entry(entry_id)`
- `ask_clarification(question)` — when required info is missing/ambiguous

### Parsing rules the system prompt must teach (and tests must cover)
- Amounts: "45k" = ₦45,000; "45 thousand"; "4.5k"; "1.2m"; "fifteen thousand naira"; "3 bags at 15k each" → total ₦45,000 (compute in code, not by the model, when quantity × unit price is given).
- Due dates: "Friday" → next occurrence in Africa/Lagos; "tomorrow"; "end of month"; "next week" → ask or default to +7 days and show it in the receipt.
- Pidgin: "collect" (took goods), "don pay" (has paid), "go pay" (will pay), "dey owe" (owes), "carry" (took), "balance" (remaining).
- If amount is missing → ask. If customer is missing for a credit sale → ask. Never invent numbers.

### System prompt
Store in `owobook/prompts/system.md`. Include: role, tone, language mirroring, the parsing rules above, "always use tools for anything involving ledger data", "ask one short question when unsure", "never give financial/legal advice".

---

## 10. Project structure

```
OwoBook/
├── CLAUDE.md                 # this file
├── README.md                 # setup, run, deploy, Meta tooling used, demo script
├── .env.example
├── .gitignore                # must include .env, *.db, __pycache__, .venv
├── pyproject.toml            # or requirements.txt
├── Dockerfile
├── render.yaml
├── alembic/ + alembic.ini
├── owobook/
│   ├── __init__.py
│   ├── main.py               # FastAPI app, routes, startup (scheduler)
│   ├── config.py             # pydantic-settings
│   ├── db.py                 # engine/session
│   ├── models.py             # SQLAlchemy models
│   ├── ledger.py             # pure business logic: record, balance, summaries (no I/O to LLM)
│   ├── money.py              # amount parsing helpers, kobo<->naira formatting (₦45,000)
│   ├── dates.py              # Lagos-time date resolution
│   ├── whatsapp/
│   │   ├── client.py         # send text/buttons/template, download media
│   │   ├── webhook.py        # verify, parse inbound payloads
│   │   └── formatting.py     # receipts, debtor lists, summaries
│   ├── transcribe/           # MetaTranscriber, FakeTranscriber, ffmpeg conversion
│   ├── llm/                  # providers (meta, baseten, fake)
│   ├── agent/
│   │   ├── loop.py
│   │   └── tools.py
│   ├── prompts/system.md
│   ├── reminders.py          # daily job
│   └── usage.py              # cost tracking
├── scripts/
│   ├── simulate.py           # CLI chat simulator (no WhatsApp needed): type messages or pass an audio file
│   └── seed_demo.py          # seed a demo trader with realistic customers/entries
├── evals/
│   ├── cases.jsonl           # 30+ Pidgin/English utterances → expected structured entries
│   └── run_evals.py          # runs cases through the real extraction path, prints accuracy
└── tests/
    ├── test_money.py
    ├── test_dates.py
    ├── test_ledger.py
    ├── test_webhook.py       # signature verification, dedupe, payload parsing
    └── test_agent.py         # with FakeProvider
```

---

## 11. Environment variables (`.env.example`)

```
# App
APP_ENV=development
TIMEZONE=Africa/Lagos
DATABASE_URL=sqlite:///./owobook.db
ALLOWED_WA_IDS=            # optional comma-separated allow-list for the demo

# LLM provider selection
LLM_PROVIDER=meta          # meta | baseten | fake
EXTRACTION_PROVIDER=baseten

# Meta Model API
MODEL_API_KEY=
META_API_BASE=https://api.meta.ai/v1
META_CHAT_MODEL=muse-spark-1.3
META_TRANSCRIBE_MODEL=muse-voice-transcribe-1.0

# Baseten (Meta open-weights models)
BASETEN_API_KEY=
BASETEN_BASE_URL=
BASETEN_MODEL=

# WhatsApp Cloud API
WA_ACCESS_TOKEN=
WA_PHONE_NUMBER_ID=
WA_APP_SECRET=
WA_VERIFY_TOKEN=
WA_GRAPH_VERSION=v21.0     # check current version in Meta docs
WA_REMINDER_TEMPLATE=      # optional approved template name
```

Never hard-code secrets. Never commit `.env`. Never print full keys in logs.

---

## 12. Build plan (work in phases; stop at each ⏸ checkpoint and tell Hosea what to test)

**Phase 0 — Skeleton**
Project structure, config, `.env.example`, `.gitignore`, DB models + migration, `money.py`, `dates.py` with full unit tests. `git init`.
⏸ Checkpoint: tests pass.

**Phase 1 — Ledger core + simulator (no external APIs)**
`ledger.py`, formatting, agent loop + tools with `FakeProvider`, `scripts/simulate.py`.
⏸ Checkpoint: Hosea can chat with OwoBook in the terminal using fake AI.

**Phase 2 — Real AI**
Meta Model API provider (and Baseten provider). Tool calling with Muse Spark. Transcription with Muse Voice Transcribe (simulator accepts an audio file path). Usage/cost logging. Build `evals/` and report accuracy.
⏸ Checkpoint: Hosea runs the simulator with real models and a recorded Pidgin voice note. If the Meta Model API account is still restricted, run with `LLM_PROVIDER=baseten` and tell Hosea.

**Phase 3 — WhatsApp**
Webhook (verify, signature, dedupe, background processing), media download, send text + interactive buttons, button handling (Correct / Fix it / Undo), onboarding flow, ngrok instructions in README.
⏸ Checkpoint: Hosea sends a real voice note to the test number and gets a receipt.

**Phase 4 — Reminders + polish**
APScheduler daily job, 24h window logic, reminder drafts, unsupported message types, error handling (any failure → friendly "Abeg try again" message + log), `seed_demo.py`.
⏸ Checkpoint: trigger reminder job manually and see the message.

**Phase 5 — Deploy + submission assets**
Dockerfile, `render.yaml`, deployment instructions, README (problem, solution, architecture diagram, Meta tooling table, setup, cost per voice note from `usage_log`, demo script).
⏸ Checkpoint: deployed URL works with WhatsApp webhook.

**Phase 6 (only if time remains)** — receipt photo → ledger, Yoruba, minimal read-only ledger web page (plain HTML, not Streamlit), Paystack test pay link.

---

## 13. Quality bar

- "Whether it works" is a judging criterion: **reliability beats feature count.**
- Every external call: timeouts, retries with backoff for 429/5xx, clear error logs.
- Webhook must always return 200 quickly; never let processing errors cause Meta retries/duplicates.
- Numbers shown to traders must come from the database, formatted as `₦45,000`.
- Write tests for money parsing, date resolution, balances, webhook signature, dedupe.
- Keep code simple and readable — Hosea needs to understand and demo it. Add short docstrings.
- Commit after each phase with a clear message.

## 14. Working agreement for Claude Code

- Ask Hosea before making choices that change scope or cost money.
- When you need a key, token or dashboard action from Hosea (Meta app setup, test numbers, ngrok, Supabase, Baseten), give him short numbered steps.
- Verify API details against current official docs before implementing clients; record findings in `docs/NOTES.md`.
- At each checkpoint, summarize: what was built, how to test it (exact commands), and what's next.
