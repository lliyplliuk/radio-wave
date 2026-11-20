import os
import time

import pytest

from radio_wave.search_history import SearchHistory


def test_persists_latest_25_and_deduplicates_preserving_latest_spelling(tmp_path):
    path = tmp_path / "state" / "history.json"
    history = SearchHistory(path)
    for number in range(30):
        history.remember(f"Поиск {number:02}")
    history.remember("Ёжик в тумане")
    history.remember("ежик В тумане")
    history.remember("   ")
    restarted = SearchHistory(path)
    assert restarted.matches("ЁЖИК") == ["ежик В тумане"]
    assert restarted.matches("Поиск 00") == []
    assert len(restarted._terms) == 25
    assert all(str(term).strip() for term in restarted._terms)


def test_prefix_before_substring_with_six_result_limit(tmp_path):
    history = SearchHistory(tmp_path / "history.json")
    history.remember("Лес")
    for number in range(8):
        history.remember(f"Песни про лес {number}")
    assert history.matches("лес") == [
        "Лес", "Песни про лес 7", "Песни про лес 6", "Песни про лес 5",
        "Песни про лес 4", "Песни про лес 3",
    ]
    assert not history.matches("")
    assert history.recent() == [f"Песни про лес {number}" for number in range(7, 1, -1)]


def test_corrupt_history_is_replaced_on_next_submit(tmp_path):
    path = tmp_path / "history.json"
    path.write_bytes(b"invalid\xff")
    history = SearchHistory(path)
    assert history.matches("любой") == []
    history.remember("Кино")
    assert SearchHistory(path).matches("кино") == ["Кино"]


def descendants(widget):
    yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from descendants(child)
        child = child.get_next_sibling()


@pytest.mark.skipif(
    not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")),
    reason="нет графического дисплея",
)
def test_native_search_history_selects_text_without_submitting(tmp_path, monkeypatch):
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gdk, GLib, Gtk
    from radio_wave import catalog_dialog

    if not Gtk.init_check():
        pytest.skip("GTK4 не может открыть дисплей")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    requests = []
    played = []
    monkeypatch.setattr(catalog_dialog, "search_picker_recordings", lambda *args: requests.append(args))
    history = SearchHistory()
    history.remember("Ёлка — Прованс")
    parent = Gtk.Window()
    window = catalog_dialog.show_catalog(
        parent, lambda: [], on_play=lambda *_args: None,
        on_play_recording=played.append, is_available=lambda _track: False,
    )
    try:
        entry = next(widget for widget in descendants(window) if isinstance(widget, Gtk.SearchEntry))
        popup = next(widget for widget in descendants(window) if isinstance(widget, Gtk.Popover))
        assert popup.get_visible()
        entry.set_text("ел")
        assert popup.get_visible()
        context = GLib.MainContext.default()
        end = time.monotonic() + 2
        while time.monotonic() < end:
            while context.pending():
                context.iteration(False)
            surface = popup.get_surface()
            if surface is not None and surface.get_mapped() and entry.get_width():
                break
            time.sleep(0.005)
        assert isinstance(surface, Gdk.Popup) and surface.get_mapped()
        _, field = entry.compute_bounds(window)
        status = entry.get_parent().get_next_sibling()
        _, status_bounds = status.compute_bounds(window)
        assert surface.get_position_x() >= field.get_x() - 12
        assert surface.get_position_x() + surface.get_width() <= field.get_x() + field.get_width() + 12
        assert surface.get_position_y() >= field.get_y() + field.get_height() - 12
        assert surface.get_position_y() + surface.get_height() <= status_bounds.get_y() + 12
        suggestions = popup.get_child()
        row = suggestions.get_first_child()
        assert row.get_child().get_label() == "Ёлка — Прованс"
        suggestions.emit("row-activated", row)
        assert entry.get_text() == "Ёлка — Прованс"
        assert not popup.get_visible()
        assert not requests and not played

        entry.set_text("ел")
        key_controller = next(
            controller for controller in entry.observe_controllers()
            if isinstance(controller, Gtk.EventControllerKey)
            and controller.get_propagation_phase() == Gtk.PropagationPhase.CAPTURE
        )
        assert key_controller.emit("key-pressed", Gdk.KEY_Down, 0, Gdk.ModifierType(0))
        assert key_controller.emit("key-pressed", Gdk.KEY_Return, 0, Gdk.ModifierType(0))
        assert entry.get_text() == "Ёлка — Прованс" and not popup.get_visible()
        assert not requests and not played

        entry.set_text("Пикник — ООО")
        assert SearchHistory().matches("Пикник") == []
        next(widget for widget in descendants(window)
             if isinstance(widget, Gtk.Button) and widget.get_label() == "Слушать").emit("clicked")
        assert SearchHistory().matches("Пикник") == ["Пикник — ООО"]
        assert played[0].artist == "Пикник"
        assert not requests
    finally:
        if window.get_visible():
            window.close()
        parent.destroy()


