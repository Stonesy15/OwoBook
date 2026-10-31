# OwoBook 📒

**A bookkeeper that lives inside WhatsApp.** Market traders send a voice note — in Pidgin or English in Lagos, Portuguese in São Paulo, French in Abidjan, Hindi in Delhi — and OwoBook turns it into a ledger in their own currency, so they never lose track of who owes them. It warns them before they give credit to someone who pays late, and turns their records into a statement they can take to a lender.

> "Mama Bisi collect 3 bags rice, she go pay 45k Friday" 🎤
>
> ✅ Recorded · 👤 Mama Bisi · 🛒 3 bags rice · 💰 ₦45,000 — owing · 📅 Due: Fri, 30 Oct
> `[ Correct ✓ ] [ Fix it ✏️ ] [ Undo ↩️ ]`

Built for the **Meta Global AI Developer Hackathon** (indie track, "Build for Your World"). Muse Spark and Muse Voice Transcribe power every core feature.

---

## The problem

Traders in Lagos markets sell on credit every day: "pay me Friday". They keep track in their heads, in notebooks, or by scrolling back through WhatsApp chats. Forgotten debts mean lost money, and nobody can tell whether the week was profitable.

Bookkeeping apps expect a download, forms, typing and English. A trader serving five customers at once has none of that time.

## The solution

OwoBook is a WhatsApp number. No app, no form, no typing. The trader just talks.

| The trader says (voice or text) | OwoBook does |
|---|---|
| "Mama Bisi collect 3 bags rice, she go pay 45k Friday" | Records a credit sale due Friday and sends a receipt with **Correct / Fix it / Undo** buttons |
| "Tutu collect three charger, 45k. He give me 20k, he go balance Saturday" | One sale: ₦45,000 total, ₦20,000 paid now, ₦25,000 owing |
| "Mama Bisi don pay 20k" | Records the payment: "Mama Bisi now owes you ₦25,000" |
| "Sold 5 crates of eggs 12k cash" | Records a cash sale |
| "Who owe me money?" | Lists debtors by amount, with overdue ⚠️ flags and a total |
| "How much I sell this week?" | Cash sales, credit sales, money collected and expenses |
| *(no amount, or an unclear name)* | Asks **one** short question instead of guessing |
| "Reply me in Yoruba" | Every reply from then on is in Yorùbá: receipts, lists, reminders and questions. Also Hausa, Igbo, Pidgin and English |
| 📷 *a photo of a receipt, or a page of the old paper debt book* | Reads it with Muse Spark vision and records every line: one receipt per customer, and a question for any amount it can't read |
| "Send me receipt for Mama Bisi" | Sends a **picture receipt** to forward to the customer: what they bought, the amount paid, the balance still owing and the due date (or **PAID IN FULL**). Every number comes from the ledger |
| "Chidi collect 2 cartons indomie 13k, he go pay Monday" *(Chidi has paid late before)* | The receipt adds a heads-up from Chidi's history: **"⚠️ Heads up: Chidi has paid late 3 of 4 times"**, and anything already overdue. "Can I give Chidi credit?" shows his payment record. Facts, not advice |
| "Send me my business record" *(or "statement for 90 days")* | A **PDF statement** for the last 30/60/90 days: sales, money collected, expenses, credit owed, week-by-week totals, every transaction and how reliably customers repay. Something to show a bank, microfinance bank or cooperative |
| "Show my ledger" | A **private link** to a read-only web page of the whole book: who owes you, overdue total, this week's sales and collections, recent records (with 🎙️/💬/📷 source). It refreshes itself, so a new voice note appears a few seconds later. Signed link, expires in 7 days, nothing to log in to |
| 🎤 *"A Maria levou 3 sacos de arroz, vai pagar 450 reais na sexta"* from a +55 number | Detects Portuguese and Brazil: welcome, receipt, buttons and replies in Portuguese, amounts in **R$** |
| "Tutu number na 0803 123 4567" *(or share Tutu's contact card)* | Saves Tutu's number for one-tap reminders |
| 08:00 every day | Sends the trader a list of debts due today or overdue, plus a polite reminder for each customer. The trader forwards it, or taps **Send to Tutu** to open their own chat with Tutu, message already typed |

**Global from the first message.** The currency comes from the trader's phone number: +234 → ₦, +254 → KSh, +233 → GH₵, +91 → ₹, +55 → R$, +225/+221/+237 → FCFA, and more. The language comes from what they say: a Portuguese, French or Hindi voice note or text switches replies to that language (Muse Voice Transcribe is steered by the country's language too). Nigerian traders who speak Pidgin or English get English replies by default. Say "reply me in Pidgin" (or Yorùbá, Hausa, Igbo, Portuguese, French, Hindi) to switch. Receipts, lists, reminders and the customer drafts then come in that language. Voice notes work in English, Pidgin, Portuguese, French and Hindi; Yorùbá, Hausa and Igbo work as text, since Muse Voice Transcribe doesn't list them yet (nor Swahili). It never messages the trader's customers itself: reminders always go out from the trader's own WhatsApp, which keeps them within WhatsApp's rules and looks trustworthy to the customer.

