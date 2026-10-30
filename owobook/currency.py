"""Each trader's currency, from their WhatsApp number's country code: +234 → ₦, +254 → KSh, +91 → ₹, +55 → R$.

Amounts are stored as integer minor units (kobo, cents, paise, centavos — 1/100 of the main unit; the DB
columns keep the name `*_kobo`). Formatting follows the trader currently being served: `use_currency()` sets
it per message (and per trader in the reminder job), so receipts and lists show the trader's own money.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class Currency:
    code: str
    symbol: str
    thousands: str = ","
    decimal: str = "."
    space: bool = False  # "KSh 45,000" vs "₦45,000"
    suffix: bool = False  # "45 000 FCFA"
    words: tuple[str, ...] = ()  # what traders say, so "45k shillings" still parses
    thousand_mil: bool = False  # Portuguese/Spanish "50 mil" = 50,000 (not 50 million)


CURRENCIES = {c.code: c for c in [
    Currency("NGN", "₦", words=("naira", "ngn")),
    Currency("GHS", "GH₵", words=("cedis", "cedi", "ghs", "ghc")),
    Currency("KES", "KSh", space=True, words=("shillings", "shilling", "bob", "ksh", "kes", "kshs")),
    Currency("UGX", "USh", space=True, words=("shillings", "shilling", "ugx", "ush")),
    Currency("TZS", "TSh", space=True, words=("shillings", "shilling", "tzs", "tsh")),
    Currency("RWF", "RWF", space=True, words=("francs", "franc", "rwf", "frw")),
    Currency("ZAR", "R", space=True, words=("rand", "zar")),
    Currency("XOF", "FCFA", thousands=" ", decimal=",", space=True, suffix=True,
             words=("francs", "franc", "cfa", "fcfa", "xof")),
    Currency("XAF", "FCFA", thousands=" ", decimal=",", space=True, suffix=True,
             words=("francs", "franc", "cfa", "fcfa", "xaf")),
    Currency("INR", "₹", words=("rupees", "rupee", "rupaye", "rupaiye", "rs", "inr")),
    Currency("BRL", "R$", thousands=".", decimal=",", space=True, words=("reais", "real", "brl"), thousand_mil=True),
    Currency("AOA", "Kz", thousands=".", decimal=",", space=True, words=("kwanzas", "kwanza", "aoa", "kz"),
             thousand_mil=True),
    Currency("MZN", "MT", thousands=".", decimal=",", space=True, suffix=True, words=("meticais", "metical", "mzn", "mt"),
             thousand_mil=True),
    Currency("MXN", "MX$", space=True, words=("pesos", "peso", "mxn"), thousand_mil=True),
    Currency("PHP", "₱", words=("pesos", "peso", "php")),
    Currency("USD", "$", words=("dollars", "dollar", "usd")),
    Currency("GBP", "£", words=("pounds", "pound", "gbp")),
    Currency("XXX", "", words=()),  # unknown country: plain numbers, no symbol
]}

# Calling code → currency. Longest prefix wins (so +225 isn't read as +22…).
COUNTRY_CURRENCY = {
    "234": "NGN", "233": "GHS", "254": "KES", "256": "UGX", "255": "TZS", "250": "RWF", "27": "ZAR",
    # West & Central African CFA franc zones
    "225": "XOF", "221": "XOF", "223": "XOF", "226": "XOF", "227": "XOF", "228": "XOF", "229": "XOF", "245": "XOF",
    "237": "XAF", "241": "XAF", "242": "XAF", "235": "XAF", "236": "XAF", "240": "XAF",
    "91": "INR", "55": "BRL", "244": "AOA", "258": "MZN", "52": "MXN", "63": "PHP", "1": "USD", "44": "GBP",
}

DEFAULT = "NGN"
_current: ContextVar[str] = ContextVar("currency", default=DEFAULT)


def currency_for_wa_id(wa_id: str | None) -> str:
    digits = "".join(ch for ch in (wa_id or "") if ch.isdigit())
    for length in (3, 2, 1):
        if code := COUNTRY_CURRENCY.get(digits[:length]):
            return code
    return "XXX"


def calling_code(wa_id: str | None) -> str:
    """'2348031234567' → '234'; used for customers' local phone numbers."""
    digits = "".join(ch for ch in (wa_id or "") if ch.isdigit())
    for length in (3, 2, 1):
        if digits[:length] in COUNTRY_CURRENCY:
            return digits[:length]
    return "234"


def get(code: str | None = None) -> Currency:
    return CURRENCIES.get(code or _current.get(), CURRENCIES[DEFAULT])


def current() -> Currency:
    return get(_current.get())


def set_current(code: str | None) -> None:
    """Serve this trader's currency for the rest of the current message (each message sets it afresh)."""
    _current.set(code if code in CURRENCIES else DEFAULT)


@contextmanager
def use_currency(code: str | None):
    """Format money in this currency inside the block (per message / per trader)."""
    token = _current.set(code if code in CURRENCIES else DEFAULT)
    try:
        yield
    finally:
        _current.reset(token)


def all_words() -> set[str]:
    return {w for c in CURRENCIES.values() for w in c.words}
