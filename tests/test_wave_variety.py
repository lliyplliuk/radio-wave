"""Разнообразие будущих и фактически выбранных песен."""

from radio_wave.queue import RadioQueue
from radio_wave.ranker import genre_family


def track(track_id, artist, *, language="ru", genre="Rock", **extra):
    return dict(id=track_id, artist=artist, title=track_id, language=language, genre=genre, **extra)


def ids(queue):
    return [item["id"] for item in queue.upcoming]


def mixed():
    russian_rock = [track(f"a-{index:02}", f"Исполнитель {index}") for index in range(12)]
    alternatives = [
        track("z-english", "English artist", language="en", genre="Pop"),
        track("z-foreign", "Andere Musiker", language="other", genre="Jazz"),
    ]
    return russian_rock, alternatives


def test_regular_play_and_download_order_vary_language_and_genre_without_pins():
    russian_rock, alternatives = mixed()
    queue = RadioQueue(lookahead=6)
    queue.set_catalogue([*russian_rock, *alternatives])

    planned = list(queue.upcoming)
    assert {item["language"] for item in planned[:4]} >= {"ru", "en", "other"}
    assert {genre_family(item["genre"]) for item in planned[:4]} >= {"rock", "pop", "jazz"}

    played = [queue.next_track() for _ in range(4)]
    assert not queue.pinned_ids()
    assert {item["language"] for item in played} >= {"ru", "en", "other"}
    assert len({item["id"] for item in played}) == 4


def test_hidden_tail_respects_recent_played_and_published_prefix():
    russian_rock, alternatives = mixed()
    history = [track("played", "Отдельный артист")]
    queue = RadioQueue(lookahead=6, history=history)
    queue.set_catalogue(russian_rock)
    shown = ids(queue)[:2]
    queue.pin_visible(shown)
    queue.set_catalogue([*russian_rock, *alternatives])

    assert ids(queue)[:2] == shown
    assert queue.upcoming[2]["language"] in {"en", "other"}
    assert [queue.next_track()["id"] for _ in range(2)] == shown
    assert queue.next_track()["language"] in {"en", "other"}


def test_unknown_metadata_does_not_count_as_alternative():
    current = track("first", "Первый")
    unknown = track("a-unknown", "Другой", language="unknown", genre="")
    different = track("z-known", "Третий", language="en", genre="Jazz")
    queue = RadioQueue(lookahead=3)
    queue.set_catalogue([current, unknown, different])
    queue.select(current["id"])
    assert queue.upcoming[0]["id"] == different["id"]
    assert queue.next_track()["id"] == different["id"]


def test_provisional_release_names_share_artist_without_merging_tagged_names():
    first_release = "Linkin Park - 2024 - From Zero"
    second_release = "Linkin Park - From Zero (2CD Deluxe Edition) (2025)"
    first = track("current", first_release, duration=0.0,
                  path=f"/cache/{first_release}/current.mp3", source="torrent", info_hash="one")
    second = track("a-other-release", second_release, duration=0.0, ready=False,
                   path=f"/cache/{second_release}/a-other-release.mp3", source="torrent", info_hash="two")
    real = track("z-tag-edit", second_release, source="torrent", duration=0.0, edited=("artist",), ready=False,
                 path=f"/cache/{second_release}/z-tag-edit.mp3", info_hash="three")
    queue = RadioQueue(lookahead=2)
    queue.set_catalogue([first, second, real])
    queue.select(first["id"])
    queue.set_history([{
        key: first[key] for key in ("id", "artist", "title", "genre", "language")
    }])

    assert ids(queue) == [real["id"], second["id"]]


def test_diversity_does_not_override_strong_dislikes_or_block_the_last_fallback():
    first = track("first", "Первый")
    suitable = track("suitable", "Второй")
    suppressed = track("suppressed", "Третий", language="en", genre="Шансон")
    dislikes = [track(f"unliked-{index}", "Шансонье", language="unknown", genre="Chanson") for index in range(11)]
    queue = RadioQueue(lookahead=3)
    queue.set_catalogue([first, suitable, suppressed])
    queue.select(first["id"], disliked=dislikes)

    assert queue.next_track()["id"] == suitable["id"]
    assert queue.next_track()["id"] == suppressed["id"]


def test_lower_scored_offer_reserves_one_hidden_slot_in_full_viable_window():
    liked = track("tasted", "Любимая группа")
    roster = [track(f"ready-{index:02}", "Любимая группа") for index in range(24)]
    proposed = track("offered", "Другой исполнитель", language="en", genre="Jazz", ready=False)
    queue = RadioQueue(lookahead=8)
    queue.fill(liked=[liked])
    queue.set_catalogue([*roster, proposed])
    assert proposed["id"] not in ids(queue)
    published = ids(queue)[:4]
    queue.pin_visible(published)

    assert queue.offer_discovery(proposed["id"])
    assert queue.offered_id() == proposed["id"]
    assert ids(queue)[:4] == published
    assert ids(queue)[-1] == proposed["id"]
    assert queue.next_track()["id"] == published[0]


def test_strongly_disfavored_offer_releases_reservation_when_other_slots_are_viable():
    roster = [track(f"ready-{index:02}", "Любимая группа") for index in range(24)]
    rejected = track("rejected", "Другой исполнитель", language="en", genre="Шансон", ready=False)
    proposed = track("offered", "Группа", language="en", genre="Jazz", ready=False)
    dislikes = [track(f"unliked-{index}", "Шансонье", language="unknown", genre="Chanson") for index in range(11)]
    queue = RadioQueue(lookahead=8)
    queue.set_catalogue([*roster, rejected, proposed])
    queue.fill(liked=[track("taste", "Любимая группа")], disliked=dislikes)

    assert not queue.offer_discovery(rejected["id"])
    assert queue.offered_id() is None
    assert queue.offer_discovery(proposed["id"])
    assert proposed["id"] in ids(queue)


def test_exhausted_offline_collection_varies_between_comparably_old_repeats():
    oldest = track("oldest", "Первый")
    variation = track("variation", "Другой", language="en", genre="Jazz")
    middle = track("middle", "Второй")
    latest = track("latest", "Третий")
    history = [oldest, variation, middle, latest]
    queue = RadioQueue(lookahead=4, history=history)
    queue.set_catalogue(history)

    assert queue.next_track(allow_repeats=False) is None
    assert queue.next_track()["id"] == variation["id"]
    assert queue.next_track()["id"] == oldest["id"]


def test_negative_language_does_not_force_old_unknown_when_only_fresh_language_is_known():
    unknown = track("known-fallback", "Группа", language="unknown")
    recent = track("recent-english", "English band", language="en")
    fresh = track("fresh-english", "Fresh band", language="en")
    dislikes = [track(f"disliked-{index}", "English artist", language="en") for index in range(18)]
    queue = RadioQueue(lookahead=4, history=[unknown, recent])
    queue.fill(disliked=dislikes)
    queue.set_catalogue([unknown, fresh])

    assert queue.next_track()["id"] == fresh["id"]
