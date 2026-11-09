"""Agent tools: JSON schemas the model sees, and the code that runs them.

Each tool returns a ToolResult with:
- `data`: a small JSON-able dict fed back to the model, and
- `reply`: the WhatsApp message composed by our code from the database (receipts, lists),
  so the amounts and names a trader sees are exactly what was saved.
"""

from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, Field, ValidationError, model_validator
from sqlalchemy.orm import Session

from owobook import business_record, currency, ledger, ledger_page, receipt
from owobook.dates import PERIODS, format_date, resolve_due_date, to_local, utcnow
from owobook.models import Trader
from owobook.money import format_money, line_total_kobo, naira_to_kobo, parse_amount
from owobook.whatsapp import formatting as fmt
from owobook.whatsapp.formatting import Lang, Reply

Amount = float | str  # the model may send 45000 or "45k"; we convert in code


def _alias(name: str) -> AliasChoices:
    """Accept the old "_naira" names too (amount_naira → amount), from before other currencies."""
    return AliasChoices(name, f"{name}_naira")


# ---------- argument schemas ---------------------------------------------------------------


class ToolArgs(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _drop_null_strings(cls, data: Any) -> Any:
        """Models sometimes send "null" (a string) for a field they meant to leave out."""
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if not _blank(v)}
        return data



class RecordSale(ToolArgs):
    """Record a sale. paid=true for cash sales; paid=false when the customer took goods on credit
    ("collect", "carry", "go pay", "will pay")."""

    customer_name: str | None = Field(None, description="Customer name as the trader said it. Required for credit sales.")
    item_description: str | None = Field(None, description="What was sold, e.g. 'rice'.")
    quantity: float | None = Field(None, description="Number of units, e.g. 3.")
    unit: str | None = Field(None, description="Unit, e.g. 'bags', 'crates'.")
    amount: Amount | None = Field(None, validation_alias=_alias("amount"), description="TOTAL price in the trader's currency (number, or as said e.g. '45k').")
    unit_price: Amount | None = Field(
        None, validation_alias=_alias("unit_price"), description="Price PER UNIT when the trader says 'each'/'per bag'. Code multiplies by quantity."
    )
    paid: bool = Field(description="true = paid in full now (cash/transfer); false = all or part still owing.")
    amount_paid: Amount | None = Field(
        None, validation_alias=_alias("amount_paid"), description="Part-payment: money the customer paid ON THE SPOT for this sale, when some is still owing."
    )
    balance: Amount | None = Field(
        None, validation_alias=_alias("balance"), description="Part-payment: what the customer still owes for this sale ('balance me 70k'), if the trader says it."
    )
    due_date: str | None = Field(
        None, description="When the customer will pay, exactly as said: 'Friday', 'tomorrow', 'end of month', or YYYY-MM-DD."
    )
    new_customer: bool = Field(False, description="true only if the trader confirmed this is a NEW customer.")


class RecordPayment(ToolArgs):
    """Record money a customer paid towards what they owe ("don pay", "paid", "settle")."""

    customer_name: str
    amount: Amount = Field(validation_alias=_alias("amount"), description="Amount paid in the trader's currency (number, or as said e.g. '20k').")
    new_customer: bool = Field(False, description="true only if the trader confirmed this is a NEW customer.")


class RecordExpense(ToolArgs):
    """Record money the trader spent on the business (transport, stock, shop rent, etc.)."""

    description: str
    amount: Amount = Field(validation_alias=_alias("amount"))


class GetCustomerBalance(ToolArgs):
    """How much one customer owes the trader, and how they've paid before (on time vs late). Also use it
    for "can I give X credit?" / "does X pay on time?" — report the facts, don't advise."""

    customer_name: str


class ListDebtors(ToolArgs):
    """List customers who owe money, biggest first."""

    overdue_only: bool = Field(False, description="true to show only overdue or due-today debts.")


class GetSalesSummary(ToolArgs):
    """Totals of cash sales, credit sales, money collected and expenses for a period."""

    period: Literal["today", "yesterday", "this_week", "last_week", "this_month", "last_month"] = "today"


class UpdateEntry(ToolArgs):
    """Correct an existing ledger entry. Only send the fields that change."""

    entry_id: int
    customer_name: str | None = None
    item_description: str | None = None
    quantity: float | None = None
    unit: str | None = None
    amount: Amount | None = Field(None, validation_alias=_alias("amount"), description="New TOTAL in the trader's currency.")
    unit_price: Amount | None = Field(None, validation_alias=_alias("unit_price"), description="New price per unit; code multiplies by quantity.")
    amount_paid: Amount | None = Field(None, validation_alias=_alias("amount_paid"), description="New amount paid on the spot (credit sales).")
    due_date: str | None = None
    paid: bool | None = None
    new_customer: bool = False


