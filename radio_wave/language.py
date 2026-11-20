"""Осторожная локальная оценка языка названия для рекомендаций."""

from collections.abc import Mapping
from functools import lru_cache
import re
from typing import Any
from unicodedata import name, normalize

from .language_codes import normalize_language_code
from .tags import normalize_language

_TITLE_CONFIDENCE = 0.6
_OTHER_CYRILLIC = frozenset("іїєґўјђљњћџѓќѕәғқңөұүһ")
_RUSSIAN_WORDS = frozenset("""
    любовь любви свет сердце ночь ночи звезда звёзды звезды осень песня песни
    снег дождь ветер солнце дорога время тишина мечта небо облака земля кровь
    крови жду жди дальше раньше снова ещё верни помни русский русская война
    танцуй лето зима весна рассвет закат счастье надежда прощай здравствуй родина
    чёрный черный белый одинокий последний первая скоро музыка девушка берегу
    кукушка свободен свобода прекрасно прекрасное любимый любимая гляжу голоса компромисс
""".split())
_RUSSIAN_FUNCTION_WORDS = frozenset("""
    без ведь если или моя мои мой наше она они под пусть тебя тебе только
    чтобы это когда теперь такой здесь между навсегда никогда всегда
""".split())
_ENGLISH_WORDS = frozenset("""
    always again alone another away beautiful believe bridge casualty crown
    goodbye everybody falling forever freedom friends goodnight heaven hello home
    hope heartbreak heart life love memories midnight moon morning never nothing
    nowhere overflow people please rain remember roads shadows shine sleeping someday
    something stars summer things through together tomorrow tonight yesterday waiting
    whisper wonderful world winter sunshine dream dreams forest dancing darkness
    highway somewhere time wrathchild
""".split())
_ENGLISH_FUNCTION_WORDS = frozenset("""
    all and are because between down each end from here in into it's just let like many more
    myself of off only over should the their them there these those through under
    until very what when where which while will with without would your you
    mine my our way don't can't ain't
""".split())
_OTHER_LATIN_WORDS = frozenset("""
    amour l'amour bésame ciao corazon corazón chanson gracias
    noche mucho saudade mañana coração t'aime träume liebe
    porquê schön suenos sueños schöne heimweh canción canciones toujours
""".split())
_OTHER_SCRIPTS = frozenset({
    "ARABIC", "ARMENIAN", "BENGALI", "CJK", "DEVANAGARI", "GEORGIAN",
    "GREEK", "HANGUL", "HEBREW", "HIRAGANA", "KATAKANA", "THAI",
})
_EDITION_SUFFIX = re.compile(
    r"\s*[\[(](?:live|remaster(?:ed)?(?: \d{4})?|radio edit|(?:official )?audio)[\])]$"
)
_ENGLISH_PHRASE = re.compile(r"\b(?:my|your|our|the)\s+hero(?:es)?\b")
_WORD = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?", re.UNICODE)
_FRENCH_TITLE = re.compile(
    r"\b(?:la vie en rose|ne me quitte pas|je t'aime|les copains d'abord|l'amour|"
    r"chanson française|toujours avec toi|comme d'habitude|rien de rien)\b"
)


@lru_cache(maxsize=2048)
def _infer_title_language(title: str) -> str:
    title = _EDITION_SUFFIX.sub("", title)
    letters = [letter for letter in title if letter.isalpha()]
    if len(letters) < 2:
        return "unknown"
    scripts = {name(letter, "").split(" ", 1)[0] for letter in letters}
    words = set(_WORD.findall(title))

    if scripts == {"CYRILLIC"}:
        if _OTHER_CYRILLIC.intersection(letters):
            return "other"
        if "ё" in letters or words & _RUSSIAN_WORDS or len(words & _RUSSIAN_FUNCTION_WORDS) >= 2:
            return "ru"
    elif scripts == {"LATIN"}:
        if words & _OTHER_LATIN_WORDS:
            return "other"
        if (
            words & _ENGLISH_WORDS or len(words & _ENGLISH_FUNCTION_WORDS) >= 2
            or _ENGLISH_PHRASE.search(title)
        ):
            return "en"
    elif scripts and scripts <= _OTHER_SCRIPTS:
        return "other"
    return "unknown"


def effective_language(track: Mapping[str, Any]) -> tuple[str, float]:
    """Язык и доверие к нему. Оценка по названию не меняет исходные теги."""
    language = normalize_language(track.get("language"))
    if language != "unknown":
        confidence = _TITLE_CONFIDENCE if track.get("lang_source") == "title" else 1.0
        return language, confidence

    title = track.get("title")
    if isinstance(title, str) and title:
        clean_title = normalize("NFKC", title[:240]).casefold().replace("’", "'")
        language = _infer_title_language(clean_title)
        if language != "unknown":
            return language, _TITLE_CONFIDENCE
    return "unknown", 0.0


def genre_language(track: Mapping[str, Any]) -> tuple[str, float]:
    """Specific language for genre taste, independent of the broader playback balance."""
    code = normalize_language_code(track.get("language_code")) or normalize_language_code(track.get("language"))
    if code:
        return code, _TITLE_CONFIDENCE if track.get("lang_source") == "title" else 1.0
    title = track.get("title")
    if isinstance(title, str):
        clean_title = normalize("NFKC", title[:240]).casefold().replace("’", "'")
        if _FRENCH_TITLE.search(clean_title) and all(
            name(letter, "").startswith("LATIN ") for letter in clean_title if letter.isalpha()
        ):
            return "fr", _TITLE_CONFIDENCE
    language, confidence = effective_language(track)
    return (language, confidence) if language in {"ru", "en"} else ("unknown", 0.0)
