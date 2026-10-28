from owobook.whatsapp.formatting import detect_language, parse_button_id, render_plain, Reply, entry_buttons


def test_detect_language():
    assert detect_language("Mama Bisi don pay 20k") == "pidgin"
    assert detect_language("Who dey owe me?") == "pidgin"
    assert detect_language("no, na 4 bags") == "pidgin"
    assert detect_language("Mama Bisi paid 20k") == "english"
    assert detect_language("Mama Bisi don pay 20k", preference="english") == "english"


def test_button_ids_round_trip():
    buttons = entry_buttons(12)
    assert [parse_button_id(b.id) for b in buttons] == [("confirm", 12), ("fix", 12), ("undo", 12)]
    assert parse_button_id("confirm:abc") is None
    assert parse_button_id("hack:1") is None


def test_render_plain_numbers_buttons():
    text = render_plain(Reply("hi", entry_buttons(1)))
    assert "[1] Correct ✓" in text and "[3] Undo ↩️" in text


def test_item_line_drops_a_unit_that_repeats_the_item():
    from owobook.models import Entry
    from owobook.whatsapp.formatting import item_line

    assert item_line(Entry(quantity=3, unit="charger", item_description="charger")) == "3 charger"
    assert item_line(Entry(quantity=2, unit="crates", item_description="crate")) == "2 crate"
    assert item_line(Entry(quantity=3, unit="bags", item_description="rice")) == "3 bags rice"