class DeleteEntry(ToolArgs):
    """Remove (undo) a ledger entry."""

    entry_id: int


class AskClarification(ToolArgs):
    """Ask the trader ONE short question when the amount, customer, or meaning is unclear. Never guess numbers."""

    question: str


class SaveProfile(ToolArgs):
    """Save the trader's own name / shop name when they tell you, the language OwoBook should reply in
    when they ask for one ("reply me in Yoruba", "speak Hausa", "English please"), or their currency when
    they say it ("my currency is reais", "use Kenyan shillings")."""

    name: str | None = None
    shop_name: str | None = None
    language_pref: Literal["english", "pidgin", "yoruba", "hausa", "igbo", "portuguese", "french", "hindi"] | None = None
    currency: str | None = Field(None, description="ISO code: NGN, GHS, KES, UGX, TZS, RWF, ZAR, XOF, XAF, INR, BRL, "
                                                   "AOA, MZN, MXN, PHP, USD or GBP.")


class SaveCustomerPhone(ToolArgs):
    """Save a customer's phone/WhatsApp number ("Tutu number na 0803 123 4567"), so their debt reminders
    come with a one-tap "Send to <name>" button. Pass the number exactly as said."""

    customer_name: str
    phone: str = Field(description="The number as said, e.g. '0803 123 4567' or '+234 803 123 4567'.")
    new_customer: bool = Field(False, description="true only if the trader confirmed this is a NEW customer.")


class MakeReceipt(ToolArgs):
    """Make a picture receipt the trader can forward to a customer ("send me receipt", "receipt for Mama Bisi",
    "give am receipt"). It shows what was bought or paid, the amount paid, the balance still owing and the due
    date. Uses the customer's latest sale or payment unless entry_id is given. If the same message also records a
    sale or payment, call that tool FIRST, then this one."""

    customer_name: str | None = Field(None, description="Whose receipt. Leave out for the latest sale/payment.")
    entry_id: int | None = Field(None, description="A specific entry from RECENT_ENTRIES, if the trader means one.")


class BusinessRecordArgs(ToolArgs):
    """Make the trader's business record: a PDF statement of sales, money collected, expenses, credit given and
    payment history for the last N days, to show a bank, microfinance bank or cooperative for a loan ("send me
    my business record", "statement for 90 days", "I need records for a loan")."""

    days: int = Field(30, description="How many days back: 30, 60 or 90 (default 30).")


class RecordPurchase(ToolArgs):
    """Record goods the TRADER bought for the shop (stock), from a supplier or market: "I buy 10 bags rice
    300k cash", "I collect 20 cartons indomie from Alhaji, I go pay him 150k Monday", "I take goods from
    Mama Nkechi". paid=false when the trader still owes the supplier. Not for customers buying from the trader."""

    supplier_name: str | None = Field(None, description="Who the trader bought from. Required when the trader still owes.")
    item_description: str | None = Field(None, description="What was bought, e.g. 'rice'.")
    quantity: float | None = Field(None, description="Number of units, e.g. 10. Goes into stock.")
    unit: str | None = Field(None, description="Unit, e.g. 'bags', 'cartons'.")
    amount: Amount | None = Field(None, validation_alias=_alias("amount"), description="TOTAL cost in the trader's currency.")
    unit_price: Amount | None = Field(None, validation_alias=_alias("unit_price"), description="Cost PER UNIT ('each'); code multiplies.")
    paid: bool = Field(description="true = the trader paid in full now; false = all or part still owed to the supplier.")
    amount_paid: Amount | None = Field(None, validation_alias=_alias("amount_paid"), description="Part-payment: what the trader paid the supplier now.")
    balance: Amount | None = Field(None, validation_alias=_alias("balance"), description="Part-payment: what the trader still owes for this.")
    due_date: str | None = Field(None, description="When the trader will pay the supplier, as said: 'Monday', 'end of month'.")
    new_supplier: bool = Field(False, description="true only if the trader confirmed this is a NEW person.")


class RecordSupplierPayment(ToolArgs):
    """Record money the TRADER paid a supplier towards what the trader owes them ("I don pay Alhaji 100k",
    "I settle Mama Nkechi 50k"). Not for customers paying the trader (that is record_payment)."""

    supplier_name: str
    amount: Amount = Field(validation_alias=_alias("amount"))
    new_supplier: bool = Field(False, description="true only if the trader confirmed this is a NEW person.")


class ListCreditors(ToolArgs):
    """List who the TRADER owes (suppliers), biggest first: "who I owe?", "how much I owe Alhaji?"."""

    due_only: bool = Field(False, description="true to show only what is due today or overdue.")


