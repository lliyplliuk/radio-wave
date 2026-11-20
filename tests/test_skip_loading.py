"""Loading skips and foreground watchdogs with a deterministic clock."""

from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import pytest

from radio_wave import service as service_module
from radio_wave.online_radio import OnlineChoice
from radio_wave.queue import RadioQueue
from radio_wave.service import _OnlineRequest
from radio_wave.torrent_index import TorrentRelease
from test_service_scale import IndexedPlayer, TransferEntry, Transfers
from test_torrent_service_flow import wav


@pytest.fixture
def wave(tmp_path, monkeypatch):
    for key in ("XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(key, str(tmp_path / key))
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(service_module, "time", SimpleNamespace(monotonic=lambda: clock.now, time=lambda: clock.now))
    entry = TransferEntry("a" * 40, 4, tmp_path)
    transfers = Transfers([entry])
    player = IndexedPlayer()
    player_service = service_module.RadioService(player=player, torrents=transfers, online=False)
    player_service._save_source(entry, {"kind": "file"}, False)
    pinned = [track["id"] for track in player_service.upcoming()]
    player_service.pin_visible(pinned)
    yield player_service, player, entry, clock, pinned
    player_service.quit()


def complete(service, entry, track_id):
    index = service.catalogue.get(track_id)["file_index"]
    location = Path(entry.file_path(index))
    location.parent.mkdir(parents=True, exist_ok=True)
    location.write_bytes(wav(index + 1))
    entry.complete.add(index)
    service._refresh_catalogue()


def test_next_during_wait_plays_first_ready_preserving_other_shown_positions(wave):
    service, player, entry, _clock, (first, second, third, fourth) = wave
    complete(service, entry, third)
    complete(service, entry, fourth)
    service.toggle_play()
    assert service.waiting
    service.user_next()
    assert service.current()["id"] == third
    assert [track["id"] for track in service.upcoming()][:2] == [second, fourth]
    assert len(player.paths) == 1
    assert not service.state.early_skips
    assert not service._failed_torrents
    complete(service, entry, first)
    service.tick()
    assert service.current()["id"] == third
    assert first not in [track["id"] for track in service.upcoming()]
    assert [track["id"] for track in service.state.history] == [third]


@pytest.mark.parametrize("transition", ["next", "finished"])
def test_wave_transition_uses_ready_visible_fresh_track_before_discovery(wave, monkeypatch, transition):
    service, player, entry, _clock, (repeat, fresh, other, last) = wave
    for track_id in (repeat, fresh, other, last):
        complete(service, entry, track_id)
    service.state.remember(service.catalogue.get(repeat))
    service.queue.set_history(service.state.history)
    if transition == "finished":
        service.play_track(service.catalogue.get(last))
        service.tick()
    ready_before = [track["id"] for track in service.visible_upcoming() if track["state"] == "ready"]
    assert ready_before[:3] == [repeat, fresh, other]

    service._online_enabled = True
    discoveries = []
    monkeypatch.setattr(service, "_lookup_online", lambda: discoveries.append("foreground"))
    if transition == "next":
        service.user_next()
    else:
        player.eos()

    assert not discoveries
    assert service.is_playing() and not service.waiting
    assert service.current()["id"] == fresh
    assert [track["id"] for track in service.visible_upcoming()][:2] == [repeat, other]


def test_user_next_uses_ready_visible_track_behind_a_pending_pin(wave):
    service, player, entry, _clock, (pending, ready, third, fourth) = wave
    complete(service, entry, ready)

    service.user_next()

    assert service.is_playing() and not service.waiting
    assert service.current()["id"] == ready
    assert len(player.paths) == 1
    assert service.queue.pinned_ids() == (pending, third, fourth)


@pytest.mark.parametrize("with_discovery", [False, True])
def test_waiting_wave_transition_starts_visible_track_that_finishes_first(wave, monkeypatch, with_discovery):
    service, player, entry, clock, (pending, ready, third, fourth) = wave
    request = None
    if with_discovery:
        service.state.remember(service.catalogue.get(pending))
        service.queue.set_history(service.state.history)
        service._online_enabled = True
        future = Future()
        future.set_running_or_notify_cancel()
        request = _OnlineRequest(False, future=future, discovery=True)
        request.start_foreground(clock.now)

        def start_discovery(*, prefetch=False, **_options):
            if not prefetch:
                service._online = request
                service._awaiting = True

        monkeypatch.setattr(service, "_lookup_online", start_discovery)

    service.user_next()
    assert service.waiting
    if with_discovery:
        assert service._online is request
    else:
        assert service._pending_wait.track_id == pending
    complete(service, entry, ready)
    service.tick()

    assert service.is_playing() and not service.waiting
    assert service.current()["id"] == ready
    assert service.queue.pinned_ids() == (pending, third, fourth)
    if request is not None:
        assert request.cancelled.is_set() and service._online is None
        future.set_result([])
        service.tick()
    assert len(player.paths) == 1


def test_manual_selection_does_not_inherit_an_automatic_transition_wait(wave):
    service, player, entry, _clock, (exact, ready, _third, _fourth) = wave
    service.user_next()
    assert service.waiting
    service.play_track(service.catalogue.get(exact), request_label="Нужный файл")
    complete(service, entry, ready)
    service.tick()

    assert service.waiting and service._requested == exact
    assert not player.paths


def test_repeated_next_moves_through_pending_heads_and_cancels_late_foreground_result(wave):
    service, player, entry, _clock, (first, second, third, _fourth) = wave
    service.toggle_play()
    # A running future cannot be cancelled by Future.cancel(). Its result still
    # has to be discarded when playback advances to another candidate.
    future = Future()
    future.set_running_or_notify_cancel()
    request = _OnlineRequest(False, future=future, track_id=first)
    service._online = request
    service.user_next()
    assert request.cancelled.is_set()
    assert service.waiting and service._pending_wait.track_id == second
    future.set_result([])
    complete(service, entry, first)
    service.tick()
    service.user_next()
    assert service.waiting and service._pending_wait.track_id == third
    assert not player.paths
    assert not service.state.history
    assert not service.state.early_skips


@pytest.mark.parametrize("trickle", [False, True])
def test_foreground_wait_uses_short_limits_and_keeps_ready_sibling(wave, trickle):
    service, _player, entry, clock, (first, second, _third, _fourth) = wave
    complete(service, entry, second)
    service.toggle_play()
    assert service.waiting and service._pending_wait.track_id == first
    index = service.catalogue.get(first)["file_index"]
    service.tick()
    if trickle:
        for _ in range(3):
            clock.now += 4
            entry.files[index]["downloaded"] += 1024
            service.tick()
    else:
        clock.now += 4.9
        service.tick()
        assert service.waiting
        clock.now += 0.1
        service.tick()
    assert service.current()["id"] == second
    assert not service._failed_torrents


def test_manual_search_future_has_whole_budget_and_never_uses_unrelated_audio(wave):
    service, player, entry, clock, (_first, second, _third, _fourth) = wave
    complete(service, entry, second)
    future = Future()
    future.set_running_or_notify_cancel()
    request = _OnlineRequest(False, future=future, manual=True, label="Кино — Нужная песня")
    request.start_foreground(clock.now)
    service._online = request
    service._awaiting = True
    clock.now += 14.9
    service.tick()
    assert service.waiting
    clock.now += 0.1
    service.tick()
    assert request.cancelled.is_set()
    assert not service.waiting
    assert "Нужная песня" in service.status_text()
    future.set_result([])
    service.tick()
    assert not player.paths


def test_manual_pending_file_timeout_preserves_ready_sibling_without_playing_it(wave):
    service, player, entry, clock, (first, second, _third, _fourth) = wave
    complete(service, entry, second)
    service.play_track(service.catalogue.get(first), request_label="Нужный файл")
    clock.now += 5
    service.tick()
    assert not service.waiting
    assert "Нужный файл" in service.status_text()
    assert not player.paths
    assert service.queue.is_ready(second)


def test_expired_wave_search_uses_ready_candidate_behind_pending_head(wave):
    service, player, entry, clock, (first, second, _third, _fourth) = wave
    service.toggle_play()
    complete(service, entry, second)
    future = Future()
    future.set_running_or_notify_cancel()
    request = _OnlineRequest(False, future=future, discovery=True)
    request.start_foreground(clock.now)
    service._online = request
    clock.now += 15
    service.tick()
    assert request.cancelled.is_set()
    assert service.current()["id"] == second
    assert service.upcoming()[0]["id"] == first
    assert len(player.paths) == 1
    future.set_result([])


def test_next_excludes_metadata_source_for_wave_without_blocking_manual_search(wave):
    service, _player, _entry, clock, _pinned = wave
    service.toggle_play()
    release = TorrentRelease("Альбом", "magnet:unused", None, 10, "https://example.org/album")
    future = Future()
    future.set_running_or_notify_cancel()
    request = _OnlineRequest(False, choice=OnlineChoice(release), future=future, discovery=True)
    request.start_foreground(clock.now)
    service._online = request
    service.user_next()
    assert request.cancelled.is_set()
    assert release.source_url in service._sources_to_skip(include_saved=True)
    assert release.source_url not in service._sources_to_skip(include_saved=False)
    assert not service._failed_releases
    future.set_result(None)


def test_timeout_fallback_can_use_ready_repeat_outside_eight_pending_slots():
    tracks = [{"id": str(i), "title": f"Песня {i}", "artist": "Группа", "ready": i == 9} for i in range(10)]
    queue = RadioQueue(lookahead=8, history=[tracks[-1]])
    queue.set_catalogue(tracks)
    assert len(queue.upcoming) == 8
    assert all(not track["ready"] for track in queue.upcoming)
    assert queue.next_available()["id"] == "9"


def test_background_source_retains_eighteen_ninety_limits_when_audio_plays(wave):
    service, _player, entry, clock, (first, _second, _third, _fourth) = wave
    complete(service, entry, first)
    service.toggle_play()
    release = TorrentRelease("Альбом", "magnet:unused", None, 10, "https://example.org/album")
    request = _OnlineRequest(True)
    request.choices.append(OnlineChoice(release))
    service._online = request
    # Mimic a magnet waiting for metadata, without network calls.
    def open_source(active, metadata=None):
        active.entry = SimpleNamespace(files=[], state="downloading", peers=0, info_hash=entry.info_hash)
        active.deadline = clock.now + active.inactivity_limit

    service._open_online_source = open_source
    service._poll_online()
    assert request.deadline == clock.now + 18
    assert request.attempt_ends == clock.now + 90
    clock.now += 15
    service.tick()
    assert service._online is request and service.current()["id"] == first
