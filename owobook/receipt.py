"""Picture receipts the trader can forward to a customer (PNG, drawn with Pillow).

A receipt is built from one ledger entry — a cash sale, a credit sale (maybe part-paid) or a
payment — and every number on it comes from the database:
- sale: item, total, amount paid, balance left on this sale, due date, and the customer's total
  balance if they owe for other things too;
- payment: amount paid, balance before and after, and the next due date while anything is owing.
Labels are in the trader's chosen language; dates stay in English like the rest of OwoBook.
"""

import io
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from sqlalchemy.orm import Session

from owobook import ledger
from owobook.dates import to_local
from owobook.models import Entry, EntryType, Trader
from owobook.money import format_money
from owobook.script import drawable
from owobook.whatsapp import formatting as fmt
from owobook.whatsapp.formatting import Lang

FONTS = Path(__file__).resolve().parent / "assets" / "fonts"  # DejaVu Sans: has ₦ and Yorùbá/Hausa/Igbo letters


@dataclass
class ReceiptInfo:
    kind: str  # "sale" | "payment"
    number: str  # "OB-00012"
    issued_at: datetime  # Lagos time
    shop: str
    customer: str | None
    rows: list[tuple[str, str]] = field(default_factory=list)  # (label key, value) above the balance box
    balance_kobo: int = 0  # what's still owing after this receipt (this sale, or the account for payments)
    all_owing_kobo: int | None = None  # customer's total, shown only when it's more than balance_kobo
    due: date | None = None


def build_receipt(db: Session, trader: Trader, entry: Entry) -> ReceiptInfo:
    """Receipt facts for one sale or payment, as of that entry (later entries don't change it)."""
    if entry.type not in ledger.RECEIPT_TYPES:
        raise ValueError(f"entry {entry.id} is a {entry.type}, not a sale or payment")
    customer = entry.customer
    after = ledger.customer_balance(db, trader, customer, upto_entry_id=entry.id) if customer else 0
    info = ReceiptInfo(
        kind="payment" if entry.type == EntryType.PAYMENT else "sale",
        number=f"OB-{entry.id:05d}",
        issued_at=to_local(entry.created_at),
        shop=drawable(trader.shop_name or trader.name or "OwoBook"),
        customer=drawable(customer.display_name) if customer else None,
    )
    if entry.type == EntryType.PAYMENT:
        info.rows = [("rc_prev", format_money(after + entry.amount_kobo)), ("rc_paid", format_money(entry.amount_kobo))]
        info.balance_kobo = max(after, 0)
        if info.balance_kobo:
            dues = [s.due_date for s, _ in ledger.outstanding_sales(db, trader, customer) if s.due_date]
            info.due = min(dues) if dues else None
        return info

    if item := fmt.item_line(entry):
        info.rows.append(("rc_item", drawable(item)))
    info.rows.append(("rc_total", format_money(entry.amount_kobo)))
    if entry.type == EntryType.CASH_SALE:
        info.rows.append(("rc_paid", format_money(entry.amount_kobo)))
        return info
    info.rows.append(("rc_paid", format_money(entry.paid_now_kobo)))
    info.balance_kobo = entry.amount_kobo - entry.paid_now_kobo
    info.due = entry.due_date
    if after > info.balance_kobo:
        info.all_owing_kobo = after
    return info


# ---------- drawing -------------------------------------------------------------------------

WIDTH, PAD = 800, 48
INK, MUTED, LINE = (28, 25, 23), (110, 104, 98), (214, 208, 200)
PAPER, BRAND, BRAND_TEXT = (255, 253, 248), (22, 101, 52), (255, 255, 255)
OWING_BG, OWING_INK = (254, 243, 199), (146, 64, 14)
PAID_BG, PAID_INK = (220, 252, 231), (22, 101, 52)


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")), size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    return lines + [line] if line else lines


