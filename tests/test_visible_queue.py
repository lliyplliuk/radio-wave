"""Publication of the exact visible prefix of the live radio queue."""

from radio_wave.queue import RadioQueue
from radio_wave.ranker import Scored


def track(track_id, ready=True, **fields):
    return {
        "id": track_id, "artist": "Исполнитель", "title": f"Песня {track_id}",
        "ready": ready, **fields,
    }


def ids(queue):
    return [item["id"] for item in queue.upcoming]


class RankSpy:
    def __init__(self):
        self.calls = []
        self.priority = {}

    def __call__(self, tracks, **_options):
        self.calls.append([item["id"] for item in tracks])
        return [
            Scored(item, self.priority.get(item["id"], 0), f"Оценка {self.priority.get(item['id'], 0)}")
            for item in sorted(tracks, key=lambda entry: -self.priority.get(entry["id"], 0))
        ]


def test_four_visible_tracks_keep_order_and_reason_until_played():
    ranker = RankSpy()
    queue = RadioQueue(ranker=ranker)
    tracks = [track(name) for name in "abcdefghij"]
    queue.set_catalogue(tracks)
    assert ids(queue) == list("abcdefgh")
    assert not queue.pinned_ids()
    queue.snapshot()
    assert not queue.pinned_ids()

    queue.pin_visible(ids(queue)[:4])
    reasons = [queue.reason(item) for item in queue.upcoming[:4]]
    ranker.priority = {"d": 4, "c": 5, "b": 6, "a": 10, "f": 100, "j": 92}
    queue.rebuild(liked=[tracks[-1]])
    queue.set_similar_recordings(["related"])
    assert list(queue.pinned_ids()) == ids(queue)[:4] == list("abcd")
    assert [queue.reason(item) for item in queue.upcoming[:4]] == reasons
    assert ranker.calls[-1] == list("efghij")
    assert ids(queue)[4:] == list("fjeg")

    assert queue.next_track()["id"] == "a"
    assert list(queue.pinned_ids()) == list("bcd")
    assert ids(queue) == list("bcdfjegh")


def test_two_visible_tracks_hold_pending_head_and_rerank_only_hidden_tail():
    ranker = RankSpy()
    queue = RadioQueue(ranker=ranker)
    queue.set_catalogue([track(name) for name in "abcdef"])
    queue.pin_visible(ids(queue)[:2])
    queue.pin_visible(["a"])
    assert queue.pinned_ids() == ("a", "b")

    ranker.priority = {"f": 100, "e": 99}
    queue.set_ready("a", False)
    queue.rebuild(disliked=[track("z")])
    assert ids(queue) == list("abfecd")
    assert queue.snapshot()[0]["state"] == "pending"
    assert queue.has_pending(allow_repeats=False)
    assert queue.next_track() is None
    assert ranker.calls[-1] == list("cdef")

    queue.set_ready("a")
    assert queue.snapshot()[0]["state"] == "ready"
    assert queue.next_track()["id"] == "a"
    assert queue.upcoming[0]["id"] == "b"


def test_manual_selection_and_terminal_failure_remove_only_affected_slots():
    queue = RadioQueue()
    items = [track(name) for name in "abcdef"]
    queue.set_catalogue(items)
    queue.pin_visible(ids(queue)[:4])
    queue.select("b")
    assert queue.pinned_ids() == ("a", "c", "d")
    queue.set_broken("a")
    assert queue.pinned_ids() == ("c", "d")
    queue.set_catalogue([
        {**item, "title": "Обновлённое название"} if item["id"] == "c"
        else {**item, "unavailable": True} if item["id"] == "d" else item
        for item in items
    ])
    assert queue.pinned_ids() == ("c",)
    assert queue.upcoming[0]["title"] == "Обновлённое название"
    assert queue.next_track()["id"] == "c"


def test_ready_alias_does_not_replace_published_source_and_repeat_cooldown_holds():
    old = track("old", title="Синяя вода")
    queue = RadioQueue(history=[old])
    queue.set_catalogue([old])
    queue.pin_visible(ids(queue))
    assert queue.next_track(allow_repeats=False) is None
    assert queue.pinned_ids() == ("old",)

    queue.set_catalogue([
        {**old, "ready": False},
        track("alternate", title="СИНЯЯ ВОДА"),
        track("fresh"),
    ])
    assert ids(queue) == ["old", "fresh"]
    assert queue.next_track(allow_repeats=False) is None
    assert not queue.has_pending(allow_repeats=False)
    queue.set_broken("old")
    assert queue.next_track(allow_repeats=False)["id"] == "fresh"


def test_cold_online_album_publishes_only_one_contiguous_slot_until_new_sources_arrive():
    def release(track_id, artist, info_hash):
        return track(track_id, artist=artist, source="torrent", info_hash=info_hash)

    kino = [release(f"kino-{index}", "Кино", "kino") for index in range(8)]
    queue = RadioQueue()
    queue.set_catalogue(kino)
    assert len(queue.upcoming) == 8
    first = queue.visible_upcoming(automatic_sources={"kino"})
    assert len(first) == 1
    queue.pin_visible([first[0]["id"]])
    assert queue.next_track()["id"] == first[0]["id"]

    after_start = queue.visible_upcoming(automatic_sources={"kino"})
    assert len(after_start) == 1
    queue.pin_visible([after_start[0]["id"]])
    alternatives = [release("alisa", "Алиса", "alisa"), release("sektor", "Сектор Газа", "sektor")]
    queue.set_catalogue([*kino, *alternatives])
    visible = queue.visible_upcoming(automatic_sources={"kino", "alisa", "sektor"})
    assert visible[0]["id"] == after_start[0]["id"]
    assert {item["artist"] for item in visible} == {"Кино", "Алиса", "Сектор Газа"}
    assert queue.next_track()["id"] == after_start[0]["id"]


def test_cold_visible_limit_keeps_existing_pins_and_leaves_offline_unchanged():
    queue = RadioQueue()
    queue.set_catalogue([track(f"album-{index}", source="torrent", info_hash="album") for index in range(8)])
    assert len(queue.visible_upcoming()) == 4
    queue.pin_visible(ids(queue)[:2])
    assert len(queue.visible_upcoming(automatic_sources={"album"})) == 2
    assert queue.next_track()["id"] == "album-0"


def test_one_compilation_can_publish_four_verified_distinct_artists():
    queue = RadioQueue()
    tracks = [track(str(i), artist=artist, source="torrent", info_hash="compilation",
                    path=f"/fixture/Collection/{i}.wav", ready=True)
              for i, artist in enumerate(("Кино", "Adele", "Алиса", "ABBA"))]
    queue.set_catalogue(tracks, automatic_sources={"compilation"})
    assert {item["artist"] for item in queue.visible_upcoming()} == {item["artist"] for item in tracks}