@pytest.mark.parametrize("kind", ("collection", "catalogue", "discovery"))
@pytest.mark.skipif(
    not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")),
    reason="нет графического дисплея",
)
def test_native_result_row_activates_the_exact_song(tmp_path, monkeypatch, kind):
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    from gi.repository import GLib, Gtk
    from radio_wave import catalog_dialog
    from radio_wave.catalog import CatalogRecording

    if not Gtk.init_check():
        pytest.skip("GTK4 не может открыть дисплей")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    selected = {"id": "one", "artist": "Группа", "title": "Песня", "source": "torrent"}
    named = CatalogRecording("Песня", "Группа", "named")
    played = []
    monkeypatch.setattr(catalog_dialog, "search_picker_recordings", lambda *_args: [named])
    monkeypatch.setattr(catalog_dialog, "similar_recordings", lambda *_args: [named])
    parent = Gtk.Window()
    window = catalog_dialog.show_catalog(
        parent,
        lambda: [selected] if kind == "collection" else [],
        on_play=lambda item: played.append(("track", item)),
        on_play_recording=lambda item: played.append(("recording", item)),
        is_available=lambda _item: False,
        seed_mbids=("seed",),
        discovery=kind == "discovery",
    )
    try:
        entry = next(widget for widget in descendants(window) if isinstance(widget, Gtk.SearchEntry))
        scroller = next(widget for widget in descendants(window) if isinstance(widget, Gtk.ScrolledWindow))
        results = next(widget for widget in descendants(scroller) if isinstance(widget, Gtk.ListBox))
        if kind != "discovery":
            entry.set_text("Песня")
        if kind == "catalogue":
            entry.emit("activate")

        context = GLib.MainContext.default()
        until = time.monotonic() + 2
        while results.get_row_at_index(0) is None and time.monotonic() < until:
            while context.pending():
                context.iteration(False)
            time.sleep(0.005)
        assert results.get_activate_on_single_click()
        row = results.get_row_at_index(0)
        assert row is not None and row.get_activatable() and row.get_selectable()
        results.emit("row-activated", row)
        results.emit("row-activated", row)
        assert played == ([("track", selected)] if kind == "collection" else [("recording", named)])
        assert not window.get_visible()
    finally:
        if window.get_visible():
            window.close()
        parent.destroy()


@pytest.mark.parametrize("version", ("same", "different-id", "live-path", "ambiguous", "known-label", "remote-only"))
@pytest.mark.skipif(
    not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")),
    reason="нет графического дисплея",
)
def test_search_prefers_compatible_ready_copy_before_and_after_catalogue(tmp_path, monkeypatch, version):
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    from gi.repository import GLib, Gtk
    from radio_wave import catalog_dialog
    from radio_wave.catalog import CatalogRecording
    from radio_wave.online_radio import cached_recording_matches

    if not Gtk.init_check():
        pytest.skip("GTK4 не может открыть дисплей")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    pending = {"id": "stale", "artist": "Группа", "title": "Песня", "mbid": "first", "source": "torrent"}
    ready = {
        "id": "ready", "artist": "Группа", "title": "Песня",
        "path": f"/collection/{'Live at Leeds' if version == 'live-path' else 'Studio'}/Песня.flac",
        "mbid": {"different-id": "second", "known-label": "first"}.get(version, ""),
    }
    remotes = [CatalogRecording(
        "Песня", "Группа", "first",
        disambiguation="запись для альбома" if version == "known-label" else "",
        album_titles=("Альбом",),
    )]
    library = [ready] if version == "remote-only" else [pending, ready]
    if version == "ambiguous":
        library.insert(1, {**pending, "id": "second-stale", "mbid": "second"})
    if version in ("ambiguous", "remote-only"):
        remotes.append(CatalogRecording("Песня", "Группа", "second"))
    monkeypatch.setattr(catalog_dialog, "search_picker_recordings", lambda _query: remotes)
    played = []
    parent = Gtk.Window()
    window = catalog_dialog.show_catalog(
        parent, lambda: library, on_play=played.append,
        on_play_recording=played.append, is_available=lambda track: track["id"] == "ready",
    )
    try:
        entry = next(widget for widget in descendants(window) if isinstance(widget, Gtk.SearchEntry))
        scroller = next(widget for widget in descendants(window) if isinstance(widget, Gtk.ScrolledWindow))
        results = next(widget for widget in descendants(scroller) if isinstance(widget, Gtk.ListBox))
        status = entry.get_parent().get_next_sibling()
        entry.set_text("Группа")
        ready_row_index = 0 if version in ("same", "known-label", "remote-only") else 2 if version == "ambiguous" else 1
        assert results.get_row_at_index(ready_row_index) is not None
        assert results.get_row_at_index(ready_row_index + 1) is None
        entry.emit("activate")
        context = GLib.MainContext.default()
        end = time.monotonic() + 2
        while status.get_text() and time.monotonic() < end:
            while context.pending():
                context.iteration(False)
            time.sleep(0.005)
        assert not status.get_text()
        if version == "remote-only":
            row = results.get_row_at_index(1)
            assert results.get_row_at_index(2) is not None
            row.get_child().get_last_child().emit("clicked")
            [selection] = played
            assert selection.mbid == "first"
            assert not cached_recording_matches(selection, ready)
            assert cached_recording_matches(selection, {**ready, "mbid": "first"})
            return
        assert results.get_row_at_index(ready_row_index + 1) is None
        row = results.get_row_at_index(ready_row_index)
        row.get_child().get_last_child().emit("clicked")
        assert played == [ready]
    finally:
        if window.get_visible():
            window.close()
        parent.destroy()
