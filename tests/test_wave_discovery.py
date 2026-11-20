"""Регрессии рекомендаций при полном запасе и накопленной истории."""

from radio_wave.queue import RadioQueue


def track(track_id, artist="Кино", genre="рок", language="ru", **metadata):
    return {
        "id": track_id,
        "title": f"Трек {track_id}",
        "artist": artist,
        "genre": genre,
        "language": language,
        **metadata,
    }


def upcoming(queue):
    return [item["id"] for item in queue.upcoming]


def test_like_reranks_full_queue_without_discarding_next_ready():
    current = track("0", artist="Сплин", genre="инди")
    familiar = [track(str(number)) for number in range(1, 7)]
    queue = RadioQueue()
    queue.set_catalogue([
        current,
        *familiar,
        track("7", artist="Сплин", genre="инди"),
        track("8", artist="АукцЫон", genre="инди"),
    ])
    assert queue.select("0") == current
    queue.upcoming = familiar[:queue.lookahead]
    before = upcoming(queue)

    queue.like(current, liked=[current])

    after = upcoming(queue)
    assert after[0] == before[0]
    assert after != before
    assert "7" in after
    assert "8" in after
    assert len(after) == 8


def test_similar_unheard_track_sampled_among_familiar_tracks():
    familiar = [track(str(number)) for number in range(8)]
    unseen = track("new", artist="АукцЫон")
    queue = RadioQueue(seen_ids=[item["id"] for item in familiar])
    queue.set_catalogue(familiar + [
        unseen,
        track("unrelated", artist="Круг", genre="шансон"),
        track("english", artist="Кино", language="en"),
        track("unknown", artist="Кино", language="unknown"),
    ])
    current = queue.select("0", liked=[familiar[0]])
    assert "new" in upcoming(queue)
    assert "Ещё не звучало" in queue.reason(unseen)

    played = [queue.next_track()["id"] for _ in range(5)]
    assert "new" in played
    assert current["id"] not in played


def test_same_artist_discovery_stays_eligible_with_other_artists_available():
    familiar = [track(str(number), artist=f"Артист {number}") for number in range(5)]
    current = track("current")
    unheard = track("unheard")
    disliked = track("disliked")
    queue = RadioQueue(seen_ids=[item["id"] for item in familiar])
    queue.russian_only = True
    queue.set_catalogue(familiar + [
        current,
        unheard,
        disliked,
        track("unknown", language="unknown"),
        track("foreign", language="other"),
    ])

    queue.select(current["id"], liked=[familiar[0]], disliked=[disliked])

    ids = upcoming(queue)
    assert "unheard" in ids
    assert ids[0] == "unheard"
    assert not ({"disliked", "unknown", "foreign"} & set(ids))
    assert len(ids) == 6


def test_unheard_similar_torrent_is_queued_before_reusing_local_tracks():
    familiar = [track(str(number)) for number in range(6)]
    remote = track("new", artist="Сплин", ready=False)
    queue = RadioQueue(seen_ids=[item["id"] for item in familiar])
    queue.set_catalogue(familiar + [remote])
    queue.select("0", liked=[familiar[0]])
    assert "new" in upcoming(queue)
    assert not queue.is_ready("new")

    assert queue.next_track() is None
    assert queue.has_pending()
    queue.set_ready("new")
    assert queue.next_track()["id"] == "new"


def test_plays_through_large_catalogue_before_repeating():
    queue = RadioQueue()
    queue.set_catalogue([track(str(number)) for number in range(8)])

    played = [queue.next_track()["id"] for _ in range(8)]

    assert len(set(played)) == 8
    assert queue.next_track()["id"] != played[-1]
    assert len(queue.upcoming) == 7


def test_short_collection_can_continue_without_immediate_repeats_or_dislikes():
    queue = RadioQueue()
    tracks = [track(str(number)) for number in range(3)]
    queue.set_catalogue(tracks)

    played = [queue.next_track(disliked=[tracks[2]])["id"] for _ in range(6)]

    assert played == ["0", "1", "0", "1", "0", "1"]


def test_matched_listenbrainz_recording_enters_queue_as_discovery():
    familiar = [track(str(number)) for number in range(7)]
    unseen = track("new", artist="Новый исполнитель", genre="инди", mbid="linked")
    queue = RadioQueue(seen_ids=[item["id"] for item in familiar])
    queue.set_catalogue(familiar + [unseen])
    queue.select("0", liked=[familiar[0]])

    queue.set_similar_recordings(["linked"])

    assert "new" in upcoming(queue)
    assert "ListenBrainz" in queue.reason(unseen)


def test_ready_genre_matches_include_multiple_artists():
    familiar = [track(str(number)) for number in range(8)]
    queue = RadioQueue()
    queue.set_catalogue(familiar + [
        track("new", artist="Сплин"),
        track("new2", artist="АукцЫон"),
    ])
    current = queue.select("0")

    queue.like(current, liked=[current])

    assert len({item["artist"] for item in queue.upcoming}) > 1
