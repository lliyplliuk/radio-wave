"""Ранжирование записей — чистый Python, без GUI и без сети.

Ранжирование учитывает:
  * лайки (похожий исполнитель / жанр дают очки),
  * дизлайки (исключаются полностью),
  * недавние прослушивания (остывание — штраф),
  * язык (небольшой исходный приоритет русского и поправка по обратной связи).

Каждый кандидат получает текстовое объяснение — его показывает кнопка
«Почему этот трек?», чтобы алгоритм можно было тестировать вручную.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from hashlib import blake2s
from math import exp, tanh
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .language import effective_language, genre_language
from .recording_version import is_live_track
from .tags import normalize_id3_genre, uses_literal_genre

Track = Dict[str, Any]
STRONGLY_DISLIKED_LANGUAGE = -1.5
STRONGLY_DISLIKED_GENRE = -0.6
_UNSCOPED_GENRE_WEIGHT = 0.35
_UNKNOWN_GENRE_LANGUAGE = "unknown"
DISCOVERY_GENRES = ("rock", "pop", "chanson", "jazz")

_ROCK_LABELS = frozenset("""
    rock русрок рок русрокъ rurock rusrock altrock punk панк postpunk grunge
    гранж metal метал металл metalcore металкор hardcore хардкор
""".split())
_ALTERNATIVE_LABELS = frozenset({"alternative", "альтернатива", "альтернативный"})
_POP_LABELS = frozenset({"pop", "поп", "synthpop", "electropop", "попса"})
_CHANSON_LABELS = frozenset({"chanson", "шансон"})
_UNKNOWN_GENRES = {"", "unknown", "неизвестно", "other", "другое"}


@dataclass
class TasteProfile:
    """Крутилки вкуса (значения по умолчанию — из настроек приложения)."""

    artist_weight: float = 1.0
    genre_weight: float = 0.8
    catalogue_weight: float = 0.3
    recent_penalty: float = 1.5
    language_weight: float = 1.0

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "TasteProfile":
        data = data or {}
        return cls(
            artist_weight=float(data.get("artist_weight", 1.0)),
            genre_weight=float(data.get("genre_weight", 0.8)),
            catalogue_weight=float(data.get("catalogue_weight", 0.3)),
            recent_penalty=float(data.get("recent_penalty", 1.5)),
            language_weight=float(data.get("language_weight", 1.0)),
        )

    def to_dict(self) -> Dict[str, float]:
        return {
            "artist_weight": self.artist_weight,
            "genre_weight": self.genre_weight,
            "catalogue_weight": self.catalogue_weight,
            "recent_penalty": self.recent_penalty,
            "language_weight": self.language_weight,
        }


@dataclass
class Scored:
    track: Track
    score: float
    reason: str
    excluded: bool = False
    note: str = ""
    similar: bool = False
    language_bias: float = 0.0
    is_live: bool = False
    genre_preference: float = 0.0

    @property
    def id(self) -> str:
        return str(self.track.get("id", ""))


def norm(value: Any) -> str:
    text = str(value or "").strip().casefold().replace("ё", "е")
    return re.sub(r"\s+", " ", text)


def artist_match(a: str, b: str) -> float:
    """1.0 — совпадение исполнителя, 0.5 — общий токен, 0 — нет совпадения."""
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    tokens_a = set(re.findall(r"\w+", na))
    tokens_b = set(re.findall(r"\w+", nb))
    shared = tokens_a & tokens_b
    if shared:
        return 0.5
    return 0.0


def genre_match(a: str, b: str) -> float:
    """Точное совпадение сильнее совпадения семейства или составного тега."""
    return _genre_match_labels(_genre_label(a), _genre_label(b))


def _genre_match_labels(a: str, b: str) -> float:
    na, nb = norm(a), norm(b)
    if na in _UNKNOWN_GENRES or nb in _UNKNOWN_GENRES:
        return 0.0
    if na == nb:
        return 1.0
    family_a, family_b = _genre_family(na[:240]), _genre_family(nb[:240])
    if family_a and family_a == family_b:
        return 0.75
    parts_a = {p.strip() for p in re.split(r"[/,;]", na) if p.strip()}
    parts_b = {p.strip() for p in re.split(r"[/,;]", nb) if p.strip()}
    if parts_a & parts_b:
        return 0.5
    return 0.0


def genre_family(value: Any) -> str:
    """Сгруппировать жанры для вкуса без изменения оригинального тега."""
    return _genre_family(norm(_genre_label(value))[:240])


def _genre_label(value: Any) -> str:
    return normalize_id3_genre(str(value) if value is not None else "")


def _track_genre_label(track: Track) -> str:
    raw = track.get("genre")
    label = "" if raw is None else str(raw)
    if uses_literal_genre(track) or (not track.get("path") and not label.startswith("(")):
        return label
    return normalize_id3_genre(label)


@lru_cache(maxsize=512)
def _genre_family(label: str) -> str:
    words = set(re.findall(r"[^\W_]+", label))
    if words & _ROCK_LABELS:
        return "rock"
    if words & _CHANSON_LABELS:
        return "chanson"
    if words & _POP_LABELS:
        return "pop"
    if words & _ALTERNATIVE_LABELS:
        return "rock"
    return "" if label in _UNKNOWN_GENRES else label


def _track_genre(track: Track) -> tuple[str, float]:
    label = _track_genre_label(track)
    if _genre_family(norm(label)[:240]):
        return label, 1.0
    return track.get("genre_hint") or "", 0.6


def track_genre_family(track: Track) -> str:
    return _genre_family(norm(_track_genre(track)[0])[:240])


def _genre_votes(tracks: Iterable[Track], exclude: set[str]) -> Counter[tuple[str, str]]:
    weights: Counter[tuple[str, str]] = Counter()
    for track in tracks:
        track_id = str(track.get("id") or "")
        if not track_id or track_id in exclude:
            continue
        exclude.add(track_id)
        label, confidence = _track_genre(track)
        family = _genre_family(norm(label)[:240])
        if family:
            language, language_confidence = genre_language(track)
            weights[family, language] += confidence * (language_confidence or 1.0)
    return weights


def genre_mix_weight(liked: Iterable[Track], disliked: Iterable[Track], profile: TasteProfile) -> Callable[[Track], float]:
    """A neutral prior and gradual, language-specific updates for genre rotation."""
    seen: set[str] = set()
    positive = _genre_votes(liked, seen)
    negative = _genre_votes(disliked, seen)

    def weight(track: Track) -> float:
        family = track_genre_family(track)
        if not family or not profile.genre_weight:
            return 1.0
        language, confidence = genre_language(track)
        if language == _UNKNOWN_GENRE_LANGUAGE:
            pos, neg = positive[family, language], negative[family, language]
        else:
            pos = confidence * positive[family, language] + _UNSCOPED_GENRE_WEIGHT * positive[family, _UNKNOWN_GENRE_LANGUAGE]
            neg = confidence * negative[family, language] + _UNSCOPED_GENRE_WEIGHT * negative[family, _UNKNOWN_GENRE_LANGUAGE]
        return exp(2.7 * min(profile.genre_weight, 3.0) * (pos - neg) / (4.0 + pos + neg))

    return weight


def _genre_affinities(liked: Iterable[Track], disliked: Iterable[Track]) -> tuple[dict[tuple[str, str], float], Counter[str]]:
    liked_ids: set[str] = set()
    positive = _genre_votes(liked, liked_ids)
    negative = _genre_votes(disliked, liked_ids)
    totals: Counter[str] = Counter()
    known: Counter[str] = Counter()
    for (family, language), weight in positive.items():
        totals[language] += weight
        if language != _UNKNOWN_GENRE_LANGUAGE:
            known[family] += weight
    return {
        key: (
            positive[key] / (totals[key[1]] or 1)
            - min(1.8, 0.17 * max(0, negative[key] - positive[key]))
        )
        for key in positive.keys() | negative.keys()
    }, known


def genre_fit(
    liked: Iterable[Track], profile: TasteProfile, *, disliked: Iterable[Track] = (),
) -> Callable[[Track], float]:
    """Подготовить оценку жанра по явным отзывам без жёсткого исключения."""
    affinities, known = _genre_affinities(liked, disliked) if profile.genre_weight else ({}, Counter())
    known_total = sum(known.values()) or 1

    def fit(track: Track) -> float:
        label, confidence = _track_genre(track)
        family = _genre_family(norm(label)[:240])
        if not family or not affinities:
            return 0.0
        language, language_confidence = genre_language(track)
        unscoped = affinities.get((family, _UNKNOWN_GENRE_LANGUAGE), 0.0)
        if language == _UNKNOWN_GENRE_LANGUAGE:
            affinity = unscoped + _UNSCOPED_GENRE_WEIGHT * known[family] / known_total
        else:
            affinity = (language_confidence * affinities.get((family, language), 0.0)
                        + _UNSCOPED_GENRE_WEIGHT * unscoped)
        return profile.genre_weight * confidence * affinity

    return fit


def genre_affinity(liked: Iterable[Track], disliked: Iterable[Track], profile: TasteProfile) -> Callable[[Track], float]:
    return genre_fit(liked, profile, disliked=disliked)


def suppressed_genres(liked: Iterable[Track], disliked: Iterable[Track], profile: TasteProfile) -> set[str]:
    """Устойчиво отрицательные оценки без языковой области для поиска источников."""
    if not profile.genre_weight:
        return set()
    affinities, _known = _genre_affinities(liked, disliked)
    return {
        family for (family, language), affinity in affinities.items()
        if language == _UNKNOWN_GENRE_LANGUAGE and profile.genre_weight * affinity <= STRONGLY_DISLIKED_GENRE
    }


def genre_suppressed(
    liked: Iterable[Track], disliked: Iterable[Track], profile: TasteProfile,
    *, fit: Callable[[Track], float] | None = None,
) -> Callable[[Track], bool]:
    liked, disliked = list(liked), list(disliked)
    fit = fit or genre_fit(liked, profile, disliked=disliked)
    legacy = suppressed_genres(liked, disliked, profile)

    def suppressed(track: Track) -> bool:
        rating = fit(track)
        return rating <= STRONGLY_DISLIKED_GENRE or (rating < 0 and track_genre_family(track) in legacy)

    return suppressed


def _genre_language_match(left: tuple[str, float], right: tuple[str, float]) -> float:
    if left[0] == right[0] == _UNKNOWN_GENRE_LANGUAGE:
        return 1.0
    if _UNKNOWN_GENRE_LANGUAGE in {left[0], right[0]}:
        return _UNSCOPED_GENRE_WEIGHT
    return min(left[1], right[1]) if left[0] == right[0] else 0.0


def _entry_fields(entry: Dict[str, Any]) -> Tuple[str, str, str]:
    return (entry.get("artist", ""), entry.get("title", ""), _track_genre_label(entry))


def is_russian_known(track: Track) -> bool:
    """Строгий критерий: язык известен точно и равен 'ru'."""
    return str(track.get("language", "unknown")).lower() == "ru"


def _language_side(language: str) -> str | None:
    if language == "ru":
        return "ru"
    return "foreign" if language in {"en", "other"} else None


def language_side(track: Track) -> str | None:
    return _language_side(effective_language(track)[0])


def language_scores(liked=(), disliked=(), finished_listens=(), early_skips=(), *, grouped=False) -> dict[str, float]:
    scores = dict.fromkeys(("ru", "foreign") if grouped else ("ru", "en", "other"), 0.0)
    events = [
        (entry, signal)
        for entries, signal in (
            (finished_listens, 0.10), (liked, 0.47), (disliked, -0.23), (early_skips, -0.115),
        )
        for entry in entries
    ]
    events.sort(key=lambda pair: pair[0].get("at") or 0)
    for entry, signal in events:
        language, confidence = effective_language(entry)
        if grouped:
            language = _language_side(language)
        if language in scores:
            # Накопленные раньше лайки не заглушают свежую серию дизлайков.
            previous = scores[language]
            scores[language] = min(1.2, previous + confidence * (signal - 0.08 * previous))
    return scores


@dataclass(frozen=True)
class LanguageMix:
    russian_share: float = 0.5
    suppressed: frozenset[str] = frozenset()
    enabled: bool = True

    def reserve_targets(self, total: int) -> dict[str, int]:
        if not self.enabled or len(self.suppressed) == 2 or total <= 0:
            return {}
        russian = round(total * self.russian_share)
        if "ru" in self.suppressed:
            russian = 0
        elif "foreign" in self.suppressed:
            russian = total
        return {side: count for side, count in (("ru", russian), ("foreign", total - russian)) if count}


def language_mix(liked=(), disliked=(), finished_listens=(), early_skips=(), *, weight=1.0) -> LanguageMix:
    if weight <= 0:
        return LanguageMix(enabled=False)
    scores = language_scores(liked, disliked, finished_listens, early_skips, grouped=True)
    suppressed = frozenset(side for side, score in scores.items() if weight * score <= STRONGLY_DISLIKED_LANGUAGE)
    # Центр 50/50 и плавное насыщение оставляют место второй стороне при малом числе оценок.
    difference = weight * (scores["ru"] - scores["foreign"])
    return LanguageMix(0.5 + 0.46 * tanh(difference / 2.3), suppressed)


def _rank_order(scored: Scored) -> tuple[float, str, bytes, str]:
    track_id = scored.id
    info_hash, _, file_index = track_id.partition(":")
    if scored.track.get("source") == "torrent" and info_hash == scored.track.get("info_hash") and file_index.isdecimal():
        # Equal scores in one release must not inherit its file index order.
        digest = blake2s(track_id.encode("utf-8"), digest_size=8).digest()
        return -scored.score, f"{info_hash}:", digest, track_id
    return -scored.score, track_id, b"", track_id


def rank(
    candidates: Sequence[Track],
    liked: Iterable[Dict[str, Any]] = (),
    disliked: Iterable[Dict[str, Any]] = (),
    recent_ids: Iterable[str] = (),
    russian_only: bool = False,
    profile: Optional[TasteProfile] = None,
    similar_mbids: Optional[Iterable[str]] = None,
    finished_listens: Iterable[Track] = (),
    early_skips: Iterable[Track] = (),
) -> List[Scored]:
    """Отсортировать кандидатов по убыванию очков.

    Возвращает список Scored; исключённые кандидаты остаются в списке
    с флагом excluded=True и причиной — чтобы UI мог показать, что трек
    отсеян (например, дизлайк или не-русский язык).
    """
    profile = profile or TasteProfile()
    liked = list(liked)
    disliked = list(disliked)
    liked_ids = {str(item.get("id", "")) for item in liked}
    disliked_ids = {str(d.get("id", "")) for d in disliked}
    explicit_ids = liked_ids | disliked_ids
    skips = {
        str(item["id"]): item for item in early_skips
        if item.get("id") and str(item["id"]) not in explicit_ids
    }
    recent = {str(r) for r in recent_ids}
    external_similar = {norm(mbid) for mbid in (similar_mbids or ()) if mbid}
    language_biases = (
        language_scores(liked, disliked, finished_listens, skips.values()) if profile.language_weight else {}
    )
    fit_genre = genre_fit(liked, profile, disliked=disliked)
    like_genres = [(like, genre_language(like)) for like in liked]
    skip_genres = [(track_id, skip, genre_language(skip)) for track_id, skip in skips.items()]

    genre_counts: Dict[str, int] = {}
    for track in candidates:
        label = norm(_track_genre_label(track))
        genre_counts[label] = genre_counts.get(label, 0) + 1
    max_genre_count = max(genre_counts.values()) if genre_counts else 0

    results: List[Scored] = []
    for track in candidates:
        track_id = str(track.get("id", ""))
        artist = track.get("artist", "")
        genre, genre_confidence = _track_genre(track)
        scoped_language = genre_language(track)

        if track_id in disliked_ids:
            results.append(Scored(track, 0.0, "вы уже отметили «не нравится»", True, "disliked"))
            continue

        if russian_only and not is_russian_known(track):
            results.append(Scored(track, 0.0, "не проходит строгий русский фильтр", True, "language"))
            continue

        score = 0.0
        reason = ""
        best_sim = 0.0
        best_genre_match = 0.0
        best_like: Optional[Dict[str, Any]] = None
        matched_artist = False
        for like, like_language in like_genres:
            la, _, lg = _entry_fields(like)
            matching_genre = _genre_match_labels(genre, lg) * _genre_language_match(scoped_language, like_language)
            sim = (profile.artist_weight * artist_match(artist, la)
                   + profile.genre_weight * genre_confidence * matching_genre)
            if sim > best_sim:
                best_sim = sim
                best_like = like
                best_genre_match = matching_genre
        if best_like is not None and best_sim > 0:
            score += best_sim
            la, _, lg = _entry_fields(best_like)
            parts = []
            matched_artist = profile.artist_weight > 0 and artist_match(artist, la) > 0
            if matched_artist:
                parts.append(f"похожий исполнитель: {la}")
            if profile.genre_weight > 0 and best_genre_match > 0:
                parts.append(f"жанр: {lg}" if genre_confidence == 1 else f"близкий жанр по раздаче: {lg}")
            reason = "похоже на «" + ", ".join(parts) + "»"
        else:
            count = genre_counts.get(norm(genre), 0)
            if max_genre_count:
                score += profile.catalogue_weight * (count / max_genre_count)
            if genre and genre_confidence == 1:
                reason = f"популярный жанр в коллекции: {genre}"
            else:
                reason = "нет явного сходства — набор из коллекции"

        pending_tags = track.get("source") == "torrent" and not track.get("ready") and genre_confidence < 1
        if pending_tags:
            reason = "Ждём теги"
            if _genre_family(norm(genre)[:240]):
                reason += f" · жанр по раздаче: {genre}"
        elif genre_confidence < 1 and _genre_family(norm(genre)[:240]) and not matched_artist:
            reason = f"Предполагаемый жанр раздачи: {genre}"

        matched_externally = norm(track.get("mbid")) in external_similar
        if matched_externally:
            score += profile.artist_weight + profile.genre_weight
            reason = (
                f"похожий трек по ListenBrainz · {reason}"
                if best_sim > 0 else "похожий трек по ListenBrainz"
            )

        if skips and track_id not in liked_ids:
            skip_weight = max(
                max(1.0 if track_id == skip_id else 0.0,
                    profile.artist_weight * artist_match(artist, skip.get("artist"))
                    + profile.genre_weight * genre_confidence * _genre_match_labels(genre, _track_genre_label(skip))
                    * _genre_language_match(scoped_language, skip_language))
                for skip_id, skip, skip_language in skip_genres
            )
            if skip_weight > 0:
                score -= 0.5 * skip_weight
                reason += " · похожие треки раньше быстро пропускали"

        if track_id in recent:
            score -= profile.recent_penalty
            reason += " · недавно играло (остывает)"

        language_bias = 0.0
        if language_biases:
            language, confidence = effective_language(track)
            language_bias = profile.language_weight * confidence * language_biases.get(language, 0.0)
        if language_bias > 0:
            reason += " · предпочтительный язык"
        elif language_bias < 0:
            reason += " · этот язык вам реже нравится"
        genre_bias = fit_genre(track)
        if genre_bias < 0:
            if track_id in liked_ids:
                genre_bias = 0.0
            else:
                reason += " · этот жанр вам реже нравится"
        live = is_live_track(track)
        if live:
            reason += " · концертная запись"
        results.append(Scored(
            track, score + language_bias + genre_bias - (0.3 if live else 0.0), reason,
            similar=matched_externally or (best_sim > 0 and (genre_confidence == 1 or (matched_artist and not pending_tags))),
            language_bias=language_bias,
            is_live=live,
            genre_preference=genre_bias + profile.genre_weight * genre_confidence * best_genre_match,
        ))

    results.sort(key=_rank_order)
    return results


def top_candidates(
    candidates: Sequence[Track],
    liked: Iterable[Dict[str, Any]] = (),
    disliked: Iterable[Dict[str, Any]] = (),
    recent_ids: Iterable[str] = (),
    russian_only: bool = False,
    profile: Optional[TasteProfile] = None,
    limit: int = 0,
    similar_mbids: Optional[Iterable[str]] = None,
) -> List[Scored]:
    """Только неотсеянные кандидаты, отсортированные по очкам."""
    scored = [s for s in rank(candidates, liked, disliked, recent_ids, russian_only, profile, similar_mbids)
              if not s.excluded]
    if limit:
        scored = scored[:limit]
    return scored
