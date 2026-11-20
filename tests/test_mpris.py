"""Verify the exported MPRIS interfaces on their own real D-Bus session."""

import shutil
import subprocess
from concurrent.futures import Future

import pytest

pytest.importorskip("gi")
from gi.repository import Gio, GLib

from radio_wave import config
from radio_wave.mpris import BUS_NAME, OBJECT_PATH, PLAYER, PROPERTIES, ROOT, MprisPlayer
from radio_wave.service import RadioService, _OnlineRequest
from test_repaired_playback import ClockedPlayer
from test_torrent_service_flow import wav


class TransportPlayer(ClockedPlayer):
    def __init__(self):
        super().__init__()
        self.play_calls = 0
        self.pause_calls = 0

    def play(self):
        self.play_calls += 1
        super().play()

    def pause(self):
        self.pause_calls += 1
        self.playing = False

    def toggle(self):
        if self.playing:
            self.pause()
        else:
            self.play()

    def stop(self):
        self.at = 0.0
        super().stop()

    def can_seek(self):
        return bool(self.paths)


@pytest.fixture
def bus():
    daemon = shutil.which("dbus-daemon")
    if daemon is None:
        pytest.skip("dbus-daemon is unavailable")
    address_flags = Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
    with subprocess.Popen([daemon, "--session", "--nofork", "--print-address=1"], stdout=subprocess.PIPE,
                          text=True) as proc:
        assert proc.stdout is not None
        connections = []
        try:
            address = proc.stdout.readline().strip()
            for _ in range(2):
                connections.append(Gio.DBusConnection.new_for_address_sync(address, address_flags, None, None))
            yield connections
        finally:
            for connection in connections:
                connection.close_sync(None)
            proc.terminate()
            proc.wait(timeout=3)


def call(connection, name, path, interface, method, parameters=None):
    replies = []

    def done(client, result):
        try:
            replies.append(client.call_finish(result).unpack())
        except GLib.Error as error:
            replies.append(error)

    connection.call(name, path, interface, method, parameters, None, Gio.DBusCallFlags.NONE, 1500, None, done)
    while not replies:
        GLib.MainContext.default().iteration(True)
    if isinstance(replies[0], GLib.Error):
        raise replies[0]
    return replies[0]


