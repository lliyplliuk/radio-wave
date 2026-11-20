"""GNOME and other desktop transport controls over the session bus."""

from hashlib import sha256
from math import isfinite

from gi.repository import Gio, GLib

from .config import APP_NAME


BUS_NAME = "org.mpris.MediaPlayer2.RadioWave"
OBJECT_PATH = "/org/mpris/MediaPlayer2"
ROOT = "org.mpris.MediaPlayer2"
PLAYER = f"{ROOT}.Player"
PROPERTIES = "org.freedesktop.DBus.Properties"
NO_TRACK = f"{OBJECT_PATH}/TrackList/NoTrack"
MICROSECONDS = 1_000_000

_INTROSPECTION = f"""
<node>
  <interface name="{ROOT}">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="{PLAYER}">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <method name="Seek"><arg name="Offset" type="x" direction="in"/></method>
    <method name="SetPosition">
      <arg name="TrackId" type="o" direction="in"/>
      <arg name="Position" type="x" direction="in"/>
    </method>
    <method name="OpenUri"><arg name="Uri" type="s" direction="in"/></method>
    <signal name="Seeked"><arg name="Position" type="x"/></signal>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="Rate" type="d" access="readwrite"/>
    <property name="Metadata" type="a{{sv}}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read">
      <annotation name="org.freedesktop.DBus.Property.EmitsChangedSignal" value="false"/>
    </property>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>
"""


def _micros(seconds: float) -> int:
    return int(max(0.0, seconds) * MICROSECONDS) if isfinite(seconds) else 0


def _track_path(track: dict | None) -> str:
    if track is None:
        return NO_TRACK
    token = sha256(track["id"].encode()).hexdigest()
    return f"{OBJECT_PATH}/Track/{token}"


