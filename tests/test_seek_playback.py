"""GTK pointer and keyboard input against a real seekable GStreamer MP3."""

import os
from pathlib import Path
import shutil
import subprocess
import time

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("GdkX11", "4.0")
from gi.repository import GdkX11, Gio, GLib, Gtk

from radio_wave.library import Catalogue
from radio_wave.player import Player
from radio_wave.service import RadioService
from radio_wave.state import AppState
from radio_wave.ui.app import RadioWaveApp


def _pump(seconds):
    deadline = time.monotonic() + seconds
    context = GLib.MainContext.default()
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        time.sleep(0.005)


def _wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _pump(0.02)
        if predicate():
            return
    pytest.fail("GTK или GStreamer не перешёл в ожидаемое состояние")


def test_pointer_and_keyboard_seek_a_playing_or_paused_mp3(tmp_path, monkeypatch):
    if os.environ.get("RADIO_WAVE_RUN_X11_INPUT_TEST") != "1":
        pytest.skip("ввод мышью и клавиатурой проверяется только на отдельном дисплее")
    if not os.environ.get("DISPLAY") or not Gtk.init_check():
        pytest.skip("требуется X11")
    ffmpeg = shutil.which("ffmpeg")
    xdotool = shutil.which("xdotool")
    xenv = os.environ.copy()
    if not xdotool:
        qa = Path(__file__).resolve().parents[1] / ".venv/qa/usr"
        bundled = qa / "bin/xdotool"
        if bundled.is_file():
            xdotool = str(bundled)
            xenv["LD_LIBRARY_PATH"] = str(qa / "lib/x86_64-linux-gnu")
    if not ffmpeg or not xdotool:
        pytest.skip("требуются ffmpeg и xdotool")
    for kind in ("DATA", "CACHE", "STATE"):
        monkeypatch.setenv(f"XDG_{kind}_HOME", str(tmp_path / kind))

    audio = tmp_path / "tone.mp3"
    subprocess.run([
        ffmpeg, "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=45",
        "-q:a", "2", str(audio),
    ], check=True)
    player = Player()
    sink = player._Gst.ElementFactory.make("fakesink")
    assert sink is not None
    sink.set_property("sync", True)
    player._playbin.set_property("audio-sink", sink)
    service = RadioService(
        state=AppState(str(tmp_path / "state.json")), catalogue=Catalogue(str(tmp_path / "catalogue.json")),
        player=player, torrents=None, online=False,
    )
    app = RadioWaveApp(service=service)
    app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
    app.register(None)
    app.activate()
    window = app.get_active_window()
    try:
        surface = window.get_surface()
        if not isinstance(surface, GdkX11.X11Surface):
            pytest.skip("требуется окно X11")
        xid = str(surface.get_xid())

        def xdo(*args):
            subprocess.run([xdotool, *map(str, args)], env=xenv, check=True)

        called = []
        seek = player.seek

        def seek_probe(seconds):
            called.append(seconds)
            return seek(seconds)

        monkeypatch.setattr(player, "seek", seek_probe)
        service.import_paths([str(audio)])
        service.play_current()
        scale = window._seek_scale
        _wait_for(lambda: player.is_playing() and scale.get_adjustment().get_upper() > 40)
        valid, bounds = scale.compute_bounds(window)
        assert valid
        center_y = int(bounds.get_y() + bounds.get_height() / 2)

        def move(fraction):
            x = int(bounds.get_x() + bounds.get_width() * fraction)
            xdo("mousemove", "--window", xid, x, center_y)

        xdo("windowfocus", xid)
        move(0.70)
        xdo("click", 1)
        _wait_for(lambda: 27 < player.position() < 36)
        assert len(called) == 1

        move(0.70)
        xdo("mousedown", 1)
        _pump(0.08)
        move(0.30)
        _pump(0.65)  # More than one refresh period, while the pointer is held.
        assert 11 < scale.get_value() < 16
        assert player.position() > 27
        assert len(called) == 1
        xdo("mouseup", 1)
        _wait_for(lambda: 11 < player.position() < 17)
        assert player.is_playing()
        assert len(called) == 2

        player.pause()
        _wait_for(lambda: not player.is_playing())
        window.refresh()
        paused_position = player.position()
        scale.grab_focus()
        xdo("key", "Right")
        _wait_for(lambda: player.position() > paused_position + 0.4)
        assert not player.is_playing()
        assert len(called) == 3
    finally:
        window.close()
        app.quit()