@pytest.fixture
def radio(tmp_path, monkeypatch, bus):
    for key in ("XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(key, str(tmp_path / key))
    server, client = bus
    player = TransportPlayer()
    service = RadioService(player=player, online=False, start_torrents=False)
    for i in range(3):
        path = tmp_path / f"Трек {i}.wav"
        path.write_bytes(wav(i + 1))
        service.import_paths([str(path)])
        track = next(track for track in service.tracks() if track["path"] == str(path))
        service.update_track(track["id"], artist="Кино")
    raises = []
    media_keys = MprisPlayer(server, service, lambda: raises.append(True))
    try:
        yield service, player, client, media_keys, raises
    finally:
        media_keys.close()
        service.quit()


def mpris(client, method, parameters=None, *, interface=PLAYER):
    return call(client, BUS_NAME, OBJECT_PATH, interface, method, parameters)


def properties(client, interface=PLAYER):
    return mpris(client, "GetAll", GLib.Variant("(s)", (interface,)), interface=PROPERTIES)[0]


def test_real_bus_transports_and_metadata(radio):
    service, player, client, media_keys, raises = radio
    root = properties(client, ROOT)
    assert root["Identity"] == config.APP_NAME and root["DesktopEntry"] == "radio-wave"
    assert root["CanRaise"] and not root["CanQuit"] and not root["HasTrackList"]
    mpris(client, "Raise", interface=ROOT)
    assert raises == [True]
    idle = properties(client)
    assert idle["PlaybackStatus"] == "Stopped" and idle["Metadata"] == {}
    assert idle["CanPlay"] and idle["CanGoNext"] and "Shuffle" not in idle and "LoopStatus" not in idle

    mpris(client, "Play")
    mpris(client, "Play")
    assert player.play_calls == 1
    service.tick()
    first = service.current()
    playing = properties(client)
    track_id = playing["Metadata"]["mpris:trackid"]
    assert GLib.Variant.is_object_path(track_id)
    assert playing["Metadata"]["mpris:length"] == 100_000_000
    assert playing["Metadata"]["xesam:title"] == first["title"]
    assert playing["Metadata"]["xesam:artist"] == ["Кино"]
    assert playing["PlaybackStatus"] == "Playing"

    mpris(client, "Pause")
    mpris(client, "Pause")
    assert player.pause_calls == 1 and properties(client)["PlaybackStatus"] == "Paused"
    mpris(client, "PlayPause")
    assert player.play_calls == 2
    mpris(client, "Next")
    assert service.current()["id"] != first["id"]
    assert [track["id"] for track in service.state.early_skips] == [first["id"]]
    new_id = properties(client)["Metadata"]["mpris:trackid"]
    mpris(client, "SetPosition", GLib.Variant("(ox)", (track_id, 2_000_000)))
    assert player.at == 0
    mpris(client, "SetPosition", GLib.Variant("(ox)", (new_id, 2_000_000)))
    assert player.at == 2.0 and properties(client)["Position"] == 2_000_000
    mpris(client, "Previous")
    assert service.current()["id"] == first["id"]
    mpris(client, "Stop")
    mpris(client, "Stop")
    assert not player.playing and properties(client)["PlaybackStatus"] == "Stopped"
    mpris(client, "Play")
    assert service.current()["id"] == first["id"] and player.playing and player.at == 0

    media_keys.close()
    owner = call(client, "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                 "NameHasOwner", GLib.Variant("(s)", (BUS_NAME,)))
    assert owner == (False,)


def test_detach_disables_control_before_releasing_bus_name(radio):
    service, player, client, media_keys, _raises = radio
    assert service.on_seek is not None
    media_keys.detach()
    assert service.on_seek is None
    owner = call(client, "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                 "NameHasOwner", GLib.Variant("(s)", (BUS_NAME,)))
    assert owner == (True,)
    with pytest.raises(GLib.Error, match="UnknownMethod"):
        mpris(client, "Play")
    assert player.play_calls == 0
    media_keys.release_name()


def test_properties_updates_ignore_ticking_position_and_stop_cancels_wait(radio):
    service, player, client, media_keys, _raises = radio
    updates, seeks = [], []
    subscriptions = [
        client.signal_subscribe(None, PROPERTIES, "PropertiesChanged", OBJECT_PATH, None,
                                Gio.DBusSignalFlags.NONE, lambda *args: updates.append(args[-1].unpack())),
        client.signal_subscribe(None, PLAYER, "Seeked", OBJECT_PATH, None,
                                Gio.DBusSignalFlags.NONE, lambda *args: seeks.append(args[-1].unpack())),
    ]
    try:
        mpris(client, "Play")
        properties(client)
        assert any(change[1].get("PlaybackStatus") == "Playing" for change in updates)
        updates.clear()
        player.at = 1.25
        media_keys.refresh()
        assert properties(client)["Position"] == 1_250_000
        assert not updates
        mpris(client, "Seek", GLib.Variant("(x)", (-2_000_000,)))
        properties(client)
        assert seeks == [(0,)]
        assert not updates
        player.stop()
        service.queue.current = None
        service._awaiting = True
        request = _OnlineRequest(False, future=Future())
        service._online = request
        media_keys.refresh()
        mpris(client, "Stop")
        assert request.cancelled.is_set() and request.future.cancelled() and not service.waiting
        assert properties(client)["PlaybackStatus"] == "Stopped"
    finally:
        for subscription in subscriptions:
            client.signal_unsubscribe(subscription)


def test_previous_walks_older_ready_history_without_bouncing(radio):
    service, player, client, _media_keys, _raises = radio
    mpris(client, "Play")
    service.tick()
    first_id = service.current()["id"]
    mpris(client, "Next")
    service.tick()
    second_id = service.current()["id"]
    mpris(client, "Next")
    service.tick()
    assert service.current()["id"] not in {first_id, second_id}
    mpris(client, "Previous")
    service.tick()
    assert service.current()["id"] == second_id
    mpris(client, "Previous")
    service.tick()
    assert service.current()["id"] == first_id
    player.at = 10.0
    mpris(client, "Previous")
    assert service.current()["id"] == first_id and player.at == 0


def test_next_and_previous_preserve_pause_without_starting_audio(radio):
    service, player, client, _media_keys, _raises = radio
    mpris(client, "Play")
    service.tick()
    first_id = service.current()["id"]
    mpris(client, "Pause")
    mpris(client, "Next")
    assert service.current()["id"] != first_id
    assert properties(client)["PlaybackStatus"] == "Paused" and player.play_calls == 1
    mpris(client, "Previous")
    assert service.current()["id"] == first_id
    assert properties(client)["PlaybackStatus"] == "Paused" and player.play_calls == 1
    assert [entry["id"] for entry in service.state.history] == [first_id]
    mpris(client, "Stop")
    mpris(client, "Next")
    assert service.current()["id"] != first_id
    assert properties(client)["PlaybackStatus"] == "Stopped" and player.play_calls == 1


def test_next_during_buffering_dispatches_to_wait_skip_without_feedback(radio):
    service, player, client, media_keys, _raises = radio
    first = service.upcoming()[0]["id"]
    stopped = _OnlineRequest(False, track_id=first, future=Future())
    service._online = stopped
    service._radio_active = service._awaiting = True
    media_keys.refresh()
    mpris(client, "Next")
    assert stopped.cancelled.is_set() and service.queue.is_skipped(first)
    assert service.current()["id"] != first and player.playing
    assert not service.state.early_skips
