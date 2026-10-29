"""Agent tools: JSON schemas the model sees, and the code that runs them.

Each tool returns a ToolResult with:
- `data`: a small JSON-able dict fed back to the model, and
- `reply`: the WhatsApp message composed by our code from the database (receipts, lists),
  so the amounts and names a trader sees are exactly what was saved.
"""

from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy.orm import Session

from owobook import ledger
from owobook.dates import PERIODS, format_date, resolve_due_date
from owobook.models import Trader
from owobook.money import format_naira, line_total_kobo, naira_to_kobo, parse_amount
from owobook.whatsapp import formatting as fmt
from owobook.whatsapp.formatting import Lang, Reply

Amount = float | str  # the model may send 45000 or "45k"; we convert in code


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
    amount_naira: Amount | None = Field(None, description="TOTAL price in naira (number, or as said e.g. '45k').")
    unit_price_naira: Amount | None = Field(
        None, description="Price PER UNIT when the trader says 'each'/'per bag'. Code multiplies by quantity."
    )
    paid: bool = Field(description="true = paid in full now (cash/transfer); false = all or part still owing.")
    amount_paid_naira: Amount | None = Field(
        None, description="Part-payment: money the customer paid ON THE SPOT for this sale, when some is still owing."
    )
    balance_naira: Amount | None = Field(
        None, description="Part-payment: what the customer still owes for this sale ('balance me 70k'), if the trader says it."
    )
    due_date: str | None = Field(
        None, description="When the customer will pay, exactly as said: 'Friday', 'tomorrow', 'end of month', or YYYY-MM-DD."
    )
    new_customer: bool = Field(False, description="true only if the trader confirmed this is a NEW customer.")


class RecordPayment(ToolArgs):
    """Record money a customer paid towards what they owe ("don pay", "paid", "settle")."""

    customer_name: str
    amount_naira: Amount = Field(description="Amount paid in naira (number, or as said e.g. '20k').")
    new_customer: bool = Field(False, description="true only if the trader confirmed this is a NEW customer.")


class RecordExpense(ToolArgs):
    """Record money the trader spent on the business (transport, stock, shop rent, etc.)."""

    description: str
    amount_naira: Amount


class GetCustomerBalance(ToolArgs):
    """How much one customer owes the trader."""

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
    amount_naira: Amount | None = Field(None, description="New TOTAL in naira.")
    unit_price_naira: Amount | None = Field(None, description="New price per unit; code multiplies by quantity.")
    amount_paid_naira: Amount | None = Field(None, description="New amount paid on the spot (credit sales).")
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
    """Save the trader's own name / shop name when they tell you, or the language OwoBook should reply in
    when they ask for one ("reply me in Yoruba", "speak Hausa", "English please")."""

    name: str | None = None
    shop_name: str | None = None
    language_pref: Literal["english", "pidgin", "yoruba", "hausa", "igbo"] | None = None


class SaveCustomerPhone(ToolArgs):
    """Save a customer's phone/WhatsApp number ("Tutu number na 0803 123 4567"), so their debt reminders
    come with a one-tap "Send to <name>" button. Pass the number exactly as said."""

    customer_name: str
    phone: str = Field(description="The number as said, e.g. '0803 123 4567' or '+234 803 123 4567'.")
    new_customer: bool = Field(False, description="true only if the trader confirmed this is a NEW customer.")


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
                f"{format_naira(paid_k)} + {format_naira(balance_k)} no be {format_naira(total)}. Wetin be the full price?"
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
    amount, paid_now = sale_amounts(a.amount_naira, a.unit_price_naira, a.quantity, a.amount_paid_naira, a.balance_naira)
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
    return ToolResult(
        data={
            "ok": True,
            "entry_id": entry.id,
            "type": entry.type,
            "amount": format_naira(entry.amount_kobo),
            "paid_now": format_naira(entry.paid_now_kobo) if entry.paid_now_kobo else None,
            "customer": entry.customer.display_name if entry.customer else None,
            "due_date": entry.due_date.isoformat() if entry.due_date else None,
            "customer_total_owed": format_naira(balance) if balance is not None else None,
        },
        reply=fmt.sale_receipt(entry, entry.customer, balance, ctx.lang, ctx.today),
        entry_id=entry.id,
    )


def _record_payment(ctx: AgentContext, a: RecordPayment) -> ToolResult:
    entry = ledger.record_payment(
        ctx.db,
        ctx.trader,
        customer_name=a.customer_name,
        amount_kobo=to_kobo(a.amount_naira),
        new_customer=a.new_customer,
        source=ctx.source,
        raw_text=ctx.raw_text,
    )
    balance = ledger.customer_balance(ctx.db, ctx.trader, entry.customer)
    return ToolResult(
        data={
            "ok": True,
            "entry_id": entry.id,
            "amount": format_naira(entry.amount_kobo),
            "customer": entry.customer.display_name,
            "remaining_balance": format_naira(balance),
        },
        reply=fmt.payment_receipt(entry, entry.customer, balance, ctx.lang),
        entry_id=entry.id,
    )