class UpdateStock(ToolArgs):
    """Change stock without a purchase: add goods ("I get 20 bags rice for shop", "add 5 cartons"), count what
    is left ("I get 17 bags rice remaining" → mode=set), or take out spoilt/used goods (mode=remove). Purchases
    with a cost go through record_purchase instead; sales take stock out by themselves."""

    item: str = Field(description="The item, e.g. 'rice'.")
    quantity: float
    unit: str | None = None
    mode: Literal["add", "set", "remove"] = "add"


class CheckStock(ToolArgs):
    """How much stock is left: one item ("how many bags rice remain?") or everything ("show my stock")."""

    item: str | None = Field(None, description="Leave out for all stock.")


class RecordSaving(ToolArgs):
    """Record the trader's savings: money put into ajo / esusu / adashe / a savings box or account ("I put 5k for
    ajo today"), or a payout collected back ("I collect my ajo 60k" → payout=true)."""

    amount: Amount = Field(validation_alias=_alias("amount"))
    group: str | None = Field(None, description="Which savings: 'ajo', 'esusu', 'adashe', 'savings box', 'bank'. Leave out if not said.")
    payout: bool = Field(False, description="true = money collected back out of the savings.")


class GetSavings(ToolArgs):
    """The trader's savings so far, per group (put in, collected, still in): "how much I don save?"."""


class SaveNote(ToolArgs):
    """Write down anything the trader wants remembered that is not a sale, payment, purchase or savings:
    "note say supplier go come Tuesday", "remember Mama Bisi like the big bags", "write down: shop rent due
    15th". Keep their words."""

    text: str = Field(description="The note, in the trader's own words (without 'note say' / 'remember').")


class FindNotes(ToolArgs):
    """Find the trader's notes: "what did I note about the supplier?", "show my notes"."""

    query: str | None = Field(None, description="Words to look for. Leave out for the latest notes.")


class DeleteNote(ToolArgs):
    """Remove a note ("delete that note"). Without note_id, the latest note is removed."""

    note_id: int | None = None


class LedgerLink(ToolArgs):
    """Send the trader a private link to a read-only web page of their ledger: who owes them, this week's sales
    and recent records ("show my ledger", "send me my book", "I want to see my records", "ledger link").
    Not for a single customer's balance (use get_customer_balance) or a PDF for a loan (use business_record)."""


TOOL_MODELS: dict[str, type[BaseModel]] = {
    "record_sale": RecordSale,
    "record_payment": RecordPayment,
    "record_expense": RecordExpense,
    "get_customer_balance": GetCustomerBalance,
    "list_debtors": ListDebtors,
    "get_sales_summary": GetSalesSummary,
    "update_entry": UpdateEntry,
    "delete_entry": DeleteEntry,
    "ask_clarification": AskClarification,
    "save_profile": SaveProfile,
    "save_customer_phone": SaveCustomerPhone,
    "make_receipt": MakeReceipt,
    "business_record": BusinessRecordArgs,
    "ledger_link": LedgerLink,
    "record_purchase": RecordPurchase,
    "record_supplier_payment": RecordSupplierPayment,
    "list_creditors": ListCreditors,
    "update_stock": UpdateStock,
    "check_stock": CheckStock,
    "record_saving": RecordSaving,
    "get_savings": GetSavings,
    "save_note": SaveNote,
    "find_notes": FindNotes,
    "delete_note": DeleteNote,
}


def _schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    schema.pop("title", None)
    schema.pop("description", None)
    for prop in schema.get("properties", {}).values():
        prop.pop("title", None)
    return schema


TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {"name": name, "description": (model.__doc__ or "").strip(), "parameters": _schema(model)},
    }
    for name, model in TOOL_MODELS.items()
]


# ---------- execution ----------------------------------------------------------------------


@dataclass
class AgentContext:
    db: Session
    trader: Trader
    lang: Lang
    today: date
    source: str = "text"
    raw_text: str | None = None


@dataclass
class ToolResult:
    data: dict[str, Any]
    reply: Reply | None = None
    is_error: bool = False  # the model should try again or ask the trader
    ends_turn: bool = False  # e.g. a clarifying question was sent
    entry_id: int | None = None


class ToolInputError(Exception):
    def __init__(self, question: str):
        super().__init__(question)
        self.question = question


def _blank(value: Any) -> bool:
    """Models sometimes send the string "null" (or "") for a field they meant to leave out."""
    return value is None or (isinstance(value, str) and value.strip().lower() in ("", "null", "none", "n/a"))


def to_kobo(value: Amount | None) -> int | None:
    """Model-supplied amount → kobo. Accepts 45000, '45000', '45k', 'forty five thousand'."""
    if _blank(value):
        return None
    if isinstance(value, (int, float)):
        kobo = naira_to_kobo(value)
    else:
        kobo = parse_amount(str(value))
    if kobo is None or kobo <= 0:
        raise ToolInputError(f"I no understand the amount '{value}'. How much?")
    return kobo


