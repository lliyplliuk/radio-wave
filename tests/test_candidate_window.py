from radio_wave.candidate_window import AUTO_CANDIDATE_LIMIT, candidate_window


def recording(number, source_name="album", **fields):
    return {
        "id": f"{source_name}-{number}", "source": "torrent", "info_hash": source_name,
        "automatic": True, **fields,
    }


def window(tracks, *, heard=(), **kwargs):
    return candidate_window(
        tracks, is_automatic=lambda track: track.get("automatic", False),
        is_unheard=lambda track: track["id"] not in heard, **kwargs,
    )


def test_large_album_view_is_bounded_without_hiding_local_or_explicit_tracks():
    album = [recording(index, ready=True) for index in range(250)]
    local = recording(0, "local", source="local", ready=True)
    explicit = recording(0, "manual", automatic=False)
    before = album.copy()

    visible = window(
        [*album, local, explicit],
        pinned_ids=(album[0]["id"], album[247]["id"]),
        upcoming_ids=(track["id"] for track in album[1:5]),
    )

    assert len(visible) == AUTO_CANDIDATE_LIMIT + 4
    assert all(track in visible for track in (*album[:5], album[247], local, explicit))
    assert len({track["id"] for track in visible}) == len(visible)
    assert album == before and len(album) == 250


def test_window_rotates_after_playback_while_retaining_nearest_four():
    tracks = [recording(index, ready=True) for index in range(100)]
    heard = set()
    visible = window(tracks, heard=heard)
    assert len(visible) == AUTO_CANDIDATE_LIMIT
    heard_before = heard.copy()

    for index in range(30):
        current, *future = [track for track in visible if track["id"] not in heard]
        heard.add(current["id"])
        upcoming = [item["id"] for item in future[:4]]
        visible = window(
            tracks, heard=heard, pinned_ids=(current["id"],), upcoming_ids=upcoming,
        )
        assert len(visible) == AUTO_CANDIDATE_LIMIT + 1
        assert current in visible
        assert all(track in visible for track in future[:4])
        assert all(track["id"] not in heard for track in visible if track is not current)

    assert heard_before == set()
    assert len(heard) == 30 and len(tracks) == 100


def test_many_pending_tracks_leave_ready_offline_fallback_and_skip_broken():
    pending = [recording(index, "new") for index in range(50)]
    fallback = [recording(index, "stored", ready=True) for index in range(4)]
    broken = [recording(index, "unavailable", broken=True) for index in range(10)]
    heard = {track["id"] for track in fallback}
    visible = window([*pending, *fallback, *broken], heard=heard)

    assert len(visible) == AUTO_CANDIDATE_LIMIT
    assert all(track in visible for track in fallback)
    assert not any(track in visible for track in broken)
    assert len([track for track in visible if track in pending]) == AUTO_CANDIDATE_LIMIT - len(fallback)


def test_equal_viability_rotates_across_distinct_sources_stably():
    majority = [recording(index, "album-a", ready=True) for index in range(1000)]
    minority = [recording(0, "album-b", ready=True), recording(0, "album-c", ready=True)]
    tracks = [*majority, *minority]
    seen_checks = 0

    def is_unheard(_track):
        nonlocal seen_checks
        seen_checks += 1
        return True

    visible = candidate_window(tracks, is_automatic=lambda _track: True, is_unheard=is_unheard)

    assert visible[:3] == [majority[0], *minority]
    assert visible == window(tracks)
    assert seen_checks == len(tracks)


def test_disliked_ready_tracks_do_not_take_the_ready_fallback_reserve():
    manual = [
        recording(index, "manual", automatic=False, ready=True)
        for index in range(4)
    ]
    rejected_auto = recording(0, "rejected", ready=True)
    pending = [recording(index, "new") for index in range(20)]
    fallback = recording(0, "stored", ready=True)
    rejected_ids = {track["id"] for track in (*manual, rejected_auto)}

    visible = window(
        [*manual, rejected_auto, *pending, fallback], heard={fallback["id"]},
        is_schedulable=lambda track: track["id"] not in rejected_ids,
    )

    assert all(track in visible for track in manual)
    assert rejected_auto not in visible
    assert fallback in visible
    assert len(visible) == len(manual) + AUTO_CANDIDATE_LIMIT


def test_language_reserve_keeps_fresh_russian_songs_with_full_foreign_buffer():
    foreign = [recording(index, "foreign", language="en", fit=1.0) for index in range(40)]
    ready = [recording(index, "offline", language="en", ready=True, fit=1.0) for index in range(4)]
    russian = [
        recording(0, "ru-rock", language="ru", fit=0.8),
        recording(1, "ru-rock", title="Звезда и ночь", fit=0.8),
        recording(0, "ru-indie", title="Дождь", fit=0.7),
    ]
    restricted = [
        recording(0, "disliked", language="ru", fit=0.9),
        recording(0, "dead", language="ru", unavailable=True, fit=0.9),
        recording(0, "suppressed", language="ru", fit=-1.0),
        recording(0, "heard", language="ru", fit=0.9),
        recording(0, "transliterated", title="Zvezda po imeni Solntse", fit=0.9),
    ]
    upcoming = [track["id"] for track in foreign[:8]]
    tracks = [*foreign, *ready, *russian, *restricted]
    options = dict(
        heard={*(track["id"] for track in ready), restricted[3]["id"]},
        upcoming_ids=upcoming,
        is_schedulable=lambda track: track is not restricted[0],
        taste_score=lambda track: track["fit"],
    )
    baseline = window(tracks, **options)
    mixed = window(tracks, reserve_russian=True, **options)

    assert [track["id"] for track in mixed[:8]] == upcoming
    assert len(mixed) == AUTO_CANDIDATE_LIMIT
    assert all(track in mixed for track in ready + russian)
    assert all(track not in mixed for track in restricted)
    assert all(track not in baseline for track in russian)


def test_language_reserve_keeps_ready_russian_ahead_of_pending_when_rock_fit_is_close():
    foreign = [recording(index, "foreign", language="en", ready=True, fit=1.0) for index in range(24)]
    pending = [recording(index, "ru-pending", language="ru", fit=1.0) for index in range(7)]
    ready = recording(0, "ru-ready", language="ru", ready=True, fit=0.8)
    visible = window(
        [*foreign, *pending, ready], reserve_russian=True,
        upcoming_ids=[item["id"] for item in foreign[:8]], taste_score=lambda item: item["fit"],
    )
    assert len(visible) == AUTO_CANDIDATE_LIMIT
    assert ready in visible


def test_language_projection_reserves_both_sides_without_evicting_visible_or_ready():
    known_unknown = [recording(index, "fallback", ready=True, language="unknown") for index in range(32)]
    russian = [recording(index, "ru", language="ru") for index in range(2)]
    foreign = [recording(index, "other", language="other") for index in range(2)]
    upcoming = [item["id"] for item in known_unknown[:8]]
    visible = window(
        [*known_unknown, *russian, *foreign], upcoming_ids=upcoming,
        language_reserves={"ru": 2, "foreign": 2},
    )
    assert len(visible) == AUTO_CANDIDATE_LIMIT
    assert [item["id"] for item in visible[:8]] == upcoming
    assert all(item in visible for item in russian + foreign)
