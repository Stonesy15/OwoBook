"""Guess the trader's language from what they said (a voice-note transcript or a text).

Muse Voice Transcribe detects the spoken language itself but doesn't report it, so we look at the
transcript: Devanagari script means Hindi; otherwise a few words only Portuguese, French or
Hinglish speakers use. English and Pidgin give None — they get the default English replies.

Used only while a trader hasn't chosen a language (language_pref "auto"); a choice like
"reply in English" always wins.
"""

import re

_MARKERS = {
    "portuguese": {
        "levou", "vai", "pagar", "pagou", "reais", "sexta", "amanhã", "amanha", "sacos", "saco", "fiado",
        "deve", "devendo", "quem", "comprou", "vendi", "vendeu", "recibo", "obrigado", "obrigada", "olá",
        "não", "hoje", "semana", "mês", "você", "meu", "minha", "loja", "caixas", "quanto", "dona", "seu",
        "dinheiro", "também", "está", "tudo",
    },
    "french": {
        "pris", "paiera", "payé", "payer", "vendredi", "demain", "sacs", "doit", "argent", "j'ai", "vendu",
        "reçu", "bonjour", "merci", "semaine", "aujourd'hui", "mois", "combien", "elle", "va", "je", "mon",
        "boutique", "oui", "qui", "francs", "dette", "acheté", "cartons", "reste", "payera", "rembourser",
    },
    "hindi": {
        "liya", "liye", "diya", "dega", "degi", "denge", "rupaye", "rupaiye", "shukravar", "bori", "chawal",
        "kitna", "kitne", "mera", "meri", "naam", "dukaan", "dukan", "baaki", "baki", "udhaar", "udhar",
        "paise", "becha", "bechi", "nahi", "hai", "hain", "aaj", "hafte", "mahine", "raseed", "kaun", "wala",
        "wali", "namaste", "kya", "mujhe",
    },
}
_GREETINGS = {"olá": "portuguese", "oi": "portuguese", "bonjour": "french", "salut": "french", "namaste": "hindi"}
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")


def detect_language(text: str | None) -> str | None:
    """'portuguese' | 'french' | 'hindi', or None (English, Pidgin, or not sure)."""
    if not text:
        return None
    if len(_DEVANAGARI.findall(text)) >= 3:
        return "hindi"
    words = re.findall(r"[\w']+", text.lower())
    if not words:
        return None
    if len(words) <= 2 and (lang := next((_GREETINGS[w] for w in words if w in _GREETINGS), None)):
        return lang  # "Olá", "Bonjour 👋"
    scores = {lang: sum(w in markers for w in words) for lang, markers in _MARKERS.items()}
    best = max(scores, key=scores.get)
    ranked = sorted(scores.values(), reverse=True)
    if ranked[0] >= 2 and ranked[0] > ranked[1]:
        return best
    return None


# Muse Voice Transcribe's names for our languages (Yorùbá, Hausa, Igbo aren't supported; Pidgin is English-based).
ASR_NAMES = {"english": "English", "pidgin": "English", "portuguese": "Portuguese", "french": "French", "hindi": "Hindi"}
# What a trader most likely speaks, by phone country code, before they've told us.
COUNTRY_VOICE = {
    "234": ["English"], "233": ["English"], "254": ["English"], "256": ["English"], "255": ["English"],
    "27": ["English"], "1": ["English"], "44": ["English"], "63": ["English", "Tagalog"],
    "55": ["Portuguese"], "244": ["Portuguese"], "258": ["Portuguese"], "52": ["Spanish"],
    "91": ["Hindi", "English"],
    **{cc: ["French"] for cc in ("225", "221", "223", "226", "227", "228", "229", "237", "241", "242", "235", "236", "250")},
}


def voice_languages(language_pref: str | None, calling_code: str) -> list[str] | None:
    """The languageBias for a trader's voice notes: their chosen language first, else their country's;
    None for an unknown country, so Muse Voice Transcribe detects the language itself."""
    chosen = ASR_NAMES.get(language_pref or "")
    country = COUNTRY_VOICE.get(calling_code)
    langs = [x for x in [chosen, *(country or [])] if x]
    if chosen and "English" not in langs:
        langs.append("English")  # traders mix in English words
    return list(dict.fromkeys(langs)) or None
