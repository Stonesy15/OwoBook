from decimal import Decimal

import pytest

from owobook.money import format_naira, kobo_to_naira, line_total_kobo, naira_to_kobo, parse_amount


@pytest.mark.parametrize(
    "naira, kobo",
    [(45000, 4_500_000), (0.5, 50), ("4500.505", 450_051), (Decimal("1.2"), 120), (0, 0)],
)
def test_naira_to_kobo(naira, kobo):
    assert naira_to_kobo(naira) == kobo


def test_naira_to_kobo_rejects_garbage():
    with pytest.raises(ValueError):
        naira_to_kobo("forty")


def test_kobo_to_naira():
    assert kobo_to_naira(4_500_050) == Decimal("45000.5")


@pytest.mark.parametrize(
    "kobo, text",
    [
        (4_500_000, "₦45,000"),
        (4_500_050, "₦45,000.50"),
        (0, "₦0"),
        (120_000_000, "₦1,200,000"),
        (-2_500_000, "-₦25,000"),
        (5, "₦0.05"),
    ],
)
def test_format_naira(kobo, text):
    assert format_naira(kobo) == text


def test_line_total_is_computed_in_code():
    # "3 bags at 15k each" → ₦45,000
    assert line_total_kobo(3, naira_to_kobo(15_000)) == 4_500_000
    assert line_total_kobo(2.5, naira_to_kobo(1_000)) == 250_000


@pytest.mark.parametrize(
    "text, naira",
    [
        ("45000", 45_000),
        ("45,000", 45_000),
        ("₦45,000", 45_000),
        ("#45,000", 45_000),
        ("N45,000", 45_000),
        ("n45k", 45_000),
        ("45k", 45_000),
        ("45K", 45_000),
        ("45 k", 45_000),
        ("4.5k", 4_500),
        ("45 thousand", 45_000),
        ("45 thousand naira", 45_000),
        ("1.2m", 1_200_000),
        ("2.5 million", 2_500_000),
        ("1 million 200 thousand", 1_200_000),
        ("fifteen thousand naira", 15_000),
        ("fifteen thousand", 15_000),
        ("forty-five thousand", 45_000),
        ("forty five k", 45_000),
        ("one hundred and fifty thousand", 150_000),
        ("two million five hundred thousand", 2_500_000),
        ("a thousand", 1_000),
        ("half a million", 500_000),
        ("five hundred naira", 500),
        ("500 naira only", 500),
        ("12k", 12_000),
        ("₦45,000.50", Decimal("45000.50")),
    ],
)
def test_parse_amount(text, naira):
    assert parse_amount(text) == naira_to_kobo(naira)


@pytest.mark.parametrize("text", ["", "   ", "k", "naira", "rice", "3 bags rice", "0", "plenty money"])
def test_parse_amount_refuses_non_amounts(text):
    assert parse_amount(text) is None
