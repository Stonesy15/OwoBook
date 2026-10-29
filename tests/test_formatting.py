from owobook.whatsapp.formatting import parse_button_id, render_plain, reply_language, Reply, entry_buttons
from owobook.whatsapp.i18n import LANGUAGES, STRINGS


def test_reply_language_is_the_choice_not_the_message():
    assert reply_language(None) == "english"
    assert reply_language("auto") == "english"  # old default: no more mirroring Pidgin
    assert reply_language("pidgin") == "pidgin"
    assert reply_language("igbo") == "igbo"
    assert reply_language("klingon") == "english"


def test_every_language_has_every_text_with_the_same_placeholders():
    import string

    def fields(text):
        return {f for _, f, _, _ in string.Formatter().parse(text) if f}

    english = STRINGS["english"]
    for lang in LANGUAGES:
        assert set(STRINGS[lang]) == set(english), lang
        for key, text in english.items():
            assert fields(STRINGS[lang][key]) == fields(text), (lang, key)


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
    assert item_line(Entry(quantity=3, unit="ctn", item_description="indomie 3 ctn")) == "indomie 3 ctn"
    assert item_line(Entry(quantity=3, unit="bags", item_description="rice 13 kg")) == "3 bags rice 13 kg"
