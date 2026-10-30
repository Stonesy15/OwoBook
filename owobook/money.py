"""Money helpers. Everything is stored as integer minor units — kobo for naira (₦1 = 100 kobo), cents,
paise or centavos for other currencies — never floats. Names say "kobo"/"naira" for history.

The LLM gives us amounts in the main unit; these helpers turn them into minor units, parse
spoken/written amounts ("45k", "4.5k", "1.2m", "fifteen thousand naira", "1.5 lakh", "50 mil reais"),
and format them in the current trader's currency ("₦45,000", "KSh 45,000", "R$ 45.000").
"""

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from owobook import currency

KOBO_PER_NAIRA = 100

_ONES = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_SCALES = {
    "k": 1_000, "thousand": 1_000, "thousands": 1_000,
    "m": 1_000_000, "mil": 1_000_000, "million": 1_000_000, "millions": 1_000_000,
    "b": 1_000_000_000, "billion": 1_000_000_000,
    "mille": 1_000,  # French
    "lakh": 100_000, "lakhs": 100_000, "lac": 100_000, "crore": 10_000_000, "crores": 10_000_000,  # India
}
# Words that can surround an amount without changing it (plus every currency's own words).
_FILLER = {"naira", "n", "ngn", "only", "and", "of"} | currency.all_words()
_SYMBOLS = ("₦", "#", "₹", "₵", "₱", "$", "£", "€")


def naira_to_kobo(naira: int | float | str | Decimal) -> int:
    """Convert a naira amount to integer kobo, rounding half-up to the nearest kobo."""
    try:
        value = Decimal(str(naira))
    except InvalidOperation as exc:
        raise ValueError(f"Not a number: {naira!r}") from exc
    return int((value * KOBO_PER_NAIRA).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def kobo_to_naira(kobo: int) -> Decimal:
    return Decimal(kobo) / KOBO_PER_NAIRA


def format_money(kobo: int) -> str:
    """Minor units → the current trader's money: 4500000 → '₦45,000' / 'KSh 45,000' / 'R$ 45.000' /
    '45 000 FCFA'; 4500050 → '₦45,000.50'."""
    cur = currency.current()
    sign = "-" if kobo < 0 else ""
    major, rem = divmod(abs(kobo), KOBO_PER_NAIRA)
    number = f"{major:,}".replace(",", cur.thousands)
    if rem:
        number += f"{cur.decimal}{rem:02d}"
    if not cur.symbol:
        return sign + number
    if cur.suffix:
        return f"{sign}{number} {cur.symbol}"
    return f"{sign}{cur.symbol}{' ' if cur.space else ''}{number}"


def line_total_kobo(quantity: int | float | Decimal, unit_price_kobo: int) -> int:
    """Quantity × unit price, computed in code (not by the model). '3 bags at 15k each' → ₦45,000."""
    total = Decimal(str(quantity)) * unit_price_kobo
    return int(total.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def parse_amount(text: str) -> int | None:
    """Parse a spoken or written amount into kobo, or return None if it isn't one.

    Handles: "45000", "45,000", "₦45,000", "#45,000", "N45k", "45k", "4.5k", "45 thousand",
    "1.2m", "2.5 million", "fifteen thousand naira", "one hundred and fifty thousand",
    "half a million", "forty-five k".
    """
    if not text:
        return None
    s = text.lower().strip()
    for symbol in _SYMBOLS:
        s = s.replace(symbol, " ")
    s = re.sub(r"\b(?:ksh|kshs|ush|tsh|gh|r|rs|kz|mx)\.?(?=\s*\d)", " ", s)  # KSh 45,000 / R 500 / Rs. 2000
    s = s.replace("-", " ")
    if currency.current().thousands == ".":
        s = re.sub(r"(?<=\d)\.(?=\d{3}\b)", "", s)  # R$ 45.000 → 45000
    s = re.sub(r"(?<=\d)[, ](?=\d{3}\b)", "", s)    # 45,000 / 45 000 → 45000
    s = re.sub(r"\bn(?=\d)", " ", s)               # N45000 → 45000
    s = re.sub(r"(?<=\d)(?=[a-z])", " ", s)        # 45k → 45 k

    tokens = re.findall(r"\d+(?:\.\d+)?|[a-z]+", s)
    if not tokens:
        return None

    total = Decimal(0)
    current = Decimal(0)
    saw_number = False

    for tok in tokens:
        if tok[0].isdigit():
            current += Decimal(tok)
            saw_number = True
        elif tok in _ONES:
            current += _ONES[tok]
            saw_number = True
        elif tok in _TENS:
            current += _TENS[tok]
            saw_number = True
        elif tok == "half":
            current += Decimal("0.5")
            saw_number = True
        elif tok == "a":
            if current == 0:  # "a thousand" = 1000, but "half a million" keeps 0.5
                current = Decimal(1)
                saw_number = True
        elif tok == "hundred":
            current = (current or 1) * 100
            saw_number = True
        elif tok == "mil" and currency.current().thousand_mil:  # Portuguese/Spanish: "50 mil" = 50,000
            total += (current or 1) * 1_000
            current = Decimal(0)
        elif tok in _SCALES:
            if not saw_number:
                return None  # a bare "k" or "m" is not an amount
            total += (current or 1) * _SCALES[tok]
            current = Decimal(0)
        elif tok in _FILLER:
            continue
        else:
            return None  # unknown word: refuse rather than guess

    if not saw_number:
        return None
    kobo = naira_to_kobo(total + current)
    return kobo if kobo > 0 else None