def _total_kobo(amount: Amount | None, unit_price: Amount | None, quantity: float | None) -> int | None:
    """Prefer quantity × unit price computed in code; fall back to the stated total."""
    unit_kobo = to_kobo(unit_price)
    if unit_kobo is not None:
        return line_total_kobo(quantity, unit_kobo) if quantity else unit_kobo
    return to_kobo(amount)


def sale_amounts(
    amount: Amount | None,
    unit_price: Amount | None,
    quantity: float | None,
    paid_now: Amount | None = None,
    balance: Amount | None = None,
) -> tuple[int | None, int]:
    """(total_kobo, paid_now_kobo) for a sale, computed in code from what the trader said.

    "gave me 60k, balance 70k" → total ₦130,000, paid ₦60,000. If the trader states all three
    numbers and they don't add up, we ask instead of picking one."""
    total = _total_kobo(amount, unit_price, quantity)
    paid_k = to_kobo(paid_now) or 0
    balance_k = to_kobo(balance)
    if balance_k is not None:
        if total is None:
            total = paid_k + balance_k
        elif not paid_k:
            paid_k = total - balance_k
        elif paid_k + balance_k != total:
            raise ToolInputError(
                f"{format_money(paid_k)} + {format_money(balance_k)} no be {format_money(total)}. Wetin be the full price?"
            )
        if paid_k < 0:
            raise ToolInputError("The balance pass the price. Wetin be the full price?")
    return total, paid_k


def _due(text: str | None, today: date) -> date | None:
    if _blank(text):
        return None
    d = resolve_due_date(text, today)
    if d is None:
        raise ToolInputError(f"I no understand the date '{text}'. When dem go pay?")
    return d


def _error(question: str) -> ToolResult:
    return ToolResult(data={"error": question, "question": question}, is_error=True)


def execute_tool(ctx: AgentContext, name: str, raw_args: dict[str, Any]) -> ToolResult:
    """Validate arguments and run one tool. Never raises for trader-fixable problems."""
    model = TOOL_MODELS.get(name)
    if not model:
        return _error(f"Unknown tool {name}")
    try:
        args = model.model_validate(raw_args or {})
        return _HANDLERS[name](ctx, args)
    except ValidationError as exc:
        return ToolResult(data={"error": f"Invalid arguments: {exc.errors(include_url=False)}"}, is_error=True)
    except ToolInputError as exc:
        return _error(exc.question)
    except ledger.AmbiguousCustomer as exc:
        result = _error(exc.question)
        result.data["candidates"] = [c.display_name for c in exc.candidates]
        return result
    except ledger.LedgerError as exc:
        result = _error(exc.question)
        result.data["error"] = str(exc)
        return result


def _record_sale(ctx: AgentContext, a: RecordSale) -> ToolResult:
    amount, paid_now = sale_amounts(a.amount, a.unit_price, a.quantity, a.amount_paid, a.balance)
    if amount is None:
        return _error("How much e cost?")
    paid = a.paid and not paid_now  # a part-payment means some is still owing
    entry = ledger.record_sale(
        ctx.db,
        ctx.trader,
        amount_kobo=amount,
        paid=paid,
        paid_now_kobo=paid_now,
        customer_name=a.customer_name,
        item_description=a.item_description,
        quantity=a.quantity,
        unit=a.unit,
        due_date=None if paid else _due(a.due_date, ctx.today),
        new_customer=a.new_customer,
        source=ctx.source,
        raw_text=ctx.raw_text,
    )
    balance = ledger.customer_balance(ctx.db, ctx.trader, entry.customer) if entry.customer else None
    notes, record = [], None
    if entry.customer and entry.type == "credit_sale":  # heads-up from how this customer paid before
        record = ledger.payment_record(ctx.db, ctx.trader, entry.customer, ctx.today, before_entry_id=entry.id)
        notes = fmt.trust_notes(entry.customer.display_name, record, ctx.lang, ctx.today)
    return ToolResult(
        data={
            "ok": True,
            "entry_id": entry.id,
            "type": entry.type,
            "payment_record": {"on_time": record.on_time, "late": record.late} if record else None,
            "amount": format_money(entry.amount_kobo),
            "paid_now": format_money(entry.paid_now_kobo) if entry.paid_now_kobo else None,
            "customer": entry.customer.display_name if entry.customer else None,
            "due_date": entry.due_date.isoformat() if entry.due_date else None,
            "customer_total_owed": format_money(balance) if balance is not None else None,
        },
        reply=fmt.sale_receipt(entry, entry.customer, balance, ctx.lang, ctx.today, notes,
                               ledger.stock_item_for(ctx.db, ctx.trader, entry)),
        entry_id=entry.id,
    )


