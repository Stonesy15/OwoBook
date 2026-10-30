"""Business record for loans: a PDF statement of a trader's last 30/60/90 days, built from the ledger.

Informal traders are refused loans because they have no records. OwoBook already has them: every sale,
payment and expense was logged on WhatsApp as it happened. This turns that into a clean statement a bank,
microfinance bank or cooperative can read — sales, collections, expenses, credit given and how reliably
the trader's customers repay. Every number comes from the database; the PDF says plainly that entries are
self-reported (not audited). Labels are English, the language lenders read most; money is the trader's
own currency.
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

from fpdf import FPDF
from fpdf.fonts import FontFace
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from owobook import ledger
from owobook.currency import get as get_currency
from owobook.dates import TZ, to_local
from owobook.models import Entry, EntryStatus, EntryType, Trader
from owobook.money import format_money
from owobook.receipt import FONTS
from owobook.script import drawable
from owobook.whatsapp import formatting as fmt

MAX_ROWS = 300  # transaction rows printed; totals always cover everything

TYPE_LABELS = {
    EntryType.CASH_SALE: "Cash sale",
    EntryType.CREDIT_SALE: "Credit sale",
    EntryType.PAYMENT: "Payment received",
    EntryType.EXPENSE: "Expense",
}


@dataclass
class WeekRow:
    start: date
    sales: int = 0
    collected: int = 0
    expenses: int = 0


@dataclass
class BusinessRecord:
    trader: Trader
    days: int
    start: date  # first day covered (Lagos)
    end: date  # last day covered (today)
    entries: list[Entry] = field(default_factory=list)  # oldest first
    cash_sales: int = 0
    credit_sales: int = 0
    collected: int = 0  # debt payments + money paid on the spot for credit sales
    expenses: int = 0
    active_days: int = 0
    outstanding: int = 0  # owed to the trader right now
    debtors: int = 0
    repaid_on_time: int = 0  # customers' debts with a due date: paid on time / paid late or still overdue
    repaid_late: int = 0
    weeks: list[WeekRow] = field(default_factory=list)
    since: date | None = None  # first record ever

    @property
    def total_sales(self) -> int:
        return self.cash_sales + self.credit_sales

    @property
    def money_in(self) -> int:
        return self.cash_sales + self.collected

    @property
    def number(self) -> str:
        return f"OB-R{self.trader.id:04d}-{self.end:%Y%m%d}-{self.days}"


def _utc_midnight(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=TZ).astimezone(timezone.utc)


def build_record(db: Session, trader: Trader, days: int, today: date) -> BusinessRecord:
    """Totals for the last `days` days (today included), plus where the trader's credit stands now."""
    start = today - timedelta(days=days - 1)
    rec = BusinessRecord(trader=trader, days=days, start=start, end=today)
    rec.entries = list(db.scalars(
        select(Entry)
        .where(Entry.trader_id == trader.id, Entry.status != EntryStatus.DELETED,
               Entry.created_at >= _utc_midnight(start), Entry.created_at < _utc_midnight(today + timedelta(days=1)))
        .order_by(Entry.created_at, Entry.id)
    ))
    weeks: dict[date, WeekRow] = {}
    active = set()
    for e in rec.entries:
        day = to_local(e.created_at).date()
        active.add(day)
        monday = day - timedelta(days=day.weekday())
        week = weeks.setdefault(monday, WeekRow(monday))
        if e.type == EntryType.CASH_SALE:
            rec.cash_sales += e.amount_kobo
            week.sales += e.amount_kobo
        elif e.type == EntryType.CREDIT_SALE:
            rec.credit_sales += e.amount_kobo
            rec.collected += e.paid_now_kobo
            week.sales += e.amount_kobo
            week.collected += e.paid_now_kobo
        elif e.type == EntryType.PAYMENT:
            rec.collected += e.amount_kobo
            week.collected += e.amount_kobo
        elif e.type == EntryType.EXPENSE:
            rec.expenses += e.amount_kobo
            week.expenses += e.amount_kobo
    first = db.scalar(select(func.min(Entry.created_at)).where(Entry.trader_id == trader.id))
    rec.since = min(to_local(x).date() for x in (trader.created_at, first) if x)
    rec.active_days = len(active)
    rec.weeks = [weeks[k] for k in sorted(weeks)]

    debtors = ledger.list_debtors(db, trader, today=today)
    rec.outstanding = sum(d.balance_kobo for d in debtors)
    rec.debtors = len(debtors)
    for customer in ledger.list_customers(db, trader):
        r = ledger.payment_record(db, trader, customer, today)
        rec.repaid_on_time += r.on_time
        rec.repaid_late += r.late + r.overdue  # for a lender, still-unpaid-after-due counts as late
    return rec


