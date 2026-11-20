"""Тесты ранжирования: фильтры, лайки, охлаждение недавних."""

from radio_wave.ranker import TasteProfile, rank, top_candidates


def track(ident, artist, genre, language="ru", title=None):
    return {"id": ident, "artist": artist, "title": title or f"трек {ident}", "genre": genre, "language": language}


def test_russian_only_keeps_only_known_russian():
    candidates = [
        track("1", "Ария", "Рок"),
        track("2", "Beatles", "Rock", "other"),
        track("3", "Кто-то", "", "unknown"),
    ]
    scored = rank(candidates, russian_only=True)
    allowed = [s.id for s in scored if not s.excluded]
    assert allowed == ["1"]


def test_russian_only_off_lets_everything_through():
    candidates = [track("1", "Ария", "Рок"), track("2", "Beatles", "Rock", "other")]
    scored = rank(candidates, russian_only=False)
    assert len([s for s in scored if not s.excluded]) == 2


def test_dislike_is_excluded_entirely():
    candidates = [track("1", "Ария", "Рок"), track("2", "Ария", "Рок")]
    scored = rank(candidates, disliked=[{"id": "1", "artist": "Ария", "title": "x", "genre": "Рок"}])
    assert [s.id for s in scored if not s.excluded] == ["2"]
    assert next(s for s in scored if s.id == "1").excluded


def test_likes_change_order_and_produce_reason():
    candidates = [
        track("1", "Кино", "Рок"),
        track("2", "Ария", "Рок"),
    ]
    liked = [{"id": "9", "artist": "Ария", "title": "x", "genre": "Рок"}]
    scored = [s for s in rank(candidates, liked=liked) if not s.excluded]
    assert scored[0].id == "2"
    assert "Ария" in scored[0].reason


def test_genre_weight_affects_score():
    candidates = [track("1", "Ария", "Рок"), track("2", "Ария", "Джаз")]
    liked = [{"id": "9", "artist": "Ария", "title": "x", "genre": "Рок"}]
    profile = TasteProfile(artist_weight=0.0, genre_weight=1.0, catalogue_weight=0.0)
    scored = [s for s in rank(candidates, liked=liked, profile=profile) if not s.excluded]
    assert scored[0].id == "1"


def test_recent_track_is_cooled_down():
    candidates = [track("1", "Ария", "Рок"), track("2", "Ария", "Рок")]
    liked = [{"id": "9", "artist": "Ария", "title": "x", "genre": "Рок"}]
    scored = [s for s in rank(candidates, liked=liked, recent_ids=["1"]) if not s.excluded]
    assert scored[0].id == "2"


def test_top_candidates_respects_limit():
    candidates = [track(str(i), "Ария", "Рок") for i in range(10)]
    assert len(top_candidates(candidates, limit=4)) == 4
