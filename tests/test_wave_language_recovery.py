"""Возврат русскоязычных кандидатов при полном буфере «Волны»."""

import pytest

from radio_wave.queue import RadioQueue
from radio_wave.ranker import TasteProfile


def track(track_id, language, *, artist="Любимая группа", genre="Rock", ready=True, **metadata):
    return {
        "id": track_id, "title": track_id, "language": language, "artist": artist, "genre": genre,
        "ready": ready, **metadata,
    }


def ids(queue):
    return [item["id"] for item in queue.upcoming]


def setup_queue(*, weight=1.0, history=("unknown", "en", "unknown"), disliked=()):
    history = [track(f"played-{index}", language) for index, language in enumerate(history)]
    candidates = [track(f"foreign-{index:02}", "en") for index in range(14)]
    queue = RadioQueue(lookahead=8, history=history, profile=TasteProfile(artist_weight=2, language_weight=weight))
    queue.fill(liked=[track("liked", "unknown")], disliked=disliked)
    queue.set_catalogue(candidates)
    return queue, candidates


def test_pending_russian_gets_a_download_slot_without_disturbing_visible_foreign_tracks():
    queue, candidates = setup_queue()
    published = ids(queue)[:4]
    queue.pin_visible(published)
    russian = track("russian", "ru", artist="Другой артист", ready=False, source="torrent")
    queue.set_catalogue([*candidates, russian])

    assert ids(queue)[:4] == published
    assert ids(queue)[4] == russian["id"]
    assert queue.next_track()["id"] == published[0]
    assert ids(queue)[:3] == published[1:]
    assert ids(queue)[4] == russian["id"]


def test_ready_recovery_plays_after_visible_prefix_and_does_not_reset_shortage():
    queue, candidates = setup_queue()
    published = ids(queue)[:4]
    queue.pin_visible(published)
    russian = track("russian", "ru", artist="Другой артист", source="torrent")
    queue.set_catalogue([*candidates, russian])

    assert ids(queue)[:5] == [*published, russian["id"]]
    assert [queue.next_track()["id"] for _ in range(4)] == published
    assert queue.next_track()["id"] == russian["id"]
    assert queue.missing_language() == "ru"
    assert queue.recovery_id() is None


def test_recovery_honors_language_weight_and_strong_negative_russian_feedback():
    russian = track("russian", "ru", artist="Другой артист", ready=False, source="torrent")
    strong = [track(f"disliked-{index}", "ru", genre="") for index in range(18)]
    for weight, dislikes in ((0, []), (1, strong)):
        queue, candidates = setup_queue(weight=weight, disliked=dislikes)
        queue.set_catalogue([*candidates, russian])
        assert "ru" not in queue.language_reserve_targets()
        assert queue.recovery_id() is None
        assert not queue.offer_recovery(russian["id"])

    queue, candidates = setup_queue(disliked=strong[:3])
    queue.set_catalogue([*candidates, russian])
    assert queue.missing_language() in {None, "ru"}
    assert queue.recovery_id() == russian["id"]


def test_recovery_and_ordinary_foreign_offer_can_both_progress():
    queue, candidates = setup_queue()
    published = ids(queue)[:4]
    queue.pin_visible(published)
    russian = track("russian", "ru", artist="Другой артист", ready=False, source="torrent")
    foreign = track("foreign-offer", "en", artist="Другая группа", ready=False, source="torrent")
    queue.set_catalogue([*candidates, russian, foreign])

    assert queue.offer_recovery(russian["id"])
    assert queue.offer_discovery(foreign["id"])
    assert queue.recovery_id() == russian["id"]
    assert queue.offered_id() == foreign["id"]
    assert ids(queue)[:5] == [*published, russian["id"]]
    assert foreign["id"] in ids(queue)[5:]
    queue.set_broken(russian["id"])
    assert queue.recovery_id() is None
    assert queue.next_track()["id"] == published[0]


def test_pending_recovery_does_not_force_wait_when_english_is_downweighted_but_ready():
    dislikes = [track(f"disliked-{index}", "en", genre="") for index in range(18)]
    queue, candidates = setup_queue(disliked=dislikes)
    russian = track("russian", "ru", artist="Другой артист", ready=False, source="torrent")
    queue.set_catalogue([*candidates, russian])

    assert ids(queue)[4] == russian["id"]
    assert all(queue.is_ready(track_id) for track_id in ids(queue)[:4])
    assert queue.next_track()["language"] == "en"


@pytest.mark.parametrize("history,learned", [
    (("en", "ru", "en", "en", "en", "en"), False),
    (("en", "ru") * 3, False),
    (("en", "ru") * 3, True),
])
def test_rolling_balance_keeps_genre_variety(history, learned):
    queue, _candidates = setup_queue(history=history)
    genres = ("Rock", "Jazz", "Pop")
    candidates = [
        track(f"{language}-{index:02}", language, artist=f"Artist {language}{index}", genre=genres[index % 3])
        for language in ("ru", "en") for index in range(16)
    ]
    likes = [track("liked-ru", "ru", genre="")] if learned else []
    dislikes = [track(f"disliked-en-{index}", "en", genre="") for index in range(4)] if learned else []
    queue.fill(liked=likes, disliked=dislikes)
    queue.set_catalogue(candidates)

    played = [queue.next_track() for _ in range(12)]

    counts = [item["language"] for item in played]
    if learned:
        assert 6 < counts.count("ru") < 12
    else:
        assert counts.count("ru") == counts.count("en") == 6
    assert {item["genre"] for item in played} == set(genres)


@pytest.mark.parametrize("history", [(), ("en", "en", "en", "en", "en", "ru")])
def test_multiple_pending_russian_tracks_enter_hidden_slots_behind_english_prefix(history):
    queue, candidates = setup_queue(history=history)
    published = ids(queue)[:4]
    queue.pin_visible(published)
    russian = [
        track("ready-ru", "ru", artist="Артист 1"),
        track("pending-ru", "ru", artist="Артист 2", ready=False),
        track("inferred-ru", "unknown", artist="Artist 3", title="Группа крови", ready=False),
    ]
    queue.set_catalogue([*candidates, *russian[:1]])
    assert queue.missing_language() == "ru"
    queue.set_catalogue([*candidates, *russian])
    assert ids(queue)[:4] == published
    assert {item["id"] for item in russian} <= set(ids(queue)[4:])
    assert queue.missing_language() is None
    assert queue.next_track()["id"] == published[0]
