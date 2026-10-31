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

## Deployment (Phase 5, checked 28 Oct 2026)
- **Render** free web service, Docker runtime, region `frankfurt` (closest to Lagos). Blueprint keys from render.com/docs/blueprint-spec: `autoDeployTrigger` replaces the deprecated `autoDeploy`; `sync: false` prompts once at Blueprint creation; `generateValue: true` makes a random 256-bit value.
- Render free: sleeps after 15 min without inbound HTTP, about 1 min to wake, 750 free instance-hours per month per workspace, ephemeral disk. So: Postgres for data, and cron-job.org pings `/health` every 10 min and calls `POST /tasks/reminders` at 08:00. `ENABLE_SCHEDULER=false` on Render, so the cron is the one trigger.
- **Database: Supabase**, not Render Postgres. Render's free Postgres is deleted 30 days after creation, which falls during judging. Supabase direct connections are IPv6-only; Render needs IPv4, so use the **session pooler** (port 5432, user `postgres.<project-ref>`), which also supports prepared statements. Avoid the transaction pooler (6543): psycopg prepares statements.
- Tested on a local PostgreSQL 16 (`pgserver`): Alembic migrations on an empty DB match the models (`alembic check`), all 210 tests pass with `TEST_DATABASE_URL` pointing at Postgres, and seed, reminders and simulator all work.
- Docker isn't installed on the dev Mac, so the image was not built locally. The same file set plus `uv sync --frozen --no-dev` was run in a scratch folder, and the server was started with the image's CMD in `APP_ENV=production` against Postgres: health, verify, unsigned POST → 401, cron without token → 403, with token → 202. The first real build happens on Render.
- ffmpeg comes from the `imageio-ffmpeg` wheel (manylinux x86_64/aarch64 builds are in `uv.lock`), so no apt packages.
- One uvicorn worker: the per-trader locks and the reminder lock live in the process. Two reminder triggers at once (scheduler + cron, or a cron retry) now run once; the second is skipped.
- **Permanent WhatsApp token**: a system-user token (Business Settings → System users, assign the app and the WABA, expiry "Never", scopes `whatsapp_business_messaging` + `whatsapp_business_management`). Source: third-party guides; Meta's own page wasn't fetched.
- **Linking the WABA to the app**: on the test number, the dashboard saved the webhook and the `messages` field, but the WABA's `subscribed_apps` listed only Meta's internal "WA DevX" app, so no messages arrived. `POST /{waba-id}/subscribed_apps` with the access token fixed it.
- Cost report: usage_log has no message id, so `usage.turns()` replays each trader's events in time order. A transcription row opens a voice-note turn (it is logged before the inbound audio message); later Muse Spark rows join the current turn.

## One-tap reminders (Phase 6, 29 Oct 2026)
- OwoBook still never messages customers. A business number can only start chats with people who opted in, and a debt message from an unknown number looks like a scam. Instead, a customer with a saved number gets a **link button** on their reminder that opens the trader's own chat with them, text prefilled: `https://wa.me/<digits>?text=<url-encoded draft>`.
- Link button = interactive `cta_url` message: `{"type": "cta_url", "body": {"text"}, "action": {"name": "cta_url", "parameters": {"display_text", "url"}}}`. Meta's docs: body ≤ 1024, display_text ≤ 20, header/footer ≤ 60, no stated URL length limit or scheme restriction. "Send to <name>" falls back to "Send to customer" past 20 chars.
- Not yet checked on a real phone: the WhatsApp token expired before this was built. To check after deploy: the button opens the customer's chat with the text, on Android and iPhone.
- Numbers are saved as WhatsApp wants them: digits with country code. `ledger.normalize_phone` handles Nigerian local (`0803…`, `803…`), `+234 0803…` and other `+` international numbers; incomplete numbers make OwoBook ask again.
- Shared contact cards (`type: "contacts"`) become a text message "Save this customer's number (contact card): Name, +234…" that goes through the agent, so name matching ("Tutu Charger" vs saved "Tutu") behaves as for spoken names. The card's `wa_id` is preferred over its formatted `phone`.
- The heads-up includes a one-line "tell me a customer's number" tip only while the trader has saved no numbers at all, so it doesn't repeat every morning once they know.
- Evals: 3 new cases (Pidgin, English, contact card); Muse Spark scored 42/42 on two runs. The extra tool schema raised the cost per call from about $0.0046 to $0.0054.

