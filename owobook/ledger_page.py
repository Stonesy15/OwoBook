"""A trader's private, read-only ledger page: "show my ledger" in WhatsApp → a link that opens it.

The link carries a signed token (trader id + expiry + HMAC), so there is nothing to store and nobody can
guess another trader's link. It expires after LINK_DAYS; asking again gives a fresh one. The page only
shows data — every change still happens in WhatsApp. It refreshes itself, so a new voice note shows up
on screen a few seconds later.
"""

import base64
import hashlib
import hmac
from datetime import date, datetime, timedelta
from html import escape

from sqlalchemy.orm import Session

from owobook import ledger
from owobook.config import get_settings
from owobook.dates import format_date, to_local, today_local, utcnow
from owobook.models import Entry, EntryType, Trader
from owobook.money import format_money
from owobook.whatsapp import formatting as fmt

LINK_DAYS = 7
REFRESH_SECONDS = 15


# ---------- the link -------------------------------------------------------------------------


def _key() -> bytes | None:
    """Signing key, derived from LINK_SECRET (or WA_APP_SECRET) so the raw app secret is never used directly."""
    s = get_settings()
    secret = s.link_secret or s.wa_app_secret
    return hmac.new(secret.encode(), b"owobook-ledger-link", hashlib.sha256).digest() if secret else None