def _record_payment(ctx: AgentContext, a: RecordPayment) -> ToolResult:
    entry = ledger.record_payment(
        ctx.db,
        ctx.trader,
        customer_name=a.customer_name,
        amount_kobo=to_kobo(a.amount),
        new_customer=a.new_customer,
        source=ctx.source,
        raw_text=ctx.raw_text,
    )
    balance = ledger.customer_balance(ctx.db, ctx.trader, entry.customer)
    return ToolResult(
        data={
            "ok": True,
            "entry_id": entry.id,
            "amount": format_money(entry.amount_kobo),
            "customer": entry.customer.display_name,
            "remaining_balance": format_money(balance),
        },
        reply=fmt.payment_receipt(entry, entry.customer, balance, ctx.lang),
        entry_id=entry.id,
    )


def _record_expense(ctx: AgentContext, a: RecordExpense) -> ToolResult:
    entry = ledger.record_expense(
        ctx.db,
        ctx.trader,
        description=a.description,
        amount_kobo=to_kobo(a.amount),
        source=ctx.source,
        raw_text=ctx.raw_text,
    )
    return ToolResult(
        data={"ok": True, "entry_id": entry.id, "amount": format_money(entry.amount_kobo)},
        reply=fmt.expense_receipt(entry, ctx.lang),
        entry_id=entry.id,
    )


def _get_customer_balance(ctx: AgentContext, a: GetCustomerBalance) -> ToolResult:
    customer = ledger.resolve_customer(ctx.db, ctx.trader, a.customer_name, create=False)
    debtor = ledger.debtor_for(ctx.db, ctx.trader, customer, ctx.today)
    record = ledger.payment_record(ctx.db, ctx.trader, customer, ctx.today)
    return ToolResult(
        data={
            "customer": customer.display_name,
            "balance": format_money(debtor.balance_kobo),
            "next_due": debtor.next_due.isoformat() if debtor.next_due else None,
            "overdue": debtor.overdue,
            "paid_on_time": record.on_time,
            "paid_late": record.late,
        },
        reply=_with_owed(ctx, customer, fmt.balance_reply(debtor, ctx.lang, ctx.today, record)),
    )


def _with_owed(ctx: AgentContext, person, reply: Reply) -> Reply:
    """Add what the trader owes this person, when they are also a supplier."""
    owed = ledger.supplier_balance(ctx.db, ctx.trader, person)
    if owed > 0:
        reply.text += "\n" + fmt.t(ctx.lang, "bal_you_owe", name=person.display_name, amount=format_money(owed))
    return reply


def _list_debtors(ctx: AgentContext, a: ListDebtors) -> ToolResult:
    debtors = ledger.list_debtors(ctx.db, ctx.trader, overdue_only=a.overdue_only, today=ctx.today)
    return ToolResult(
        data={
            "debtors": [
                {"name": d.customer.display_name, "owes": format_money(d.balance_kobo), "overdue": d.overdue}
                for d in debtors
            ]
        },
        reply=fmt.debtors_reply(debtors, ctx.lang, overdue_only=a.overdue_only, today=ctx.today),
    )


def _get_sales_summary(ctx: AgentContext, a: GetSalesSummary) -> ToolResult:
    assert a.period in PERIODS
    s = ledger.sales_summary(ctx.db, ctx.trader, a.period, ctx.today)
    return ToolResult(
        data={
            "period": a.period,
            "cash_sales": format_money(s.cash_sales_kobo),
            "credit_sales": format_money(s.credit_sales_kobo),
            "collected": format_money(s.collected_kobo),
            "expenses": format_money(s.expenses_kobo),
        },
        reply=fmt.summary_reply(s, ctx.lang),
    )


def _update_entry(ctx: AgentContext, a: UpdateEntry) -> ToolResult:
    entry = ledger.get_entry(ctx.db, ctx.trader, a.entry_id)
    quantity = a.quantity if a.quantity is not None else entry.quantity
    amount = _total_kobo(a.amount, a.unit_price, quantity)
    if amount is None and a.quantity is not None and entry.quantity:
        # "no, na 3 cartons": keep the saved price per unit and recompute the total in code.
        amount = line_total_kobo(a.quantity, round(entry.amount_kobo / entry.quantity))
    fields: dict[str, Any] = {}
    if a.due_date is not None:
        fields["due_date"] = _due(a.due_date, ctx.today)
    entry = ledger.update_entry(
        ctx.db,
        ctx.trader,
        a.entry_id,
        customer_name=a.customer_name,
        new_customer=a.new_customer,
        item_description=a.item_description,
        quantity=a.quantity,
        unit=a.unit,
        amount_kobo=amount,
        paid_now_kobo=to_kobo(a.amount_paid),
        paid=a.paid,
        **fields,
    )
    return ToolResult(
        data={"ok": True, "entry_id": entry.id, "amount": format_money(entry.amount_kobo)},
        reply=receipt_after_edit(ctx, entry),
        entry_id=entry.id,
    )


