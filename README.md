# OwoBook

OwoBook turns WhatsApp voice notes in Pidgin and English into a ledger for market traders, so they never lose track of who owes them.

> The full README (architecture, Meta tooling, costs, demo script) comes in Phase 5. This file covers running it so far.

## Run locally

```bash
uv sync
cp .env.example .env        # then fill in the keys
uv run pytest               # tests (no keys needed)
```

**Terminal simulator** (no WhatsApp needed):

```bash
uv run python scripts/simulate.py --provider fake   # rule-based fake AI, no keys
uv run python scripts/simulate.py -v                # real Muse Spark + Muse Voice Transcribe
```

**Evals** (39 Pidgin/English messages → expected ledger tool calls):

```bash
uv run python evals/run_evals.py --provider meta
uv run python evals/run_evals.py --provider baseten
```

## Connect WhatsApp (development, with Meta's test number)

1. In the [Meta App Dashboard](https://developers.facebook.com/apps), create an app and add the **WhatsApp** product.
2. Under **WhatsApp → API Setup**:
   - copy the **temporary access token** into `WA_ACCESS_TOKEN`. It expires after 24 hours; Phase 5 switches to a permanent token.
   - copy the **Phone number ID** into `WA_PHONE_NUMBER_ID`.
   - in the **To** field, add your own WhatsApp number and confirm it with the code WhatsApp sends you.
3. Under **App settings → Basic**, copy the **App secret** into `WA_APP_SECRET`.
4. Make up a random `WA_VERIFY_TOKEN` (any string). Set `ALLOWED_WA_IDS` to your number in international format without `+`, e.g. `2348012345678`.
5. Start the server and a public tunnel, in two terminals:
   ```bash
   uv run uvicorn owobook.main:app --port 8000
   ```
   ```bash
   ngrok http 8000
   ```
6. Under **WhatsApp → Configuration → Webhook**, set the callback URL to `https://<your-ngrok-domain>/webhook` and the verify token to your `WA_VERIFY_TOKEN`. Click **Verify and save**. Then, under **Webhook fields**, subscribe to **messages**.
7. Send a voice note to the test number from your phone. The receipt comes back with **Correct / Fix it / Undo** buttons.

The ngrok URL changes each time ngrok restarts (unless you use a free static domain). When it changes, update the callback URL in step 6.

## Debt reminders

Every day at **08:00 Africa/Lagos**, the server messages each trader about debts due today or overdue. The trader gets a heads-up, then a polite message for each customer that they can forward. OwoBook never messages customers.

```bash
uv run python scripts/seed_demo.py --reset                  # demo ledger for the first ALLOWED_WA_IDS number
uv run python scripts/run_reminders.py --dry-run            # see this morning's reminders, send nothing
uv run python scripts/run_reminders.py --wa-id 234801...    # send them now over WhatsApp
```

WhatsApp only allows free-form messages within 24 hours of the trader's last message. So send OwoBook any message first, or set `WA_REMINDER_TEMPLATE` to an approved template (see `.env.example`). In the simulator, type `/reminders`.

## How a message flows

`POST /webhook` → check `X-Hub-Signature-256` → reply `200` at once → in the background, one message at a time per trader:
1. skip duplicates (Meta retries) by message id;
2. mark read and show "typing…";
3. download the voice note, then transcribe it with Muse Voice Transcribe;
4. run the Muse Spark agent with ledger tools;
5. save to the database;
6. send the receipt with reply buttons.

Any failure sends the trader a friendly "please try again" and is logged.
