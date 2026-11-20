"""Тесты очереди: вместимость, пополнение, лайки, сломанные торренты."""

from radio_wave.queue import RadioQueue


def track(ident, artist="Ария", genre="Рок", ready=True):
    return {
        "id": ident,
        "artist": artist,
        "title": f"трек {ident}",
        "genre": genre,
        "language": "ru",
        "ready": ready,
    }


def build(count=12, ready=True):
    queue = RadioQueue()
    queue.set_catalogue([track(str(i), ready=ready) for i in range(count)])
    return queue


def test_queue_capacity_is_eight():
    queue = build()
    assert len(queue.upcoming) == 8


def test_start_sets_current_and_refills():
    queue = build()
    current = queue.start()
    assert current is not None
    assert queue.current is not None
    assert len(queue.upcoming) == 8


def test_transition_adds_one_and_keeps_capacity():
    queue = build()
    queue.start()
    before = [t["id"] for t in queue.upcoming]
    queue.next_track()
    after = [t["id"] for t in queue.upcoming]
    assert len(after) == 8
    assert before[0] != after[0]


def test_like_keeps_immediately_next_ready_track():
    queue = build()
    queue.start()
    queue.next_track()
    current = queue.current
    immediately_next = queue.upcoming[0]["id"]
    queue.like(current, liked=[{"id": current["id"], "artist": current["artist"], "genre": current["genre"]}])
    assert queue.upcoming[0]["id"] == immediately_next
    assert len(queue.upcoming) == 8


def test_broken_torrent_gives_no_feedback():
    queue = build()
    queue.set_broken("2")
    queue.start()
    queue.next_track()
    ids = [t["id"] for t in queue.upcoming]
    assert "2" not in ids
    assert queue.current["id"] != "2"


def test_pending_track_is_skipped_when_ready_one_exists():
    queue = RadioQueue()
    queue.set_catalogue([
        track("1", ready=False),
        track("2", ready=True),
        track("3", ready=True),
    ])
    current = queue.start()
    assert current["id"] == "2"


def test_no_ready_track_returns_none():
    queue = RadioQueue()
    queue.set_catalogue([track("1", ready=False), track("2", ready=False)])
    assert queue.start() is None
    assert queue.next_track() is None


def test_snapshot_reports_states():
    queue = RadioQueue()
    queue.set_catalogue([track("1", ready=True), track("2", ready=False)])
    queue.start()
    snapshot = queue.snapshot()
    assert snapshot[0]["state"] == "playing"
    states = {item["id"]: item["state"] for item in snapshot}
    assert states["2"] == "pending"


def test_russian_is_preferred_but_english_can_play():
    queue = RadioQueue()
    queue.set_catalogue([
        {"id": "1", "artist": "Ария", "title": "a", "genre": "Рок", "language": "ru", "ready": True},
        {"id": "2", "artist": "Beatles", "title": "b", "genre": "Рок", "language": "en", "ready": True},
    ])
    assert [t["id"] for t in queue.upcoming] == ["1", "2"]
    assert queue.start()["id"] == "1"
    assert queue.next_track()["id"] == "2"