---

## Meta AI tooling

| Component | Role in OwoBook |
|---|---|
| **Muse Spark** (`muse-spark-1.3`, Meta Model API) | The agent. It reads each message, picks a ledger tool (record a sale or payment, look up balances, summarise sales, correct an entry) and fills in its arguments. It also asks a clarifying question when something is missing. |
| **Muse Spark vision** (same model, image input) | Reads receipt photos, invoices and handwritten debt-book pages into one line per entry, which then go through the same agent and tools as a voice note. |
| **Muse Voice Transcribe** (`muse-voice-transcribe-1.0`) | Turns WhatsApp voice notes into text in five languages. Each call is biased toward the trader's language (from their choice or phone country) and their own customer names, so names come back the way the trader saved them. |
| **Muse Spark Open on Baseten** (`meta-models/muse-spark-open-1026`) | Meta's open-weights model. It's a drop-in second provider (`LLM_PROVIDER=baseten`) and was evaluated on the same test set. |
| **WhatsApp Cloud API** | The whole user interface: voice notes in, receipts and reply buttons out, read receipts and the "typing…" indicator. |

No non-Meta model is used anywhere.

### Accuracy (evals)

[`evals/cases.jsonl`](evals/cases.jsonl) holds 42 real-style Pidgin and English messages, each with the ledger entry it should produce: "45k", "4.5k", "fifteen thousand naira", "3 bags at 15k each", part-payments, "next tomorrow", saving customer numbers and so on. [`evals/run_evals.py`](evals/run_evals.py) runs them through the real agent and tools.

| Provider | Model | Score |
|---|---|---|
| Meta Model API | `muse-spark-1.3` | **42/42** (two runs) |
| Baseten | `meta-models/muse-spark-open-1026` | **39/39** (two runs, before the 3 phone-number cases were added) |
| Keyword rules, no AI (for comparison) | — | 28/36 on the earlier 36-case set |

The gap shows what Muse Spark adds: keyword rules miss the loose, mixed Pidgin-English of real voice notes that Muse Spark handles. (The same keyword rules let the automated tests run offline without API keys.)

### Cost per voice note

Each model call is logged in `usage_log`. [`scripts/cost_report.py`](scripts/cost_report.py) attributes each call to the message that caused it. Measured on real voice notes and text:

| | Cost |
|---|---|
| Muse Voice Transcribe (≈9s voice note) | $0.0004 |
| Muse Spark (≈3,500 input / 370 output tokens) | $0.0059 |
| **One voice note, end to end** | **≈ $0.0064 (about ₦10)** |
| One text message | ≈ $0.0047 |
| One photo (vision read + agent) | ≈ $0.011 for a receipt, ≈ $0.026 for a 4-line debt-book page |

A trader sending 20 voice notes a day costs about **$4 a month**. That's low enough for a ₦2,000–3,000 monthly subscription to be profitable. These are upper bounds: the system prompt is the same on every call, so cached-input pricing would cut it further.

```bash
uv run python scripts/cost_report.py --usd-ngn 1550
```

---

## Architecture

