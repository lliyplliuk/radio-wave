import pytest

from radio_wave.candidate_window import candidate_window
from radio_wave.ranker import (
    TasteProfile, genre_family, genre_fit, language_scores, rank, suppressed_genres,
)


def liked_tracks():
    genres = ["Rock", "RusRock", "Рок"] * 4 + ["Rock", "Pop", "Поп"]
    return [
        {"id": f"liked-{index}", "artist": f"Любимый артист {index}", "genre": genre, "language": "ru"}
        for index, genre in enumerate(genres)
    ]


def track(track_id, genre, *, ready=True, **fields):
    return dict(id=track_id, source="torrent", info_hash=track_id, genre=genre, ready=ready, **fields)


@pytest.mark.parametrize("genre", [
    "Rock", "RusRock", "Русский рок", "Punk", "Панк-рок", "Post-punk",
    "Alternative", "Alternative Rock", "Панк / Поп", "Heavy metal",
])
def test_rock_family_unifies_native_and_cyrillic_genres(genre):
    assert genre_family(genre) == "rock"


def test_rock_likes_win_over_pop_majority_without_excluding_pop():
    rock = track("rock", "Alternative", language="ru")
    pop = [track(f"pop-{index}", "Поп" if index % 2 else "Pop", language="ru") for index in range(30)]
    [first, *rest] = rank([*pop, rock], liked=liked_tracks())

    assert first.id == "rock"
    assert "жанр" in first.reason
    assert all(not row.excluded for row in rest)


def test_candidate_projection_keeps_future_rock_among_many_ready_pop_tracks():
    pop = [track(f"pop-{index}", "Pop") for index in range(40)]
    rock = [track(f"rock-{index}", "Metal", ready=False) for index in range(10)]
    fit = genre_fit(liked_tracks(), TasteProfile())
    visible = candidate_window(
        [*pop, *rock], is_automatic=lambda _: True, is_unheard=lambda _: True,
        taste_score=fit, pinned_ids=(pop[0]["id"],), upcoming_ids=(item["id"] for item in pop[1:5]),
    )

    assert len(visible) == 17
    assert visible[:5] == pop[:5]
    assert len([item for item in visible if item in rock]) >= 8
    assert any(item["ready"] and item is not pop[0] for item in visible)


def test_source_hint_never_overrides_tag_and_cannot_fake_verified_similarity():
    unknown = track("unknown", "", ready=False)
    hinted = track("hinted", "", ready=False, genre_hint="Русский рок")
    confirmed = track("confirmed", "Рок")
    pop = track("pop", "Pop", genre_hint="Rock")
    results = {row.id: row for row in rank([unknown, hinted, confirmed, pop], liked=liked_tracks())}
    fit = genre_fit(liked_tracks(), TasteProfile())

    assert fit(confirmed) > fit(hinted) > fit(pop) > fit(unknown)
    assert results["unknown"].reason == "Ждём теги"
    assert results["hinted"].reason == "Ждём теги · жанр по раздаче: Русский рок"
    assert not results["hinted"].similar and hinted["genre"] == ""
    assert results["confirmed"].similar and "Ждём теги" not in results["confirmed"].reason
    assert genre_fit(liked_tracks(), TasteProfile(genre_weight=0))(confirmed) == 0


def test_foreign_rock_still_has_legacy_genre_fit_with_zero_language_weight():
    rock = track("foreign", "Punk", artist="Ramones", language="en")
    pop = track("pop", "Pop", language="ru")
    old_likes = [dict(item, language="unknown") for item in liked_tracks()]
    [first, second] = rank([pop, rock], liked=old_likes, profile=TasteProfile(language_weight=0))

    assert first.id == rock["id"] and not first.excluded
    assert first.language_bias == second.language_bias == 0
    assert language_scores(liked=liked_tracks())["en"] == 0


def test_distinct_chanson_dislikes_are_learned_without_banning_genre_or_unknowns():
    dislikes = [
        {"id": f"disliked-{index}", "genre": "Chanson" if index % 2 else "Русский шансон"}
        for index in range(11)
    ] + [{"id": f"pop-{index}", "genre": "Поп"} for index in range(4)]
    rock = track("rock", "Alternative")
    chanson = track("chanson", "Шансон")
    unknown = track("unknown", "")
    scores = {row.id: row for row in rank([rock, chanson, unknown], liked=liked_tracks(), disliked=dislikes)}
    baseline = {row.id: row for row in rank([rock, chanson, unknown], liked=liked_tracks())}
    fit = genre_fit(liked_tracks(), TasteProfile(), disliked=dislikes)

    assert genre_family("Chanson") == genre_family("Русский шансон") == "chanson"
    assert scores["rock"].score > scores["unknown"].score > scores["chanson"].score
    assert not scores["chanson"].excluded and "жанр вам реже нравится" in scores["chanson"].reason
    assert scores["unknown"] == baseline["unknown"]
    assert fit(chanson) < -1 and fit(unknown) == 0
    assert suppressed_genres(liked_tracks(), dislikes, TasteProfile()) == {"chanson"}
    assert suppressed_genres(liked_tracks(), dislikes[:1] * 11, TasteProfile()) == set()
    assert suppressed_genres(liked_tracks(), dislikes, TasteProfile(genre_weight=0)) == set()


def test_candidate_window_skips_chanson_when_possible_but_keeps_pinned_and_last_fallback():
    ready_chanson = [track(f"chanson-{index}", "Chanson") for index in range(20)]
    ready_rock = [track(f"ready-rock-{index}", "Рок") for index in range(4)]
    undownloaded_rock = [track(f"pending-rock-{index}", "Metal", ready=False) for index in range(15)]
    dislikes = [{"id": f"bad-{index}", "genre": "шансон"} for index in range(11)]
    heard = {item["id"] for item in ready_rock}
    options = dict(
        is_automatic=lambda _: True, is_unheard=lambda item: item["id"] not in heard,
        taste_score=genre_fit(liked_tracks(), TasteProfile(), disliked=dislikes),
    )
    output = candidate_window(
        [*ready_chanson, *ready_rock, *undownloaded_rock], **options,
        upcoming_ids=(ready_chanson[0]["id"],),
    )
    assert len(output) == 16
    assert output[0] == ready_chanson[0]
    assert all(item in output for item in ready_rock)
    assert not any(item in output for item in ready_chanson[1:])

    fallback = candidate_window(ready_chanson, **options)
    assert len(fallback) == 16
