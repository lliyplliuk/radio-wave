"""Automatic playback across cached, pending, and alternate audio copies."""

from radio_wave.queue import RadioQueue


MBID = "8708b6d3-a99e-4607-9dfb-11622acf3d69"


def track(track_id, artist="Ария", title=None, ready=True, **options):
    return {
        "id": track_id, "artist": artist, "title": title or f"Песня {track_id}",
        "language": "ru", "ready": ready, **options,
    }


def test_repeated_next_does_not_cycle_ready_cache_when_unheard_tracks_can_download():
    familiar = [track(f"cached-{i}") for i in range(11)]
    history = (familiar * 7)[-70:]
    pending = [track(f"unheard-{i:02}", ready=False, source="torrent") for i in range(70)]
    queue = RadioQueue(history=history)
    queue.set_catalogue(familiar + pending)
    assert all(queue.is_unheard(item) for item in queue.upcoming)

    played = []
    for _ in range(6):
        assert queue.next_track() is None
        assert queue.has_pending(allow_repeats=False)
        queue.set_ready(queue.upcoming[0]["id"])
        selected = queue.next_track()
        assert selected is not None
        played.append(selected["id"])

    assert len(set(played)) == 6
    assert all(track_id.startswith("unheard-") for track_id in played)


def test_alternate_files_match_mbids_and_unambiguous_clean_tags():
    first = track("first", "Татьяна Куртукова", "Синяя вода", mbid=MBID)
    same_id = track("second", "другой текст", "другой текст", mbid=MBID.upper())
    same_tags = track("third", "001. Татьяна Куртукова", "СИНЯЯ   ВОДА!")
    other = track("live", "Татьяна Куртукова", "Синяя вода (Live)")
    malformed = track("corrupted", "\0Татьяна Куртукова", "Синяя вода")
    queue = RadioQueue(history=[first])
    queue.set_catalogue([same_id, same_tags, other, malformed])

    assert not queue.is_unheard(same_id)
    assert not queue.is_unheard(same_tags)
    assert queue.is_unheard(other)
    assert queue.is_unheard(malformed)
    assert len(queue.upcoming) == 3
    assert queue.next_track()["id"] in {"live", "corrupted"}


def test_legacy_history_find_same_title_with_no_recording_ids():
    older = track("old", "КИНО", "Звёзды")
    alias = track("alias", " Кино ", "Звезды.")
    other = track("new", "Кино", "Весна")
    queue = RadioQueue()
    queue.set_catalogue([alias, other])
    queue.set_history([older])
    assert not queue.is_unheard(alias)
    assert queue.next_track()["id"] == "new"


def test_online_cooldown_uses_last_20_distinct_recordings_and_manual_replay_works():
    tracks = [track(str(number)) for number in range(21)]
    queue = RadioQueue(history=tracks + [tracks[-1]] * 24)
    queue.set_catalogue([tracks[0], tracks[1], tracks[-1]])
    assert queue.next_track(allow_repeats=False)["id"] == "0"

    queue = RadioQueue(history=tracks[1:])
    queue.set_catalogue([tracks[1], tracks[-1]])
    assert queue.next_track(allow_repeats=False) is None
    assert queue.select(tracks[-1]["id"]) == tracks[-1]


def test_exhausted_offline_queue_uses_an_oldest_ready_alternative():
    latest = track("latest")
    previous = track("previous")
    alias = track("alias", title=latest["title"])
    queue = RadioQueue(history=[previous, latest])
    queue.set_catalogue([latest, alias, previous])
    assert not queue.has_pending()
    assert queue.next_track(allow_repeats=False) is None
    assert queue.next_track()["id"] == "previous"
    assert queue.next_track()["id"] in {"latest", "alias"}


def test_fresh_unavailable_and_broken_items_allow_offline_repeat():
    old = track("old")
    queued = track("pending", ready=False)
    broken = track("missing", ready=False, unavailable=True)
    queue = RadioQueue(history=[old])
    queue.set_catalogue([old, queued, broken])
    assert queue.next_track() is None
    queue.set_broken("pending")
    assert not queue.has_pending()
    assert queue.next_track()["id"] == "old"