```
Trader (WhatsApp)
     │  voice note / photo / text / button tap
     ▼
WhatsApp Cloud API ──webhook──▶ FastAPI (OwoBook, Render)
                                  │
                                  ├─ 1. check X-Hub-Signature-256, reply 200 at once
                                  ├─ 2. per-trader queue; skip duplicates by message id
                                  ├─ 3. download voice note / photo (Graph API)
                                  ├─ 4. voice → text ───────▶ Muse Voice Transcribe
                                  │     photo → lines ──────▶ Muse Spark vision
                                  ├─ 5. agent + ledger tools ▶ Muse Spark (or Muse Spark Open on Baseten)
                                  ├─ 6. tools read/write ───▶ Postgres (Supabase)
                                  └─ 7. receipt + buttons ──▶ WhatsApp Cloud API

cron-job.org ── 08:00 Lagos ──▶ POST /tasks/reminders ──▶ due/overdue debts ──▶ trader
```

**Design choices that keep it reliable**

- **Numbers come from the database, never from the model.** Muse Spark chooses the tool and its arguments. Our code saves the entry and writes the receipt from what was saved, so the amount the trader sees is exactly what's in the ledger. Money is stored as integer kobo.
- **Arithmetic is done in code.** For "3 bags at 15k each" the model passes the quantity and unit price, and code computes ₦45,000. Part-payments work the same way.
- **Questions are tool calls.** "Who owe me money?" runs a SQL query. The ledger is never stuffed into the prompt.
- **Photos become text first.** Muse Spark vision turns a receipt or debt-book page into plain lines ("Chidi took 3 cartons of indomie for 19500, will pay Saturday"). Those go through the same agent and tools as a voice note, so a photo gets the same receipts, buttons and checks. An unreadable amount is written as "(amount unclear)", and OwoBook asks instead of guessing.
- **Every write can be undone.** Correct / Fix it / Undo buttons; "Fix it" lets the next voice note correct that entry.
- **The webhook always replies 200 within milliseconds.** Work happens in the background, one message at a time per trader. Duplicates from Meta's retries are dropped before any paid model call. Any failure sends the trader "please try again" and is logged.
- **One-tap reminders without breaking WhatsApp's rules.** If the trader has saved a customer's number, the reminder is a link-button message (`cta_url`). Its **Send to …** button opens `wa.me/<number>?text=<reminder>`, the trader's own chat with that customer with the message ready, so the trader still decides and sends.
- **WhatsApp's 24-hour window is respected.** Reminders are sent as normal messages if the trader wrote in the last 24 hours. Otherwise they use an approved template if one is configured, and are skipped if not.

---

## Run it locally

Needs Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env          # add your keys
uv run pytest                 # 226 tests, no keys needed
```

**Terminal simulator.** Chat with OwoBook without WhatsApp:

```bash
uv run python scripts/simulate.py --provider fake   # rule-based fake AI, no keys
uv run python scripts/simulate.py -v                # real Muse Spark + Muse Voice Transcribe
```

Type messages, or paste the path to an audio file to send it as a voice note. Type `1`/`2`/`3` to tap a button and `/reminders` to preview the morning reminders.

**Demo data and evals**

```bash
uv run python scripts/seed_demo.py --wa-id 2348012345678 --reset   # a realistic week of trading
uv run python evals/run_evals.py --provider meta
uv run python scripts/run_reminders.py --dry-run                    # this morning's reminders, sends nothing
```

### Connect WhatsApp locally (Meta test number + ngrok)

1. In the [Meta App Dashboard](https://developers.facebook.com/apps), create an app and add the **WhatsApp** product.
2. Under **WhatsApp → API Setup**:
   - copy the access token into `WA_ACCESS_TOKEN` (the temporary one lasts 24 hours; see [Permanent token](#3-permanent-whatsapp-token));
   - copy the **Phone number ID** into `WA_PHONE_NUMBER_ID`;
   - add your own number as a recipient.
3. Under **App settings → Basic**, copy the **App secret** into `WA_APP_SECRET`. Choose any `WA_VERIFY_TOKEN`, and put your number in `ALLOWED_WA_IDS`.
4. Start the server and a tunnel, each in its own terminal:
   ```bash
   uv run uvicorn owobook.main:app --port 8000
   ```
   ```bash
   ngrok http 8000
   ```
5. Under **WhatsApp → Configuration → Webhook**, set the callback URL to `https://<ngrok-domain>/webhook` and the verify token to your `WA_VERIFY_TOKEN`. Click **Verify and save**, then subscribe to the **messages** field.
6. Link your WhatsApp Business Account to the app. On test numbers the dashboard often skips this, and then no messages arrive:
   ```bash
   curl -X POST "https://graph.facebook.com/v26.0/<WABA_ID>/subscribed_apps" -H "Authorization: Bearer $WA_ACCESS_TOKEN"
   ```
