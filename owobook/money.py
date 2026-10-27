"""Money helpers. Everything is stored as integer kobo (₦1 = 100 kobo) — never floats.

The LLM gives us amounts in naira; these helpers turn them into kobo, parse
spoken/written amounts ("45k", "4.5k", "1.2m", "fifteen thousand naira"),
and format kobo for display ("₦45,000").
"""

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

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
}
# Words that can surround an amount without changing it.
_FILLER = {"naira", "n", "ngn", "only", "and", "of"}


def naira_to_kobo(naira: int | float | str | Decimal) -> int:
    """Convert a naira amount to integer kobo, rounding half-up to the nearest kobo."""
    try:
        value = Decimal(str(naira))
    except InvalidOperation as exc:
        raise ValueError(f"Not a number: {naira!r}") from exc
    return int((value * KOBO_PER_NAIRA).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def kobo_to_naira(kobo: int) -> Decimal:
    return Decimal(kobo) / KOBO_PER_NAIRA


def format_naira(kobo: int) -> str:
    """Format kobo for traders: 4500000 → '₦45,000'; 4500050 → '₦45,000.50'."""
    sign = "-" if kobo < 0 else ""
    naira, rem = divmod(abs(kobo), KOBO_PER_NAIRA)
    text = f"₦{naira:,}"
    if rem:
        text += f".{rem:02d}"
    return sign + text


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
    s = s.replace("₦", " ").replace("#", " ").replace("-", " ")
    s = re.sub(r"(?<=\d),(?=\d)", "", s)          # 45,000 → 45000
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
