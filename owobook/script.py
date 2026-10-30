"""Make text drawable on receipt pictures and PDFs.

Our drawing setup (Pillow without libraqm, fpdf2 without shaping) can't lay out Devanagari, so a Hindi name
like "रमेश" would come out as boxes. `drawable()` spells Devanagari in Latin letters ("Ramesh") — simple
phonetic rules, good enough for names and items. Chat messages are untouched: WhatsApp shows Hindi fine.
"""

import re

_VOWELS = {"अ": "a", "आ": "a", "इ": "i", "ई": "i", "उ": "u", "ऊ": "u", "ऋ": "ri", "ए": "e", "ऐ": "ai",
           "ओ": "o", "औ": "au", "ऑ": "o"}
_CONSONANTS = {"क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n", "च": "ch", "छ": "chh", "ज": "j", "झ": "jh",
               "ञ": "n", "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n", "त": "t", "थ": "th", "द": "d",
               "ध": "dh", "न": "n", "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m", "य": "y", "र": "r",
               "ल": "l", "व": "v", "श": "sh", "ष": "sh", "स": "s", "ह": "h"}
_NUKTA = {"क": "q", "ख": "kh", "ग": "g", "ज": "z", "ड": "r", "ढ": "rh", "फ": "f"}  # क़ ज़ ड़ फ़ …
_MATRAS = {"ा": "a", "ि": "i", "ी": "i", "ु": "u", "ू": "u", "ृ": "ri", "े": "e", "ै": "ai", "ो": "o",
           "ौ": "au", "ॅ": "e", "ॉ": "o"}
_MARKS = {"ं": "n", "ँ": "n", "ः": "h", "।": ".", "॥": "."}
_VIRAMA, _NUKTA_SIGN = "्", "़"
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")


def _word(word: str) -> str:
    out, i = [], 0
    while i < len(word):
        ch = word[i]
        if ch in _CONSONANTS:
            sound = _CONSONANTS[ch]
            if i + 1 < len(word) and word[i + 1] == _NUKTA_SIGN:
                sound, i = _NUKTA.get(ch, sound), i + 1
            nxt = word[i + 1] if i + 1 < len(word) else ""
            if nxt in _MATRAS:
                sound, i = sound + _MATRAS[nxt], i + 1
            elif nxt == _VIRAMA:
                i += 1
            elif nxt and (nxt in _CONSONANTS or nxt in _MARKS):
                sound += "a"  # inherent vowel; dropped only at the end of a word ("रमेश" → "ramesh")
            out.append(sound)
        elif ch in _VOWELS:
            out.append(_VOWELS[ch])
        elif ch in _MARKS:
            out.append(_MARKS[ch])
        elif "०" <= ch <= "९":  # Devanagari digits
            out.append(str(ord(ch) - 0x0966))
        elif ch not in (_NUKTA_SIGN, _VIRAMA):
            out.append(ch)
        i += 1
    text = "".join(out)
    return text[:1].upper() + text[1:] if _DEVANAGARI.search(word[:1]) else text


def drawable(text: str | None) -> str | None:
    """Text safe to draw with our fonts: Devanagari → Latin letters; everything else unchanged."""
    if not text or not _DEVANAGARI.search(text):
        return text
    return re.sub(r"\S+", lambda m: _word(m[0]) if _DEVANAGARI.search(m[0]) else m[0], text)