7. Send "hi" to the test number.

---

## Deploy (Render + Supabase, free tiers)

The web service runs on Render's free plan (Docker, [`render.yaml`](render.yaml)) and the ledger lives in Supabase Postgres. Render's disk is wiped on every deploy, so SQLite is only for local development.

### 1. Database: Supabase
1. Create a project at [supabase.com](https://supabase.com). The region closest to Lagos is fine.
2. Click **Connect**, then copy the **Session pooler** connection string (port **5432**). Render has no IPv6, and only the pooler works over IPv4. Put your database password in it.

OwoBook creates its tables on startup (Alembic migrations), so there's nothing to run by hand.

### 2. Web service: Render
1. Push this repository to GitHub.
2. In [Render](https://dashboard.render.com), choose **New → Blueprint** and pick the repo. Render reads `render.yaml`.
3. Fill in the secrets it asks for: `DATABASE_URL`, `MODEL_API_KEY`, `BASETEN_API_KEY`, `WA_ACCESS_TOKEN`, `WA_PHONE_NUMBER_ID`, `WA_APP_SECRET`, `ALLOWED_WA_IDS`, and `WA_REMINDER_TEMPLATE` (optional; leave it empty).
4. Once it's live, `https://<service>.onrender.com/health` returns `{"ok":true}`.

`WA_VERIFY_TOKEN` and `TASK_TOKEN` are generated by Render. Copy them from the service's **Environment** tab.

### 3. Permanent WhatsApp token
The API Setup token expires after 24 hours. For the deployed bot:
1. Go to **Meta Business Settings → Users → System users**, and add a system user with the Admin role.
2. Click **Add assets**: add your app (full control) and your WhatsApp account (full control).
3. Click **Generate new token**, pick your app, set the expiry to **Never**, and tick `whatsapp_business_messaging` and `whatsapp_business_management`.
4. Paste it into `WA_ACCESS_TOKEN` on Render.

### 4. Point WhatsApp at Render
In **WhatsApp → Configuration**, set the callback URL to `https://<service>.onrender.com/webhook` and the verify token to Render's `WA_VERIFY_TOKEN`. Click **Verify and save**.

### 5. Keep it awake and send reminders: cron-job.org
Render's free plan sleeps after 15 idle minutes, and the first message after that waits about a minute. Two free jobs on [cron-job.org](https://cron-job.org) fix both problems:

| Job | URL | Method | Schedule (Africa/Lagos) | Header |
|---|---|---|---|---|
| Keep awake | `https://<service>.onrender.com/health` | GET | every 10 minutes | — |
| Reminders | `https://<service>.onrender.com/tasks/reminders` | POST | daily 08:00 | `X-Task-Token: <TASK_TOKEN>` |

Render's 750 free hours a month cover one service running all month. The in-process scheduler is off on Render (`ENABLE_SCHEDULER=false`), so the cron is the only trigger. Reminders go out at most once per trader per day, even if the trigger fires twice.

---

## Demo script (≈ 4½ minutes)

*Setup:* the deployed bot, your phone on screen-share or mirrored, and `seed_demo.py --reset` run for your number.

1. **The problem (20s).** A trader's notebook and a phone full of "I go pay you Friday" chats. "Lagos traders lose money to forgotten credit every week."
2. **Log by voice (40s).** Record a Pidgin voice note: *"Mama Bisi collect 3 bags rice, she go pay 45k Friday."* The receipt arrives with the transcript, amount and due date. Tap **Correct ✓**.
3. **Part-payment (25s).** Voice: *"Tutu collect three charger, 45k. He give me 20k, he go balance Saturday."* One receipt: ₦45,000 total, ₦20,000 paid, ₦25,000 owing. Say *"send me receipt"* and a picture receipt arrives, ready to forward to Tutu.
4. **Fix a mistake (20s).** Tap **Fix it ✏️** on a receipt and say *"no, na 4 bags, 60k."* A corrected receipt comes back.
5. **Ask (25s).** *"Who owe me money?"* gives the list with ⚠️ overdue flags. *"How much I sell this week?"* gives the totals.
6. **Reminders (35s).** Say *"Mama Bisi number na 0803 123 4567"*. Trigger the morning job: "3 customers have debts due or overdue", then a polite message per customer. Tap **Send to Mama Bisi** and WhatsApp opens her chat with the reminder already typed. Forward the others with a long-press.
7. **Old debt book (20s).** Snap a page of a paper debt book. OwoBook lists what it read, records each customer's debt, and asks about the one amount it couldn't read.
8. **Who pays late (25s).** *"Iya Tola collect 1 bag rice 30k, she go pay Friday."* The receipt warns: "⚠️ Heads up: Iya Tola already has ₦32,000 overdue." Then *"Can I give Alhaji Musa credit?"* shows his payment record. "The decision every trader makes every day, now with data."
9. **Business record for a loan (25s).** *"Send me my business record."* A PDF arrives: 30 days of sales, collections, credit owed, how reliably customers repay, every transaction. "Traders are refused loans for having no records. Now they have them." Then *"show my ledger"*: open the private link on a laptop beside the phone, send one more voice note, and watch it appear on the page.
10. **Global (15s).** A second phone: *"Minha moeda é real"*, then a Portuguese voice note — *"A Maria levou três sacos de arroz, vai pagar quatrocentos e cinquenta reais na sexta."* Welcome, receipt and buttons in Portuguese, R$ 450. (A real +55 number gets reais and Portuguese automatically.)
11. **Under the hood (25s).** The architecture diagram: Muse Voice Transcribe → Muse Spark tool calls → Postgres. Then evals at 42/42 on Muse Spark, and about ₦10 per voice note.

---

## Project layout

```
owobook/
  main.py              FastAPI app: webhook, /tasks/reminders, /health, scheduler
  agent/               handler (dedupe, onboarding, buttons, editing), agent loop, ledger tools
  llm/                 Meta Model API, Baseten and fake providers (OpenAI-compatible tool calling)
  transcribe/          Muse Voice Transcribe client, ffmpeg → WAV
  vision.py            receipt / debt-book photos → lines, with Muse Spark vision
  receipt.py           picture receipts for customers (Pillow, bundled DejaVu font for ₦ and tone marks)
  business_record.py   the PDF statement for loans (fpdf2)
  ledger_page.py       private read-only ledger page behind a signed 7-day link
  currency.py          currency from the phone's country code; money formatting per trader
  language.py          language detection and voice-note language hints
  script.py            Devanagari → Latin letters for pictures/PDFs
  whatsapp/            Cloud API client, webhook parsing + signature check, receipt formatting,
                       i18n.py: every fixed text in English, Pidgin, Yorùbá, Hausa and Igbo
  ledger.py            business logic: sales, payments, balances, summaries
  money.py, dates.py   "45k" / "fifteen thousand naira" parsing, Lagos dates ("next tomorrow")
  reminders.py         daily reminders, 24-hour window, templates
  usage.py             token/audio cost tracking
  prompts/             the Muse Spark system prompt and the photo-reading prompt
scripts/               simulate, seed_demo, run_reminders, cost_report
evals/                 42 labelled Pidgin/English cases + runner
tests/                 335 tests (money, currencies, languages, dates, ledger, trust, agent, photos, receipts, business record, ledger page, webhook, reminders, usage)
```

More detail on decisions and API findings is in [`docs/NOTES.md`](docs/NOTES.md).