def receipt_after_edit(ctx: AgentContext, entry) -> Reply:
    """The right receipt for a just-changed entry of any type, with balances from the database."""
    if entry.type in ("purchase", "supplier_payment"):
        balance = ledger.supplier_balance(ctx.db, ctx.trader, entry.customer) if entry.customer else None
    else:
        balance = ledger.customer_balance(ctx.db, ctx.trader, entry.customer) if entry.customer else None
    pot = _pot_for(ctx, entry.item_description) if entry.type in ("saving", "saving_payout") else None
    return fmt.receipt_for(entry, balance, ctx.lang, ctx.today, ledger.stock_item_for(ctx.db, ctx.trader, entry), pot)


def _delete_entry(ctx: AgentContext, a: DeleteEntry) -> ToolResult:
    entry = ledger.delete_entry(ctx.db, ctx.trader, a.entry_id)
    text = fmt.t(ctx.lang, "undone")
    if entry.customer and entry.type in ("purchase", "supplier_payment"):
        owed = ledger.supplier_balance(ctx.db, ctx.trader, entry.customer)
        text += "\n" + fmt.owe_sentence(entry.customer.display_name, owed, ctx.lang)
    elif entry.customer:
        balance = ledger.customer_balance(ctx.db, ctx.trader, entry.customer)
        text += "\n" + fmt.balance_sentence(entry.customer.display_name, balance, ctx.lang)
    return ToolResult(data={"ok": True, "deleted_entry_id": entry.id}, reply=Reply(text))


def _ask_clarification(ctx: AgentContext, a: AskClarification) -> ToolResult:
    return ToolResult(data={"asked": a.question}, reply=Reply(a.question), ends_turn=True)


def _save_profile(ctx: AgentContext, a: SaveProfile) -> ToolResult:
    tr = ctx.trader
    if a.name:
        tr.name = a.name.strip()
    if a.shop_name:
        tr.shop_name = a.shop_name.strip()
    if a.language_pref:
        tr.language_pref = a.language_pref
        ctx.lang = fmt.reply_language(a.language_pref)  # this reply and the rest of the turn use it
    code = (a.currency or "").strip().upper()
    if code and (code not in currency.CURRENCIES or code == "XXX"):
        raise ToolInputError(f"I don't know the currency '{a.currency}'. Which one?")
    if code:
        tr.currency = code
        currency.set_current(code)
    ctx.db.flush()
    lines = []
    if a.name or a.shop_name:
        who = f"{tr.name} ({tr.shop_name})" if tr.name and tr.shop_name else tr.name or tr.shop_name
        lines.append(fmt.t(ctx.lang, "profile_saved", name=who))
    if a.language_pref:
        lines.append(fmt.t(ctx.lang, "language_saved"))
    if code:
        cur = currency.get(code)
        lines.append(fmt.t(ctx.lang, "currency_saved", currency=f"{cur.code} ({cur.symbol})"))
    data = {"ok": True, "name": tr.name, "shop_name": tr.shop_name, "reply_language": ctx.lang, "currency": tr.currency}
    return ToolResult(data=data, reply=Reply("\n\n".join(lines)) if lines else None)


def _save_customer_phone(ctx: AgentContext, a: SaveCustomerPhone) -> ToolResult:
    customer = ledger.resolve_customer(ctx.db, ctx.trader, a.customer_name, create=True, new_customer=a.new_customer)
    ledger.set_customer_phone(ctx.db, ctx.trader, customer, a.phone)
    text = fmt.t(ctx.lang, "phone_saved", name=customer.display_name, phone=fmt.format_phone(customer.phone))
    return ToolResult(data={"ok": True, "customer": customer.display_name, "phone": customer.phone}, reply=Reply(text))


def _make_receipt(ctx: AgentContext, a: MakeReceipt) -> ToolResult:
    if a.entry_id is not None:
        entry = ledger.get_entry(ctx.db, ctx.trader, a.entry_id)
    else:
        customer = ledger.resolve_customer(ctx.db, ctx.trader, a.customer_name, create=False) if a.customer_name else None
        entry = ledger.latest_receipt_entry(ctx.db, ctx.trader, customer)
    if entry is None or entry.status == "deleted" or entry.type not in ledger.RECEIPT_TYPES:
        return ToolResult(data={"error": "nothing to make a receipt for"}, reply=Reply(fmt.t(ctx.lang, "receipt_none")),
                          ends_turn=True)
    info = receipt.build_receipt(ctx.db, ctx.trader, entry)
    caption = fmt.t(ctx.lang, "receipt_caption", name=info.customer or "your customer")
    return ToolResult(
        data={"ok": True, "receipt_no": info.number, "entry_id": entry.id, "customer": info.customer,
              "balance_owing": format_money(info.balance_kobo), "sent_as": "picture"},
        reply=Reply(caption, image=receipt.render_png(info, ctx.lang)),
        entry_id=entry.id,
    )