## Receipt and debt-book photos (Phase 6, 29 Oct 2026)
- Docs (dev.meta.ai/docs/image-understanding): Chat Completions takes `{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}` in **user** messages only. JPEG/PNG/GIF/WebP, ≤ 50 MB inline, ≤ 50 images per request. A ~1280px image costs about 1,300–1,500 input tokens. The Responses API is the recommended one, but Chat Completions works and keeps one client.
- Two steps, like a voice note: (1) one vision call with `prompts/photo.md` and no tools turns the photo into one line per entry ("Mama Bisi took 3 bags of rice for 45000, will pay Friday"), or `NOT_A_RECEIPT`; (2) the lines go to the normal agent as `[photo]\n<lines>`. So photos reuse every tool, receipt and test, and numbers still come from the database. The trader sees what was read above the first receipt.
- Unreadable numbers are written as `(amount unclear)`, and the agent asks about them after recording the rest. Caption beats photo. A photo with nothing to record but a caption ("Tunde collect 2 of these, 10k") is handled as the caption.
- The vision call is logged in `usage_log` with provider `<provider>:vision`, so `usage.turns()` counts photos as their own kind.
- With `LLM_PROVIDER=meta` and Baseten configured, a failed vision call falls back to Muse Spark Open on Baseten. It's untested whether that model accepts images; if not, the trader gets "please try again".
- Tested live on 29 Oct with generated images: a printed receipt → one part-paid sale (₦54,000, ₦20,000 paid, due Friday); a handwritten debt-book page → 2 sales, 1 payment, and a question about the `???` amount. Cost ≈ $0.011 and ≈ $0.026.

## Picture receipts (30 Oct 2026)