def _sign(key: bytes, payload: str) -> str:
    mac = hmac.new(key, payload.encode(), hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def make_token(trader_id: int, now: datetime | None = None, days: int = LINK_DAYS) -> str | None:
    """'<id>.<expiry>.<signature>' (base 36 numbers), or None if no secret is configured."""
    key = _key()
    if key is None:
        return None
    expires = int(((now or utcnow()) + timedelta(days=days)).timestamp())
    payload = f"{_b36(trader_id)}.{_b36(expires)}"
    return f"{payload}.{_sign(key, payload)}"


def read_token(token: str, now: datetime | None = None) -> int | None:
    """The trader id in a valid, unexpired token; None otherwise."""
    key = _key()
    try:
        tid, exp, sig = token.split(".")
        trader_id, expires = int(tid, 36), int(exp, 36)
    except ValueError:
        return None
    if key is None or not hmac.compare_digest(sig, _sign(key, f"{tid}.{exp}")):
        return None
    if expires < (now or utcnow()).timestamp():
        return None
    return trader_id


def link_for(trader: Trader, now: datetime | None = None) -> str | None:
    """The full private URL, or None if the server's public address or secret isn't configured."""
    base = get_settings().public_base_url.rstrip("/")
    token = make_token(trader.id, now)
    return f"{base}/l/{token}" if base and token else None


def _b36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        n, r = divmod(n, 36)
        out = digits[r] + out
        if not n:
            return out


# ---------- the page -------------------------------------------------------------------------

_SOURCE = {"voice": "🎙️ voice", "text": "💬 text", "image": "📷 photo"}


def _describe(e: Entry) -> str:
    who = e.customer.display_name if e.customer else ""
    item = fmt.item_line(e) or ""
    if e.type == EntryType.PAYMENT:
        return f"{who} paid"
    if e.type == EntryType.EXPENSE:
        return f"Expense: {item or e.item_description or ''}".strip()
    if e.type == EntryType.CASH_SALE:
        return f"Sold {item} (cash)" + (f" to {who}" if who else "")
    text = f"{who} took {item} on credit" if item else f"{who} — credit sale"
    if e.paid_now_kobo:
        text += f", paid {format_money(e.paid_now_kobo)} now"
    return text


def render_page(db: Session, trader: Trader, today: date | None = None) -> str:
    """The ledger page as HTML. Every number comes straight from the database."""
    today = today or today_local()
    debtors = ledger.list_debtors(db, trader, today=today)
    week = ledger.sales_summary(db, trader, "this_week", today)
    recent = ledger.recent_entries(db, trader, limit=25)
    owed = sum(d.balance_kobo for d in debtors)
    overdue = sum(d.balance_kobo for d in debtors if d.overdue)
    title = escape(trader.shop_name or trader.name or "My shop")

    debtor_rows = "".join(
        f"<tr><td>{escape(d.customer.display_name)}</td><td class='num'>{format_money(d.balance_kobo)}</td>"
        f"<td>{_due_cell(d, today)}</td></tr>"
        for d in debtors
    ) or "<tr><td colspan='3' class='empty'>Nobody owes you money right now 🎉</td></tr>"

    activity_rows = "".join(
        f"<tr><td class='when'>{to_local(e.created_at):%d %b, %H:%M}</td><td>{escape(_describe(e))}"
        f"<span class='src'>{_SOURCE.get(e.source, '')}</span></td>"
        f"<td class='num {'in' if e.type == EntryType.PAYMENT else ''}'>{format_money(e.amount_kobo)}</td></tr>"
        for e in recent
    ) or "<tr><td colspan='3' class='empty'>Nothing recorded yet. Send a voice note in WhatsApp.</td></tr>"

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow"><meta http-equiv="refresh" content="{REFRESH_SECONDS}">
<title>{title} · OwoBook</title>
<style>{_CSS}</style>
</head><body><main>
<header><div class="brand">OwoBook</div><h1>{title}</h1>
<p class="muted">Private, read-only ledger · updated {to_local(utcnow()):%d %b %Y, %H:%M}</p></header>
<section class="cards">
<div class="card hero"><span>Owed to you</span><strong>{format_money(owed)}</strong>
<small>{len(debtors)} customer{'s' if len(debtors) != 1 else ''}</small></div>
<div class="card"><span>Overdue</span><strong class="{'warn' if overdue else ''}">{format_money(overdue)}</strong></div>
<div class="card"><span>Sales this week</span><strong>{format_money(week.total_sales_kobo)}</strong>
<small>{week.sale_count} sale{'s' if week.sale_count != 1 else ''}</small></div>
<div class="card"><span>Collected this week</span><strong class="ok">{format_money(week.collected_kobo)}</strong></div>
</section>
<section><h2>Who owes you</h2><div class="scroll"><table>
<thead><tr><th>Customer</th><th class="num">Owes</th><th>Due</th></tr></thead><tbody>{debtor_rows}</tbody>
</table></div></section>
<section><h2>Recent activity</h2><div class="scroll"><table>
<thead><tr><th>When</th><th>What</th><th class="num">Amount</th></tr></thead><tbody>{activity_rows}</tbody>
</table></div></section>
<footer class="muted">To add or change anything, just message OwoBook on WhatsApp. Powered by Meta AI.</footer>
</main></body></html>"""


def _due_cell(d: ledger.Debtor, today: date) -> str:
    if not d.next_due:
        return "<span class='muted'>—</span>"
    text = escape(format_date(d.next_due, today))
    if d.overdue:
        return f"<span class='pill warn'>⚠️ {text}</span>"
    if d.due_today:
        return f"<span class='pill today'>Today</span>"
    return text


def expired_page() -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex, nofollow">
<title>Link expired · OwoBook</title><style>{_CSS}</style></head><body><main>
<header><div class="brand">OwoBook</div><h1>This link has expired</h1>
<p class="muted">Ledger links work for {LINK_DAYS} days. Send <b>“show my ledger”</b> to OwoBook on WhatsApp for a new one.</p>
</header></main></body></html>"""


_CSS = """
:root{--bg:#f6f7f5;--card:#fff;--text:#16201a;--muted:#5f6b63;--line:#e3e7e3;--green:#0f7a43;--warn:#b42318;
--warnbg:#fde8e6;--todaybg:#fff4d6;--today:#8a5a00}
@media (prefers-color-scheme:dark){:root{--bg:#0f1411;--card:#18201b;--text:#e8eee9;--muted:#9aa79f;--line:#26302a;
--green:#4cc485;--warn:#ff8a7a;--warnbg:#3a1c19;--todaybg:#3a2f12;--today:#ffd36b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:860px;margin:0 auto;padding:24px 16px 40px}
.brand{color:var(--green);font-weight:700;letter-spacing:.04em;font-size:13px;text-transform:uppercase}
h1{margin:4px 0 2px;font-size:26px}h2{font-size:17px;margin:28px 0 10px}
.muted{color:var(--muted);font-size:13px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-top:20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;display:flex;flex-direction:column;gap:2px}
.card span{color:var(--muted);font-size:13px}.card strong{font-size:22px;font-variant-numeric:tabular-nums}
.card small{color:var(--muted)}.hero{border-color:var(--green)}.hero strong{color:var(--green);font-size:26px}
.warn{color:var(--warn)}.ok{color:var(--green)}
.scroll{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:12px}
table{width:100%;border-collapse:collapse}th,td{padding:10px 14px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.03em}
tr:last-child td{border-bottom:0}.num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
td.in{color:var(--green)}.when{white-space:nowrap;color:var(--muted);font-size:13px}
.src{display:block;color:var(--muted);font-size:12px}.empty{color:var(--muted);text-align:center;padding:20px}
.pill{padding:2px 8px;border-radius:99px;font-size:13px;white-space:nowrap}
.pill.warn{background:var(--warnbg)}.pill.today{background:var(--todaybg);color:var(--today)}
footer{margin-top:28px;text-align:center}
"""