def _business_record(ctx: AgentContext, a: BusinessRecordArgs) -> ToolResult:
    days = min(max(a.days, 7), 365)
    rec = business_record.build_record(ctx.db, ctx.trader, days, ctx.today)
    if not rec.entries:
        return ToolResult(data={"error": "no entries in that period"}, reply=Reply(fmt.t(ctx.lang, "record_empty", days=days)),
                          ends_turn=True)
    pdf = business_record.render_pdf(rec, to_local(utcnow()))
    return ToolResult(
        data={"ok": True, "days": days, "sent_as": "pdf", "total_sales": format_money(rec.total_sales),
              "transactions": len(rec.entries)},
        reply=Reply(fmt.t(ctx.lang, "record_caption", days=days), document=pdf, filename=business_record.filename(rec)),
    )


def _ledger_link(ctx: AgentContext, a: LedgerLink) -> ToolResult:
    url = ledger_page.link_for(ctx.trader)
    if url is None:
        return ToolResult(data={"error": "ledger links are not set up on this server"},
                          reply=Reply(fmt.t(ctx.lang, "ledger_link_off")), ends_turn=True)
    return ToolResult(data={"ok": True, "sent_as": "link", "valid_days": ledger_page.LINK_DAYS},
                      reply=Reply(fmt.t(ctx.lang, "ledger_link", url=url, days=ledger_page.LINK_DAYS)))


def _record_purchase(ctx: AgentContext, a: RecordPurchase) -> ToolResult:
    amount, paid_now = sale_amounts(a.amount, a.unit_price, a.quantity, a.amount_paid, a.balance)
    if amount is None:
        return _error("How much you buy am?")
    if a.paid and not paid_now:
        paid_now = amount
    entry = ledger.record_purchase(
        ctx.db, ctx.trader, amount_kobo=amount, paid_now_kobo=paid_now, supplier_name=a.supplier_name,
        item_description=a.item_description, quantity=a.quantity, unit=a.unit,
        due_date=None if paid_now >= amount else _due(a.due_date, ctx.today), new_supplier=a.new_supplier,
        source=ctx.source, raw_text=ctx.raw_text,
    )
    owed = ledger.supplier_balance(ctx.db, ctx.trader, entry.customer) if entry.customer else None
    stock = ledger.stock_item_for(ctx.db, ctx.trader, entry)
    return ToolResult(
        data={"ok": True, "entry_id": entry.id, "type": "purchase", "amount": format_money(entry.amount_kobo),
              "supplier": entry.customer.display_name if entry.customer else None,
              "trader_owes_supplier": format_money(owed) if owed is not None else None,
              "stock_now": f"{stock.quantity:g} {stock.unit or ''}".strip() if stock else None},
        reply=fmt.purchase_receipt(entry, owed, ctx.lang, ctx.today, stock),
        entry_id=entry.id,
    )


def _record_supplier_payment(ctx: AgentContext, a: RecordSupplierPayment) -> ToolResult:
    entry = ledger.record_supplier_payment(
        ctx.db, ctx.trader, supplier_name=a.supplier_name, amount_kobo=to_kobo(a.amount),
        new_supplier=a.new_supplier, source=ctx.source, raw_text=ctx.raw_text,
    )
    owed = ledger.supplier_balance(ctx.db, ctx.trader, entry.customer)
    return ToolResult(
        data={"ok": True, "entry_id": entry.id, "amount": format_money(entry.amount_kobo),
              "supplier": entry.customer.display_name, "trader_still_owes": format_money(owed)},
        reply=fmt.supplier_payment_receipt(entry, owed, ctx.lang),
        entry_id=entry.id,
    )


def _list_creditors(ctx: AgentContext, a: ListCreditors) -> ToolResult:
    creditors = ledger.list_creditors(ctx.db, ctx.trader, due_only=a.due_only, today=ctx.today)
    return ToolResult(
        data={"trader_owes": [{"name": c.supplier.display_name, "owed": format_money(c.owed_kobo), "overdue": c.overdue}
                              for c in creditors]},
        reply=fmt.creditors_reply(creditors, ctx.lang, ctx.today),
    )


def _update_stock(ctx: AgentContext, a: UpdateStock) -> ToolResult:
    item, change = ledger.change_stock(ctx.db, ctx.trader, a.item, a.quantity, unit=a.unit, mode=a.mode)
    text = fmt.t(ctx.lang, "stock_updated") + "\n\n" + fmt.stock_line(item, ctx.lang)
    return ToolResult(data={"ok": True, "item": item.name, "change": change, "now": item.quantity, "unit": item.unit},
                      reply=Reply(text))