def _record_expense(ctx: AgentContext, a: RecordExpense) -> ToolResult:
    entry = ledger.record_expense(
        ctx.db,
        ctx.trader,
        description=a.description,
        amount_kobo=to_kobo(a.amount_naira),
        source=ctx.source,
        raw_text=ctx.raw_text,
    )
    return ToolResult(
        data={"ok": True, "entry_id": entry.id, "amount": format_naira(entry.amount_kobo)},
        reply=fmt.expense_receipt(entry, ctx.lang),
        entry_id=entry.id,
    )


def _get_customer_balance(ctx: AgentContext, a: GetCustomerBalance) -> ToolResult:
    customer = ledger.resolve_customer(ctx.db, ctx.trader, a.customer_name, create=False)
    debtor = ledger.debtor_for(ctx.db, ctx.trader, customer, ctx.today)
    return ToolResult(
        data={
            "customer": customer.display_name,
            "balance": format_naira(debtor.balance_kobo),
            "next_due": debtor.next_due.isoformat() if debtor.next_due else None,
            "overdue": debtor.overdue,
        },
        reply=fmt.balance_reply(debtor, ctx.lang, ctx.today),
    )


def _list_debtors(ctx: AgentContext, a: ListDebtors) -> ToolResult:
    debtors = ledger.list_debtors(ctx.db, ctx.trader, overdue_only=a.overdue_only, today=ctx.today)
    return ToolResult(
        data={
            "debtors": [
                {"name": d.customer.display_name, "owes": format_naira(d.balance_kobo), "overdue": d.overdue}
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
            "cash_sales": format_naira(s.cash_sales_kobo),
            "credit_sales": format_naira(s.credit_sales_kobo),
            "collected": format_naira(s.collected_kobo),
            "expenses": format_naira(s.expenses_kobo),
        },
        reply=fmt.summary_reply(s, ctx.lang),
    )


def _update_entry(ctx: AgentContext, a: UpdateEntry) -> ToolResult:
    entry = ledger.get_entry(ctx.db, ctx.trader, a.entry_id)
    quantity = a.quantity if a.quantity is not None else entry.quantity
    amount = _total_kobo(a.amount_naira, a.unit_price_naira, quantity)
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
        paid_now_kobo=to_kobo(a.amount_paid_naira),
        paid=a.paid,
        **fields,
    )
    balance = ledger.customer_balance(ctx.db, ctx.trader, entry.customer) if entry.customer else None
    return ToolResult(
        data={"ok": True, "entry_id": entry.id, "amount": format_naira(entry.amount_kobo)},
        reply=fmt.receipt_for(entry, balance, ctx.lang, ctx.today),
        entry_id=entry.id,
    )


def _delete_entry(ctx: AgentContext, a: DeleteEntry) -> ToolResult:
    entry = ledger.delete_entry(ctx.db, ctx.trader, a.entry_id)
    text = fmt.t(ctx.lang, "undone")
    if entry.customer:
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
    ctx.db.flush()
    lines = []
    if a.name or a.shop_name:
        who = f"{tr.name} ({tr.shop_name})" if tr.name and tr.shop_name else tr.name or tr.shop_name
        lines.append(fmt.t(ctx.lang, "profile_saved", name=who))
    if a.language_pref:
        lines.append(fmt.t(ctx.lang, "language_saved"))
    data = {"ok": True, "name": tr.name, "shop_name": tr.shop_name, "reply_language": ctx.lang}
    return ToolResult(data=data, reply=Reply("\n\n".join(lines)) if lines else None)


def _save_customer_phone(ctx: AgentContext, a: SaveCustomerPhone) -> ToolResult:
    customer = ledger.resolve_customer(ctx.db, ctx.trader, a.customer_name, create=True, new_customer=a.new_customer)
    ledger.set_customer_phone(ctx.db, ctx.trader, customer, a.phone)
    text = fmt.t(ctx.lang, "phone_saved", name=customer.display_name, phone=fmt.format_phone(customer.phone))
    return ToolResult(data={"ok": True, "customer": customer.display_name, "phone": customer.phone}, reply=Reply(text))


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
}


def describe_entry(entry, today: date) -> str:
    """One line describing an entry, for the model's context."""
    parts = [f"#{entry.id}", entry.type]
    if entry.customer:
        parts.append(entry.customer.display_name)
    if item := fmt.item_line(entry):
        parts.append(item)
    parts.append(format_naira(entry.amount_kobo))
    if entry.paid_now_kobo:
        parts.append(f"paid {format_naira(entry.paid_now_kobo)} on the spot")
    if entry.due_date:
        parts.append(f"due {format_date(entry.due_date, today)}")
    return " · ".join(parts)