- "Send me receipt (for X)" → `make_receipt` tool → a PNG drawn by `owobook/receipt.py` from one ledger entry
  (the customer's latest sale or payment unless the model passes `entry_id`). Every number comes from the DB.
- Sale: item, total, amount paid, balance on this sale, due date, plus "Total balance owing" when the customer
  owes for other things too. Payment: balance before, amount paid, what's left and the next due date. Nothing
  left → a green PAID IN FULL box. Balances are **as of that entry** (`customer_balance(upto_entry_id=…)`), so
  an old receipt doesn't change when later payments come in.
- WhatsApp (docs checked 30 Oct 2026): `POST /{phone_number_id}/media` multipart (`messaging_product`, `type`,
  `file`) → `{"id"}`, valid 30 days; then `{"type": "image", "image": {"id", "caption"}}`, caption ≤ 1024.
  PNG/JPEG, 8-bit RGB(A), ≤ 5 MB (ours are ~50–60 KB).
- Font: DejaVu Sans (bundled in `owobook/assets/fonts`, its free license alongside), because it has ₦ and the
  Yorùbá/Hausa/Igbo letters (ẹ ọ ṣ ɗ ƙ ɓ ụ ị). Labels follow the trader's language; dates stay English.
- OwoBook still never messages customers: the trader forwards the picture from their own WhatsApp.
- Payment receipts and part-paid sale receipts end with a hint: say "receipt" for a picture receipt.

## Reply languages (29 Oct 2026)
- Hosea's call: replies are in the trader's **chosen** language, default English, no longer mirrored from the message (a Pidgin voice note gets English). `traders.language_pref` already existed; `auto` (the old default) now means English, so no migration.
- Choices: english, pidgin, yoruba, hausa, igbo. The trader switches by asking ("reply me in Yoruba"), which calls `save_profile(language_pref=...)`. The confirmation is sent in the new language.
- All fixed texts (receipts, lists, summaries, onboarding, reminders, customer drafts) are in `owobook/whatsapp/i18n.py`, 65 keys × 5 languages. A test checks every language has every key with the same `{placeholders}`. Muse Spark writes its own questions in REPLY_LANGUAGE from the context.
- The Yorùbá, Hausa and Igbo texts are my translations and need a native speaker's check before launch. Dates stay as "Fri, 30 Oct" in every language.
- **Voice notes in Yorùbá, Hausa or Igbo aren't supported:** Muse Voice Transcribe (dev.meta.ai/docs/speech-to-text, checked 29 Oct) lists 25 languages, none of them Nigerian. Pidgin works because it's English-based. The language-switch confirmation and onboarding say to speak English or Pidgin in voice notes. Text in those languages works: Muse Spark understood Yorùbá and Hausa sales and payments in a live test.
- Date words are only parsed in English/Pidgin, so the system prompt tells the model to translate "ọ̀la", "gobe", "echi" etc. first. Before that, "lola" cost one extra model call.

## Global: currency, auto language, trust score, business record (30 Oct 2026)

**Muse Voice Transcribe languages** (docs checked 30 Oct 2026): Arabic, Bengali, Dutch, English, French, German,
Hebrew, Hindi, Indonesian, Italian, Japanese, Kannada, Korean, Malay, Mandarin, Marathi, Polish, Portuguese,
Spanish, Tagalog, Tamil, Telugu, Thai, Turkish, Vietnamese. **No Swahili** (nor Yorùbá, Hausa, Igbo). The response
has **no detected-language field**; `languageBias` is a hint and omitting it lets the model detect the language.
- We picked Portuguese (Brazil) for the demo, and added French (CFA franc zones) and Hindi (India).
- Live test: a pt-BR voice note sent with the old fixed `languageBias: ["English"]` + Pidgin keywords came back as
  English nonsense ("…semovita four thousand"). So the bias now follows the trader: chosen language first, else
  the phone country's (+55 Portuguese, +225 French, +91 Hindi + English); Pidgin keywords only for +234; unknown
  countries get no bias. After the fix the same note transcribed correctly.

**Currency** (`currency.py`): from the WhatsApp number's calling code (longest prefix): NGN, GHS, KES, UGX, TZS, RWF,
ZAR, XOF/XAF, INR, BRL, AOA, MZN, MXN, PHP, USD, GBP; unknown → plain numbers. Stored on `traders.currency`
(migration `5b1f0c2a9d17`, existing traders NGN). The trader can change it ("my currency is reais" →
`save_profile.currency`). Formatting follows the trader being served via a context variable set per message and
per trader in the reminder job: `₦45,000`, `KSh 45,000`, `₹45,000`, `R$ 45.000`, `45 000 FCFA`. Amounts stay
integer minor units (the `*_kobo` names are historical). Parsing adds "lakh/crore", French "mille", and
"mil" = thousand for BRL/AOA/MZN/MXN traders (still "million" elsewhere). Tool fields renamed `amount_naira` →
`amount` (etc.); the old names still validate. Local customer numbers get the trader's own country code.

**Auto language** (`language.py`): only while `language_pref` is "auto" (never chosen). Devanagari → Hindi;
otherwise ≥ 2 marker words only Portuguese/French/Hinglish speakers use (or a lone "Olá"/"Bonjour"). English and
Pidgin → None, so Hosea's earlier decision (Pidgin voice note → English reply) still holds. Once detected, the
language is saved. Date words in pt/fr/hi (and ọ̀la/gobe/echi) are translated in `dates.py`. Dates are still
displayed in English ("Fri, 6 Nov"). Receipt buttons are translated (≤ 20 chars).
- Hindi in pictures/PDFs: Pillow here has no libraqm and fpdf2 no shaping, so Devanagari can't be drawn. Hindi
  traders get English labels on receipts/PDFs, the model is told to pass names in Latin letters, and
  `script.py` spells any remaining Devanagari in Latin ("रमेश" → "Ramesh"). Chat messages stay in Hindi.

**Trust score** (`ledger.payment_record`): replays a customer's credit sales and payments in order, paying off the
oldest debt first. A paid-off sale with a due date is on time (paid on/before the due date, Lagos dates) or late;
an unpaid one past its due date is overdue. New credit sales get a heads-up when there's anything overdue, or
≥ 2 late (or more late than on time); ≥ 3 on time and none late gets a 👍. `get_customer_balance` shows the
record. Facts only — no "don't give credit" advice.

**Business record** (`business_record.py`, fpdf2, A4 text PDF, English labels, trader's currency): trader
details, totals for 30/60/90 days (cash vs credit sales, money collected, expenses, sales minus expenses, active
days, average per trading day), credit owed now, % of customer debts paid by the due date (late + still overdue
count against), week-by-week table, every transaction (latest 300), and a plain note that figures are
self-reported and not audited. Sent as a WhatsApp document: `{"type": "document", "document": {"id",
"filename", "caption"}}`, PDF ≤ 100 MB (docs checked 30 Oct 2026).

## Private ledger page (31 Oct 2026)

- "Show my ledger" → tool `ledger_link` → `https://<server>/l/<token>`. The token is `<trader id>.<expiry>.<HMAC-SHA256, 16 bytes>` (base 36 numbers, base64url signature), signed with a key derived from `LINK_SECRET` or `WA_APP_SECRET`. Nothing is stored; links expire after 7 days. A bad, expired or unknown link gets the same 404 page.
- The server address comes from `PUBLIC_BASE_URL`, or `RENDER_EXTERNAL_URL`, which Render sets on every web service, so no dashboard change is needed. Without an address or secret the tool says the page isn't available.
- Read-only by design: the page never changes data; WhatsApp stays the only way to write. It sends `Cache-Control: no-store`, `Referrer-Policy: no-referrer` and `noindex`, and refreshes every 15 s for the demo.
- Limits: page labels are English only; a link can't be revoked before it expires (changing `LINK_SECRET` revokes all links).

