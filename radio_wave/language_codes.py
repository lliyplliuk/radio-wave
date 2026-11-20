"""Specific language codes shared by audio tags and genre feedback."""

import re


_CODE = re.compile(r"([a-z]{2,3})(?:[-_][a-z0-9]{2,8})*")
_AMBIGUOUS = {"und", "zxx", "mis", "mul", "xxx", "zzz"}
_ALIASES = {
    "russian": "ru", "русский": "ru", "rus": "ru",
    "english": "en", "английский": "en", "eng": "en",
    "french": "fr", "français": "fr", "французский": "fr", "fra": "fr", "fre": "fr",
    "german": "de", "deu": "de", "ger": "de", "spa": "es", "ita": "it", "por": "pt",
    "ukr": "uk", "bel": "be", "pol": "pl", "ces": "cs", "cze": "cs", "slk": "sk", "slo": "sk",
    "nld": "nl", "dut": "nl", "swe": "sv", "nor": "no", "dan": "da", "fin": "fi", "isl": "is", "ice": "is",
    "ron": "ro", "rum": "ro", "hun": "hu", "bul": "bg", "srp": "sr", "hrv": "hr", "slv": "sl",
    "ell": "el", "gre": "el", "tur": "tr", "ara": "ar", "heb": "he", "hin": "hi", "ben": "bn",
    "zho": "zh", "chi": "zh", "jpn": "ja", "kor": "ko", "vie": "vi", "tha": "th", "ind": "id",
    "fas": "fa", "per": "fa", "hye": "hy", "arm": "hy", "kat": "ka", "geo": "ka", "kaz": "kk",
}


def normalize_language_code(value: str | None) -> str:
    """Return a specific code, or an empty string for absent or mixed language."""
    text = str(value or "").strip().casefold()
    if text in _ALIASES:
        return _ALIASES[text]
    match = _CODE.fullmatch(text)
    if not match or (code := match[1]) in _AMBIGUOUS:
        return ""
    return _ALIASES.get(code, code)