class MprisPlayer:
    def __init__(self, connection: Gio.DBusConnection, service, raise_window) -> None:
        self._connection = connection
        self._service = service
        self._raise_window = raise_window
        self._registered = []
        self._owner = 0
        self._previous = {}
        self._root = {
            "CanQuit": GLib.Variant("b", False),
            "CanRaise": GLib.Variant("b", True),
            "HasTrackList": GLib.Variant("b", False),
            "Identity": GLib.Variant("s", APP_NAME),
            "DesktopEntry": GLib.Variant("s", "radio-wave"),
            "SupportedUriSchemes": GLib.Variant("as", []),
            "SupportedMimeTypes": GLib.Variant("as", []),
        }
        self._actions = {
            "Play": service.play_playback,
            "Pause": service.pause_playback,
            "PlayPause": service.toggle_play,
            "Previous": service.previous_track,
            "Stop": service.stop_playback,
        }
        try:
            for interface in Gio.DBusNodeInfo.new_for_xml(_INTROSPECTION).interfaces:
                self._registered.append(connection.register_object(
                    OBJECT_PATH, interface, self._on_method, self._get_property, self._set_property,
                ))
            self._owner = Gio.bus_own_name_on_connection(
                connection, BUS_NAME, Gio.BusNameOwnerFlags.DO_NOT_QUEUE, None, None,
            )
        except GLib.Error:
            self.close()
            raise
        self._previous = self._player_properties()
        service.on_seek = self._seeked

    def detach(self) -> None:
        if self._service.on_seek == self._seeked:
            self._service.on_seek = None
        for item in self._registered:
            self._connection.unregister_object(item)
        self._registered.clear()

    def release_name(self) -> None:
        owner, self._owner = self._owner, 0
        if owner:
            Gio.bus_unown_name(owner)

    def close(self) -> None:
        try:
            self.detach()
        finally:
            self.release_name()

    def _metadata(self) -> GLib.Variant:
        track = self._service.current()
        data = {}
        if track is not None:
            data["mpris:trackid"] = GLib.Variant("o", _track_path(track))
            data["xesam:title"] = GLib.Variant("s", track.get("title") or "")
            data["xesam:artist"] = GLib.Variant("as", [track["artist"]] if track.get("artist") else [])
            length = _micros(self._service.duration() or track.get("duration") or 0.0)
            if length:
                data["mpris:length"] = GLib.Variant("x", length)
        return GLib.Variant("a{sv}", data)

    def _player_properties(self) -> dict:
        service = self._service
        can_play = service.can_play()
        can_control = service.player is not None
        can_seek = service.current() is not None and service.can_seek()
        return {
            "PlaybackStatus": GLib.Variant("s", service.playback_status()),
            "Rate": GLib.Variant("d", 1.0),
            "Metadata": self._metadata(),
            "Volume": GLib.Variant("d", service.volume()),
            "MinimumRate": GLib.Variant("d", 1.0),
            "MaximumRate": GLib.Variant("d", 1.0),
            "CanGoNext": GLib.Variant("b", service.can_go_next()),
            "CanGoPrevious": GLib.Variant("b", can_seek or service.can_go_previous()),
            "CanPlay": GLib.Variant("b", can_play),
            "CanPause": GLib.Variant("b", can_play),
            "CanSeek": GLib.Variant("b", can_seek),
            "CanControl": GLib.Variant("b", can_control),
        }

    def refresh(self) -> None:
        if not self._registered or self._connection.is_closed():
            return
        values = self._player_properties()
        changed = {key: value for key, value in values.items() if value != self._previous.get(key)}
        self._previous = values
        if changed:
            self._emit(PROPERTIES, "PropertiesChanged", GLib.Variant("(sa{sv}as)", (PLAYER, changed, [])))

    def _emit(self, interface: str, name: str, parameters: GLib.Variant) -> None:
        try:
            self._connection.emit_signal(None, OBJECT_PATH, interface, name, parameters)
        except GLib.Error:
            pass  # A closed session bus must not affect local playback.

    def _seeked(self, seconds: float) -> None:
        self._emit(PLAYER, "Seeked", GLib.Variant("(x)", (_micros(seconds),)))

    def _get_property(self, _connection, _sender, _path, interface, name):
        if interface == ROOT:
            return self._root[name]
        if name == "Position":
            return GLib.Variant("x", _micros(self._service.position()))
        return self._previous[name]

    def _set_property(self, _connection, _sender, _path, _interface, name, variant) -> bool:
        value = variant.unpack()
        if name == "Rate":
            return value == 1.0  # Only normal-speed playback is supported.
        if name != "Volume" or self._service.player is None or not isfinite(value):
            return False
        self._service.set_volume(max(0.0, min(value, 1.0)))
        self.refresh()
        return True

    def _on_method(self, _connection, _sender, _path, interface, method, parameters, invocation) -> None:
        service = self._service
        try:
            if interface == ROOT and method == "Raise":
                self._raise_window()
            elif method == "Next" and service.can_go_next():
                service.user_next(autoplay=service.playback_status() == "Playing")
            elif method in self._actions and service.player is not None:
                self._actions[method]()
            elif method in {"Seek", "SetPosition"}:
                self._seek(method, parameters.unpack())
            elif method in {"Quit", "OpenUri"}:
                invocation.return_dbus_error("org.freedesktop.DBus.Error.NotSupported", f"{method} is unsupported")
                return
            self.refresh()
            invocation.return_value(GLib.Variant("()", ()))
        except (OSError, RuntimeError, ValueError) as error:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.Failed", type(error).__name__)

    def _seek(self, method: str, args: tuple) -> None:
        service = self._service
        if service.current() is None or not service.can_seek():
            return
        duration = _micros(service.duration())
        if method == "Seek":
            position = _micros(service.position()) + args[0]
            if position > duration:
                service.next_track(autoplay=service.playback_status() == "Playing")
                return
        else:
            track_id, position = args
            if track_id != _track_path(service.current()) or not 0 <= position <= duration:
                return
        service.seek(max(0, position) / MICROSECONDS)
