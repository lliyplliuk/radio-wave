"""Воспроизведение через GStreamer playbin."""

from __future__ import annotations

import os
from math import isfinite
from typing import Callable, Optional

PLAYBIN_VIDEO = 1 << 0
PLAYBIN_TEXT = 1 << 2


def _gst():
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    return Gst


class Player:
    """Тонкая обёртка над Gst.Element 'playbin'."""

    def __init__(self) -> None:
        Gst = _gst()
        Gst.init(None)
        self._Gst = Gst
        self._playbin = Gst.ElementFactory.make("playbin", "player")
        if self._playbin is None:
            raise RuntimeError("Не удалось создать GStreamer playbin")
        flags = self._playbin.get_property("flags")
        self._playbin.set_property("flags", int(flags) & ~(PLAYBIN_VIDEO | PLAYBIN_TEXT))
        self._bus = self._playbin.get_bus()
        self._bus.add_signal_watch()
        self._bus_handler = self._bus.connect("message", self._on_message)
        self._current_uri = ""
        self._on_eos: Optional[Callable[[], None]] = None
        self._on_error: Optional[Callable[[str], None]] = None

    # ------------------------------------------------------------ callbacks
    def set_on_eos(self, callback: Optional[Callable[[], None]]) -> None:
        self._on_eos = callback

    def set_on_error(self, callback: Optional[Callable[[str], None]]) -> None:
        self._on_error = callback

    def _on_message(self, _bus, message) -> None:
        Gst = self._Gst
        if message.type == Gst.MessageType.EOS:
            if self._on_eos:
                self._on_eos()
        elif message.type == Gst.MessageType.ERROR:
            err, _debug = message.parse_error()
            if self._on_error:
                self._on_error(str(err))

    # -------------------------------------------------------------- loading
    def load(self, path: str) -> bool:
        Gst = self._Gst
        if not path or not os.path.isfile(path):
            return False
        uri = Gst.filename_to_uri(os.path.abspath(path))
        self._playbin.set_state(Gst.State.NULL)
        self._playbin.set_property("uri", uri)
        self._current_uri = uri
        self._playbin.set_state(Gst.State.PAUSED)
        return True

    def play(self) -> None:
        Gst = self._Gst
        self._playbin.set_state(Gst.State.PLAYING)

    def pause(self) -> None:
        Gst = self._Gst
        self._playbin.set_state(Gst.State.PAUSED)

    def toggle(self) -> None:
        if self.is_playing():
            self.pause()
        else:
            self.play()

    def stop(self) -> None:
        Gst = self._Gst
        self._playbin.set_state(Gst.State.NULL)

    # --------------------------------------------------------------- state
    def is_playing(self) -> bool:
        Gst = self._Gst
        _, state, _ = self._playbin.get_state(0)
        return state == Gst.State.PLAYING

    def is_loaded(self) -> bool:
        return bool(self._current_uri)

    def current_path(self) -> str:
        if not self._current_uri:
            return ""
        from urllib.parse import unquote, urlparse
        parsed = urlparse(self._current_uri)
        return unquote(parsed.path)

    def position(self) -> float:
        Gst = self._Gst
        ok, value = self._playbin.query_position(Gst.Format.TIME)
        if ok and value >= 0:
            return value / 1_000_000_000.0
        return 0.0

    def duration(self) -> float:
        Gst = self._Gst
        ok, value = self._playbin.query_duration(Gst.Format.TIME)
        if ok and value > 0:
            return value / 1_000_000_000.0
        return 0.0

    def can_seek(self) -> bool:
        Gst = self._Gst
        query = Gst.Query.new_seeking(Gst.Format.TIME)
        return self._playbin.query(query) and query.parse_seeking()[1]

    def seek(self, seconds: float) -> bool:
        Gst = self._Gst
        duration = self.duration()
        if duration <= 0 or not isfinite(seconds) or not self.can_seek():
            return False
        target = max(0.0, min(seconds, duration))
        return self._playbin.seek_simple(
            Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE, int(target * Gst.SECOND),
        )

    def set_volume(self, value: float) -> None:
        self._playbin.set_property("volume", max(0.0, min(1.0, float(value))))

    def volume(self) -> float:
        return float(self._playbin.get_property("volume") or 0.0)

    def detach_callbacks(self) -> None:
        self._on_eos = self._on_error = None
        if self._bus_handler is None:
            return
        handler, self._bus_handler = self._bus_handler, None
        try:
            self._bus.disconnect(handler)
        finally:
            self._bus.remove_signal_watch()

    def shutdown(self) -> None:
        try:
            self.detach_callbacks()
        finally:
            self.stop()
