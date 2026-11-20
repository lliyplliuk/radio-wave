from collections import Counter

from radio_wave.candidate_window import candidate_window
from radio_wave.queue import RadioQueue
from radio_wave.ranker import TasteProfile


def song(number, genre, *, artist=None, language="ru"):
    return {"id": str(number), "title": str(number), "artist": artist or f"Artist {number}",
            "genre": genre, "language": language}


def test_neutral_wave_exposes_rare_genres_despite_a_large_rock_catalogue():
    queue = RadioQueue()
    tracks = [song(f"rock-{index:02}", "Rock") for index in range(32)]
    tracks += [song(f"{genre}-{index}", genre) for genre in ("Pop", "Chanson", "Jazz") for index in range(8)]
    queue.set_catalogue(tracks)
    counts = Counter(queue.next_track()["genre"] for _ in range(12))
    assert set(counts) == {"Rock", "Pop", "Chanson", "Jazz"}
    assert max(counts.values()) <= 4


def test_missing_genre_still_suggests_variety_when_rock_fills_the_queue():
    queue = RadioQueue()
    queue.set_catalogue([song(f"rock-{index:02}", "Rock") for index in range(16)])
    assert queue.missing_genre(language="ru") in {"pop", "chanson", "jazz"}
    assert queue.next_track()["genre"] == "Rock"


def test_nearby_ready_alternative_artists_are_played_even_when_one_artist_is_liked():
    queue = RadioQueue()
    kino = [song(f"kino-{index:02}", "Rock", artist="Кино") for index in range(24)]
    others = [song(f"{artist}-{index}", "Rock", artist=artist) for artist in ("Алиса", "Машина времени", "Сектор Газа") for index in range(4)]
    queue.fill(liked=[song("taste", "Rock", artist="Кино")])
    queue.set_catalogue([*kino, *others])
    counts = Counter(queue.next_track()["artist"] for _ in range(8))
    assert {"Алиса", "Машина времени", "Сектор Газа"} <= counts.keys()
    assert 0 < counts["Кино"] <= 4


def test_one_genre_like_is_mild_and_sustained_likes_change_the_mix():
    tracks = [song(f"{genre}-{index:02}", genre) for genre in ("Rock", "Pop", "Chanson", "Jazz") for index in range(24)]

    def heard(likes, *, weight=0.8):
        queue = RadioQueue(profile=TasteProfile(genre_weight=weight))
        queue.fill(liked=[song(f"liked-{index}", "Rock", artist="Different musician") for index in range(likes)])
        queue.set_catalogue(tracks)
        return queue.genre_reserve_targets(language="ru"), Counter(queue.next_track()["genre"] for _ in range(16))

    neutral_reserve, neutral = heard(0)
    one_like_reserve, one_like = heard(1)
    sustained_reserve, sustained = heard(12)
    assert neutral_reserve == one_like_reserve == {"rock": 1, "pop": 1, "chanson": 1, "jazz": 1}
    assert sustained_reserve["rock"] > 1 and len(sustained_reserve) > 1
    assert neutral["Rock"] < one_like["Rock"] < sustained["Rock"]
    assert set(sustained) == set(neutral)
    assert heard(12, weight=0)[0] == {}


def test_french_chanson_feedback_only_steers_foreign_supply():
    queue = RadioQueue()
    likes = [dict(song(f"fr-{index}", "Chanson", language="other"), language_code="fra") for index in range(12)]
    queue.fill(liked=likes)
    neutral = {"rock": 1, "pop": 1, "chanson": 1, "jazz": 1}
    assert queue.genre_reserve_targets() == queue.genre_reserve_targets(language="ru") == neutral
    assert queue.genre_reserve_targets(language="foreign")["chanson"] > 1


def test_source_genre_has_a_pending_slot_without_moving_the_visible_prefix():
    rock = [song(f"rock-{index:02}", "Rock") for index in range(24)]
    pop = dict(song("pop-source", ""), source="torrent", genre_hint="Pop", ready=False)
    queue = RadioQueue()
    queue.set_catalogue(rock)
    visible = [track["id"] for track in queue.upcoming[:4]]
    queue.pin_visible(visible)
    queue.set_catalogue([*rock, pop])

    assert [track["id"] for track in queue.upcoming[:4]] == visible
    assert pop["id"] in {track["id"] for track in queue.upcoming[4:]}
    assert queue.missing_genre(language="ru", skip={"chanson", "jazz"}) is None
    assert queue.next_track()["id"] == visible[0]


def test_projection_reserves_new_genre_behind_existing_sources_and_ready_fallback():
    rock = [dict(song(f"rock-{index:02}", "Rock"), source="torrent", ready=True) for index in range(25)]
    pending = dict(song("new-jazz", ""), source="torrent", genre_hint="Jazz", ready=False)
    result = candidate_window(
        [*rock, pending], is_automatic=lambda _: True, is_unheard=lambda _: True,
        upcoming_ids=[track["id"] for track in rock[:4]], genre_reserves={"jazz": 1},
    )
    assert result[:4] == rock[:4]
    assert pending in result
    assert sum(track["ready"] for track in result) >= 4