def _check_stock(ctx: AgentContext, a: CheckStock) -> ToolResult:
    if a.item:
        item = ledger.find_stock_item(ctx.db, ctx.trader, a.item)
        if item is None:
            return ToolResult(data={"item": a.item, "tracked": False},
                              reply=Reply(fmt.t(ctx.lang, "stock_unknown", item=a.item)), ends_turn=True)
        return ToolResult(data={"item": item.name, "quantity": item.quantity, "unit": item.unit},
                          reply=Reply(fmt.stock_line(item, ctx.lang)))
    items = ledger.list_stock(ctx.db, ctx.trader)
    return ToolResult(data={"items": [{"item": i.name, "quantity": i.quantity, "unit": i.unit} for i in items]},
                      reply=fmt.stock_reply(items, ctx.lang))


def _pot_for(ctx: AgentContext, group: str | None):
    key = ledger.normalize_name(group or "savings") or "savings"
    return next((p for p in ledger.savings_summary(ctx.db, ctx.trader) if ledger.normalize_name(p.name) == key), None)


def _record_saving(ctx: AgentContext, a: RecordSaving) -> ToolResult:
    entry = ledger.record_saving(ctx.db, ctx.trader, amount_kobo=to_kobo(a.amount), group=a.group, payout=a.payout,
                                 source=ctx.source, raw_text=ctx.raw_text)
    pot = _pot_for(ctx, entry.item_description)
    return ToolResult(
        data={"ok": True, "entry_id": entry.id, "type": entry.type, "amount": format_money(entry.amount_kobo),
              "group": entry.item_description, "still_saved": format_money(pot.balance_kobo) if pot else None},
        reply=fmt.saving_receipt(entry, pot, ctx.lang),
        entry_id=entry.id,
    )


def _get_savings(ctx: AgentContext, a: GetSavings) -> ToolResult:
    pots = ledger.savings_summary(ctx.db, ctx.trader)
    return ToolResult(
        data={"savings": [{"group": p.name, "saved": format_money(p.saved_kobo), "collected": format_money(p.collected_kobo),
                           "still_in": format_money(p.balance_kobo)} for p in pots]},
        reply=fmt.savings_reply(pots, ctx.lang),
    )


def _save_note(ctx: AgentContext, a: SaveNote) -> ToolResult:
    note = ledger.save_note(ctx.db, ctx.trader, a.text, source=ctx.source)
    return ToolResult(data={"ok": True, "note_id": note.id}, reply=Reply(fmt.t(ctx.lang, "note_saved", text=note.text)))


def _find_notes(ctx: AgentContext, a: FindNotes) -> ToolResult:
    notes = ledger.find_notes(ctx.db, ctx.trader, a.query)
    return ToolResult(data={"notes": [{"id": n.id, "text": n.text} for n in notes]},
                      reply=fmt.notes_reply(notes, ctx.lang, ctx.today))


def _delete_note(ctx: AgentContext, a: DeleteNote) -> ToolResult:
    note = ledger.delete_note(ctx.db, ctx.trader, a.note_id)
    return ToolResult(data={"ok": True, "deleted_note_id": note.id},
                      reply=Reply(fmt.t(ctx.lang, "note_deleted", text=note.text)))


_HANDLERS = {
    "record_sale": _record_sale,
    "record_payment": _record_payment,
    "record_expense": _record_expense,
    "get_customer_balance": _get_customer_balance,
    "list_debtors": _list_debtors,
    "get_sales_summary": _get_sales_summary,
    "update_entry": _update_entry,
    "delete_entry": _delete_entry,
    "ask_clarification": _ask_clarification,
    "save_profile": _save_profile,
    "save_customer_phone": _save_customer_phone,
    "make_receipt": _make_receipt,
    "business_record": _business_record,
    "ledger_link": _ledger_link,
    "record_purchase": _record_purchase,
    "record_supplier_payment": _record_supplier_payment,
    "list_creditors": _list_creditors,
    "update_stock": _update_stock,
    "check_stock": _check_stock,
    "record_saving": _record_saving,
    "get_savings": _get_savings,
    "save_note": _save_note,
    "find_notes": _find_notes,
    "delete_note": _delete_note,
}


def describe_entry(entry, today: date) -> str:
    """One line describing an entry, for the model's context."""
    parts = [f"#{entry.id}", entry.type]
    if entry.customer:
        parts.append(entry.customer.display_name)
    if item := fmt.item_line(entry):
        parts.append(item)
    parts.append(format_money(entry.amount_kobo))
    if entry.paid_now_kobo:
        parts.append(f"paid {format_money(entry.paid_now_kobo)} on the spot")
    if entry.due_date:
        parts.append(f"due {format_date(entry.due_date, today)}")
    return " · ".join(parts)