# ---------- PDF -----------------------------------------------------------------------------

GREEN, INK, MUTED, RULE = (22, 101, 52), (28, 25, 23), (110, 104, 98), (214, 208, 200)


class _PDF(FPDF):
    record_number = ""

    def footer(self) -> None:
        self.set_y(-14)
        self.set_font("DejaVu", "", 7.5)
        self.set_text_color(*MUTED)
        self.cell(0, 4, f"Statement {self.record_number}  ·  Page {self.page_no()}/{{nb}}", align="C")


def _d(d: date) -> str:
    return f"{d.day} {d:%b %Y}"


def render_pdf(rec: BusinessRecord, generated_at: datetime) -> bytes:
    """The statement as PDF bytes (A4, text — searchable and copyable)."""
    tr = rec.trader
    cur = get_currency(tr.currency)
    shop, owner = drawable(tr.shop_name), drawable(tr.name)  # our fonts can't lay out Devanagari
    pdf = _PDF(format="A4")
    pdf.record_number = rec.number
    pdf.set_margins(16, 16, 16)
    pdf.set_auto_page_break(True, margin=20)
    pdf.add_font("DejaVu", "", str(FONTS / "DejaVuSans.ttf"))
    pdf.add_font("DejaVu", "B", str(FONTS / "DejaVuSans-Bold.ttf"))
    pdf.add_page()
    width = pdf.epw

    # Header band
    pdf.set_fill_color(*GREEN)
    pdf.rect(0, 0, pdf.w, 34, style="F")
    pdf.set_xy(16, 9)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("DejaVu", "B", 18)
    pdf.cell(width, 9, "BUSINESS RECORD")
    pdf.set_xy(16, 19)
    pdf.set_font("DejaVu", "", 10)
    pdf.cell(width, 6, f"{shop or owner or 'Trader'}  ·  Last {rec.days} days: {_d(rec.start)} – {_d(rec.end)}")
    pdf.set_y(42)

    def label_value(label: str, value: str, bold: bool = False) -> None:
        pdf.set_font("DejaVu", "", 9.5)
        pdf.set_text_color(*MUTED)
        pdf.cell(width * 0.45, 6.5, label)
        pdf.set_font("DejaVu", "B" if bold else "", 9.5)
        pdf.set_text_color(*INK)
        pdf.cell(width * 0.55, 6.5, value, align="R", new_x="LMARGIN", new_y="NEXT")

    def heading(text: str) -> None:
        pdf.ln(3)
        pdf.set_font("DejaVu", "B", 11.5)
        pdf.set_text_color(*GREEN)
        pdf.cell(width, 8, text, new_x="LMARGIN", new_y="NEXT")
        pdf.set_draw_color(*RULE)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + width, pdf.get_y())
        pdf.ln(2)

    heading("Trader")
    if owner:
        label_value("Name", owner)
    if shop:
        label_value("Business", shop)
    label_value("WhatsApp number", fmt.format_phone(tr.wa_id))
    label_value("Currency", f"{cur.code} ({cur.symbol})" if cur.symbol else "—")
    label_value("Recording with OwoBook since", _d(rec.since))
    label_value("Statement generated", f"{_d(generated_at.date())}, {generated_at:%H:%M}")

    heading(f"Summary — last {rec.days} days")
    label_value("Total sales", format_money(rec.total_sales), bold=True)
    label_value("   Cash sales", format_money(rec.cash_sales))
    label_value("   Credit sales (goods given on credit)", format_money(rec.credit_sales))
    label_value("Money collected on credit (repayments + part-payments)", format_money(rec.collected))
    label_value("Money in (cash sales + money collected)", format_money(rec.money_in), bold=True)
    label_value("Business expenses recorded", format_money(rec.expenses))
    label_value("Sales minus expenses", format_money(rec.total_sales - rec.expenses), bold=True)
    label_value("Days with recorded trading", f"{rec.active_days} of {rec.days}")
    label_value("Transactions recorded", str(len(rec.entries)))
    if rec.active_days:
        label_value("Average sales per trading day", format_money(rec.total_sales // rec.active_days))

    heading("Credit given to customers (today)")
    label_value("Owed to the trader now", format_money(rec.outstanding), bold=True)
    label_value("Customers who owe", str(rec.debtors))
    repaid = rec.repaid_on_time + rec.repaid_late
    if repaid:
        pct = round(100 * rec.repaid_on_time / repaid)
        label_value("Customer debts paid by the due date (all history)", f"{pct}%  ({rec.repaid_on_time} of {repaid})")
    else:
        label_value("Customer debts paid by the due date (all history)", "No debts with due dates yet")

    table_style = FontFace(emphasis="BOLD", color=(255, 255, 255), fill_color=GREEN)
    zebra = dict(cell_fill_color=(246, 244, 238), cell_fill_mode="ROWS", borders_layout="HORIZONTAL_LINES")
    pdf.set_fill_color(255, 255, 255)
    pdf.set_draw_color(*RULE)
    if rec.weeks:
        heading("Week by week")
        pdf.set_font("DejaVu", "", 8.5)
        pdf.set_text_color(*INK)
        with pdf.table(col_widths=(30, 24, 24, 22), text_align=("LEFT", "RIGHT", "RIGHT", "RIGHT"),
                       headings_style=table_style, line_height=6, **zebra) as table:
            table.row(["Week starting", "Sales", "Collected", "Expenses"])
            for w in rec.weeks:
                table.row([_d(w.start), format_money(w.sales), format_money(w.collected), format_money(w.expenses)])

    heading("Transactions")
    pdf.set_font("DejaVu", "", 8)
    pdf.set_text_color(*INK)
    if not rec.entries:
        pdf.cell(width, 6, "No transactions in this period.", new_x="LMARGIN", new_y="NEXT")
    else:
        shown = rec.entries[-MAX_ROWS:]
        with pdf.table(col_widths=(17, 20, 22, 41, 20), text_align=("LEFT", "LEFT", "LEFT", "LEFT", "RIGHT"),
                       headings_style=table_style, line_height=5.2, **zebra) as table:
            table.row(["Date", "Type", "Customer", "Details", "Amount"])
            for e in shown:
                details = fmt.item_line(e) or ""
                if e.type == EntryType.CREDIT_SALE:
                    extra = []
                    if e.paid_now_kobo:
                        extra.append(f"paid {format_money(e.paid_now_kobo)} on the spot")
                    if e.due_date:
                        extra.append(f"due {_d(e.due_date)}")
                    details = "; ".join(x for x in [details, *extra] if x)
                table.row([to_local(e.created_at).strftime("%d %b"), TYPE_LABELS.get(e.type, e.type),
                           drawable(e.customer.display_name) if e.customer else "—", drawable(details),
                           format_money(e.amount_kobo)])
        if len(rec.entries) > len(shown):
            pdf.ln(1)
            pdf.cell(width, 5, f"Showing the latest {len(shown)} of {len(rec.entries)} transactions; totals cover all.",
                     new_x="LMARGIN", new_y="NEXT")

    pdf.ln(5)
    pdf.set_font("DejaVu", "", 7.5)
    pdf.set_text_color(*MUTED)
    pdf.multi_cell(width, 4, (
        "How this record was made: the trader logged each sale, payment and expense on WhatsApp (voice notes, "
        "texts or receipt photos) as it happened, and OwoBook keeps them in a ledger. Figures are self-reported "
        "by the trader and have not been audited. Cash sales and credit sales are counted when they were recorded; "
        "\"money collected\" is repayments and part-payments received from customers."
    ))
    return bytes(pdf.output())


def filename(rec: BusinessRecord) -> str:
    name = re.sub(r"[^0-9A-Za-z]+", "-", drawable(rec.trader.shop_name or rec.trader.name) or "trader").strip("-")[:40]
    return f"OwoBook-business-record-{name}-{rec.days}d-{rec.end:%Y-%m-%d}.pdf"