def _draw(img: Image.Image, info: ReceiptInfo, lang: Lang) -> int:
    """Draw the receipt top-down; returns the height used."""
    d = ImageDraw.Draw(img)
    inner = WIDTH - 2 * PAD
    t = lambda key: fmt.t(lang, key)  # noqa: E731

    # Header band: shop name.
    shop_font = _font(40, True)
    shop_lines = _wrap(d, info.shop.upper(), shop_font, inner)
    band = 40 + 50 * len(shop_lines) + 24
    d.rectangle([0, 0, WIDTH, band], fill=BRAND)
    y = 40
    for line in shop_lines:
        d.text((WIDTH / 2, y), line, font=shop_font, fill=BRAND_TEXT, anchor="mt")
        y += 50
    y = band + 36

    title = t("rc_payment" if info.kind == "payment" else "rc_sale")
    d.text((WIDTH / 2, y), title, font=_font(34, True), fill=INK, anchor="mt")
    y += 64

    label_font, value_font = _font(26), _font(26, True)

    def split(label: str, value: str, font) -> tuple[list[str], list[str]]:
        return _wrap(d, label, label_font, int(inner * 0.42)), _wrap(d, value, font, int(inner * 0.56))

    def row(label: str, value: str, *, font=value_font, color=INK) -> None:
        nonlocal y
        label_lines, value_lines = split(label, value, font)
        for i in range(max(len(label_lines), len(value_lines))):
            if i < len(label_lines):
                d.text((PAD, y), label_lines[i], font=label_font, fill=MUTED)
            if i < len(value_lines):
                d.text((WIDTH - PAD, y), value_lines[i], font=font, fill=color, anchor="ra")
            y += 36
        y += 10

    def rule() -> None:
        nonlocal y
        for x in range(PAD, WIDTH - PAD, 18):  # dashed, like a till receipt
            d.line([(x, y), (x + 9, y)], fill=LINE, width=2)
        y += 26

    row(t("rc_no"), info.number, font=label_font, color=INK)
    row(t("rc_date"), f"{info.issued_at:%a, %-d %b %Y, %-I:%M %p}", font=label_font, color=INK)
    if info.customer:
        row(t("rc_customer"), info.customer)
    rule()
    for key, value in info.rows:
        row(t(key), value)
    rule()

    # Balance box: what's still owing (+ due date), or PAID IN FULL.
    if info.balance_kobo > 0:
        big = _font(30, True)
        box_rows = [(t("rc_balance"), format_money(info.balance_kobo), big)]
        if info.due:
            box_rows.append((t("rc_due"), f"{info.due:%a, %-d %b %Y}", value_font))
        if info.all_owing_kobo:
            box_rows.append((t("rc_all_owing"), format_money(info.all_owing_kobo), value_font))
        top, y = y, y + 22
        box_height = sum(36 * max(map(len, split(label, value, font))) + 10 for label, value, font in box_rows)
        d.rounded_rectangle([PAD - 16, top, WIDTH - PAD + 16, y + box_height + 12], radius=16, fill=OWING_BG)
        for label, value, font in box_rows:
            row(label, value, font=font, color=OWING_INK)
        y += 34
    else:
        d.rounded_rectangle([PAD - 16, y, WIDTH - PAD + 16, y + 76], radius=16, fill=PAID_BG)
        d.text((WIDTH / 2, y + 38), f"✓ {t('rc_paid_full')}", font=_font(32, True), fill=PAID_INK, anchor="mm")
        y += 110

    for line in _wrap(d, t("rc_thanks"), _font(26), inner):
        d.text((WIDTH / 2, y), line, font=_font(26), fill=INK, anchor="mt")
        y += 36
    y += 14
    d.text((WIDTH / 2, y), "Made with Meta", font=_font(20), fill=MUTED, anchor="mt")
    return y + 48


def render_png(info: ReceiptInfo, lang: Lang) -> bytes:
    """The receipt as PNG bytes (8-bit RGB, well under WhatsApp's 5 MB image limit)."""
    canvas = Image.new("RGB", (WIDTH, 2400), PAPER)
    height = _draw(canvas, info, lang)
    buf = io.BytesIO()
    canvas.crop((0, 0, WIDTH, height)).save(buf, format="PNG", optimize=True)
    return buf.getvalue()
