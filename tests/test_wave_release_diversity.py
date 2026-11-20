from radio_wave.queue import RadioQueue


def track(ident, artist, release, ready=True, language="ru", **fields):
    return {
        "id": ident, "title": f"Трек {ident}", "artist": artist, "language": language,
        "genre": "Рок", "source": "torrent", "info_hash": release, "ready": ready, **fields,
    }


def upcoming(queue):
    return [item["id"] for item in queue.upcoming]


def test_automatic_album_fallback_plays_all_unheard_tracks_in_a_stable_shuffle():
    release = "2542e5876a307b78adddc29a4d6013ac25b03b44"
    indices = [2, 3, 12, 13, 14, 15]
    album = [track(f"{release}:{index}", "Кино", release, file_index=index) for index in indices]
    queue = RadioQueue()
    queue.set_catalogue(album, automatic_sources={release})
    planned = upcoming(queue)

    queue.set_catalogue(list(reversed(album)), automatic_sources={release})
    queue.fill()
    assert upcoming(queue) == planned
    actual = [queue.next_track(allow_repeats=False)["file_index"] for _ in album]
    assert sorted(actual) == indices
    assert actual != indices
    assert actual != sorted(indices, key=str)


def test_fourth_visible_slot_prefers_another_ready_release_without_moving_pins():
    pins = [
        track("a", "Дельфин", "first"),
        track("b", "Кино", "second"),
        track("c", "ABBA", "compilation", path="/fixture/Collection/ABBA.wav"),
    ]
    same_release = track("d", "Muse", "compilation", path="/fixture/Collection/Muse.wav")
    other_release = track("z", "Алиса", "new-release")
    queue = RadioQueue(history=[track("history", "Алиса", "old-release")])
    sources = {item["info_hash"] for item in [*pins, same_release, other_release]}
    queue.set_catalogue(pins, automatic_sources=sources)
    visible_ids = [item["id"] for item in queue.visible_upcoming()]
    queue.pin_visible(visible_ids)

    queue.set_catalogue([*pins, same_release, other_release], automatic_sources=sources)

    assert [item["id"] for item in queue.visible_upcoming()] == [*visible_ids, other_release["id"]]
    assert upcoming(queue)[4:] == [same_release["id"]]


def test_diversifies_large_release_with_eligible_pending_rock_songs():
    album = [track(f"album-{i:02}", "Дельфин", "one-release") for i in range(25)]
    album[1]["artist"] = "Название сборника"
    same_artist = track("other-codec", " ДЕЛЬФИН ", "second-hash")
    russian = track("new-ru", "АукцЫон", "third-hash", ready=False)
    foreign = track("new-en", "Muse", "fourth-hash", ready=False, language="en")
    unavailable = track("missing", "Radiohead", "missing", ready=False, unavailable=True)
    queue = RadioQueue(early_skips=[album[3]])
    queue.set_catalogue(album)
    assert queue.select("album-00")["id"] == "album-00"
    shown = upcoming(queue)[:1]
    queue.pin_visible(shown)

    queue.set_catalogue([*album, same_artist, russian, foreign, unavailable])
    assert upcoming(queue)[:1] == shown
    assert {"new-ru", "new-en"} <= set(upcoming(queue)[1:])
    assert "missing" not in upcoming(queue)
    assert queue.select("other-codec")["id"] == "other-codec"
    assert queue.pinned_ids() == tuple(shown)


def test_different_tags_in_same_release_and_case_variants_are_spaced():
    first = track("a", "Кино", "one")
    group_release = track("b", "Виктор Цой", "ONE")
    same_artist = track("c", "  КИНО  ", "codec")
    pending = track("d", "Ария", "rock", ready=False)
    queue = RadioQueue()
    queue.set_catalogue([first, group_release, same_artist, pending])
    assert upcoming(queue)[:2] == ["a", "d"]
    queue.set_broken("d")
    assert set(upcoming(queue)) == {"a", "b", "c"}


def test_discovery_offer_waits_for_unpublished_slot_and_is_consumed():
    queue = RadioQueue(lookahead=4)
    existing = [track(str(i), "Дельфин", "album") for i in range(7)]
    queue.set_catalogue(existing)
    visible = upcoming(queue)
    queue.pin_visible(visible)
    foreign = track("foreign", "Muse", "separate", ready=False, language="en")
    other = track("other", "Blur", "another", ready=False, language="en")
    queue.set_catalogue([*existing, foreign, other])
    assert queue.offer_discovery("foreign")
    assert not queue.offer_discovery("other")
    assert upcoming(queue) == visible
    assert queue.offered_id() == "foreign"

    assert queue.next_track()["id"] == visible[0]
    assert upcoming(queue) == [*visible[1:], "foreign"]
    assert queue.offered_id() == "foreign"
    queue.set_ready("foreign")
    assert queue.select("foreign")["id"] == "foreign"
    assert queue.offered_id() is None
    assert queue.offer_discovery("other")
    queue.set_broken("other")
    assert queue.offered_id() is None
    assert not queue.offer_discovery("other")
