"""GTK views for searching and listening to named recordings."""

import re
from dataclasses import replace
from threading import Thread

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk, Pango

from .catalog import CatalogError, CatalogRecording, normalize, search_picker_recordings, similar_recordings
from .diagnostics import trace
from .online_radio import ambiguous_recording_copies, compatible_recording_copies, recording_key
from .search_history import SearchHistory


def _name(artist, title):
    return " — ".join(part for part in (artist, title) if part)


def _recording_query(query):
    parts = re.split(r"\s*[–—]\s*|\s+-\s+", query, maxsplit=1)
    if len(parts) != 2:
        return None
    artist, title = (part.strip() for part in parts)
    return CatalogRecording(title=title, artist=artist, mbid="") if title and artist else None


def _duration(milliseconds):
    if milliseconds is None:
        return ""
    minutes, seconds = divmod(milliseconds // 1000, 60)
    return f"{minutes}:{seconds:02d}"


def show_catalog(
    parent, tracks, *, on_play, on_play_recording, is_available, seed_mbids=(),
    on_discovery=None, on_play_search=None, discovery=False,
):
    """Show songs and delegate source choice to the playback service."""
    seeds = tuple(seed_mbids)
    history = SearchHistory()
    window = Gtk.Window(
        title="Поиск и открытия", transient_for=parent, modal=True,
        default_width=670, default_height=550,
    )
    layout = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    layout.set_margin_top(18)
    layout.set_margin_bottom(18)
    layout.set_margin_start(18)
    layout.set_margin_end(18)
    window.set_child(layout)

    search_row = Gtk.Box(spacing=8)
    search_box = Gtk.SearchEntry(hexpand=True)
    search_box.set_property("placeholder-text", "Название или исполнитель")
    search_row.append(search_box)
    suggestions = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
    suggestions.set_activate_on_single_click(True)
    history_popup = Gtk.Popover(child=suggestions, autohide=False, has_arrow=False)
    history_popup.set_parent(search_box)
    history_popup.set_position(Gtk.PositionType.BOTTOM)
    find_button = Gtk.Button(label="Найти")
    search_row.append(find_button)
    discover_button = Gtk.Button(label="По лайкам", sensitive=bool(seeds))
    search_row.append(discover_button)
    layout.append(search_row)

    status = Gtk.Label(xalign=0, wrap=True, selectable=True)
    status.add_css_class("dim-label")
    layout.append(status)

    results = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
    results.set_activate_on_single_click(True)
    results.set_show_separators(True)
    scroller = Gtk.ScrolledWindow(vexpand=True, child=results)
    scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    layout.append(scroller)

    closed = False
    visible_query = None
    active = False
    wanted = None

    def close(_window):
        nonlocal closed
        closed = True
        history_popup.unparent()
        return False

    def size_history():
        if not closed and history_popup.get_visible():
            width = max(search_box.get_width(), 160)
            rectangle = Gdk.Rectangle()
            rectangle.width = width
            rectangle.height = search_box.get_height()
            history_popup.set_pointing_to(rectangle)
            history_popup.set_size_request(width, -1)
            _minimum, height, *_baselines = history_popup.measure(Gtk.Orientation.VERTICAL, width)
            status.set_margin_top(height)
            history_popup.present()
        return GLib.SOURCE_REMOVE

    def hide_history():
        history_popup.popdown()
        status.set_margin_top(0)

    def show_history(query):
        while row := suggestions.get_first_child():
            suggestions.remove(row)
        for term in history.matches(query) if query else history.recent():
            row = Gtk.ListBoxRow()
            row.search_term = term
            label = Gtk.Label(
                label=term, xalign=0, single_line_mode=True, ellipsize=Pango.EllipsizeMode.END,
                tooltip_text=term,
            )
            label.set_margin_start(10)
            label.set_margin_end(10)
            label.set_margin_top(6)
            label.set_margin_bottom(6)
            row.set_child(label)
            suggestions.append(row)
        if suggestions.get_first_child() is not None and search_box.get_mapped():
            history_popup.popup()
            size_history()
            GLib.idle_add(size_history)
        else:
            hide_history()

    def use_suggestion(_list, row):
        search_box.set_text(row.search_term)
        search_box.set_position(-1)
        search_box.grab_focus()
        hide_history()

    def suggest_keys(_controller, keyval, _keycode, _state):
        if not history_popup.get_visible():
            return False
        if keyval == Gdk.KEY_Escape:
            hide_history()
            return True
        selected = suggestions.get_selected_row()
        if keyval in (Gdk.KEY_Down, Gdk.KEY_Up):
            step = 1 if keyval == Gdk.KEY_Down else -1
            index = selected.get_index() + step if selected else 0
            row = suggestions.get_row_at_index(index)
            if row is not None:
                suggestions.select_row(row)
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and selected is not None:
            use_suggestion(suggestions, selected)
            return True
        return False

    def clear():
        while child := results.get_first_child():
            results.remove(child)

    def add_row(title, detail, callback, item):
        row = Gtk.ListBoxRow(activatable=callback is not None, selectable=callback is not None)
        content = Gtk.Box(spacing=12)
        content.set_margin_top(7)
        content.set_margin_bottom(7)
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True)
        labels.append(Gtk.Label(label=title, xalign=0, wrap=True, selectable=callback is None))
        if detail:
            subtitle = Gtk.Label(label=detail, xalign=0, wrap=True)
            subtitle.add_css_class("dim-label")
            labels.append(subtitle)
        content.append(labels)

        listen = Gtk.Button(label="Слушать", sensitive=callback is not None, valign=Gtk.Align.CENTER)

        if callback is not None:
            row.play = lambda: callback(item)
            listen.connect("clicked", lambda _button: play_row(results, row))
        content.append(listen)
        row.set_child(content)
        results.append(row)

    def play_row(_list, row):
        if not closed and row.get_activatable():
            row.play()
            window.close()

    def add_recording(recording, play=on_play_recording):
        detail = " · ".join(filter(None, (
            recording.disambiguation, *recording.album_titles[:1], _duration(recording.duration_ms),
        )))
        add_row(_name(recording.artist, recording.title), detail, play, recording)

    def add_local(track, ready):
        artist, title = track.get("artist", ""), track.get("title", "")
        name = _name(artist, title)
        if ready or track.get("source") == "torrent":
            add_row(name, "В коллекции", on_play, track)
        elif track.get("mbid") or (artist and title):
            add_recording(CatalogRecording(title=title, artist=artist, mbid=track.get("mbid") or ""))
        else:
            add_row(name, "Файл недоступен", None, track)

    def local_matches(query, recordings=()):
        terms = set(normalize(query).split())
        library = list(tracks())
        matches = [
            track for track in library
            if all(term in normalize(_name(track.get("artist"), track.get("title"))) for term in terms)
        ]
        keys = {_key(track) for track in matches}
        if not keys:
            return [], ambiguous_recording_copies(recordings)
        candidates = [track for track in library if _key(track) in keys]
        ambiguous = ambiguous_recording_copies([*candidates, *recordings])
        ready_copies = {}
        ready_ids = set()
        for track in candidates:
            if is_available(track):
                ready_copies.setdefault(_key(track), []).append(track)
                ready_ids.add(track["id"])
        found = {}
        for track in matches:
            if track["id"] not in ready_ids:
                track = next((
                    copy for copy in ready_copies.get(_key(track), ())
                    if compatible_recording_copies(track, copy, ambiguous=ambiguous)
                ), track)
            found[track["id"]] = (track, track["id"] in ready_ids)
        return list(found.values()), ambiguous

    def show_exact(recording, query):
        def listen(value):
            history.remember(query)
            on_play_recording(value)

        clear()
        status.set_label("")
        add_recording(recording, listen)

    def show_search_results(query, recordings, error=""):
        clear()
        local, ambiguous = local_matches(query, recordings)

        def play_catalogue_match(recording):
            if recording.mbid and recording_key(recording.artist, recording.title) in ambiguous:
                recording = replace(recording, require_cached_mbid=True)
            on_play_recording(recording)

        ready = [track for track, available in local if available]
        used_recordings = set()
        for track, available in local:
            alternate = None if available else next((
                recording for recording in recordings if compatible_recording_copies(track, recording, ambiguous=ambiguous)
            ), None)
            if alternate is None:
                add_local(track, available)
            elif alternate not in used_recordings:
                add_recording(alternate, play_catalogue_match)
                used_recordings.add(alternate)
        displayed = len(local)
        for recording in recordings:
            if recording in used_recordings or any(
                compatible_recording_copies(track, recording, ambiguous=ambiguous) for track in ready
            ):
                continue
            add_recording(recording, play_catalogue_match)
            displayed += 1
        if displayed:
            status.set_label(error)
        elif on_play_search:
            status.set_label(error or "В музыкальном каталоге песни не найдены.")
            add_row(query, "Автоматический поиск песни", on_play_search, query)
        else:
            status.set_label(error or "Песни не найдены. Попробуй указать исполнителя и название.")

    def local_search(_entry=None):
        nonlocal visible_query, wanted
        query = search_box.get_text().strip()
        if query == visible_query:
            return
        visible_query = query
        wanted = None
        update_controls()
        show_history(query)
        if recording := _recording_query(query):
            show_exact(recording, query)
        else:
            clear()
            if query:
                local, _ambiguous = local_matches(query)
                for track, ready in local:
                    add_local(track, ready)
                status.set_label("Нажми Enter или «Найти» для поиска песен.")
            else:
                status.set_label("Введи название или «Исполнитель — Песня».")

    def update_controls():
        find_button.set_sensitive(wanted is None)
        discover_button.set_sensitive(bool(seeds) and wanted is None)

    def fetch(kind, query, query_function, render):
        nonlocal wanted
        key = kind, normalize(query)
        if wanted is not None and wanted[0] == key:
            return
        wanted = key, query, query_function, render
        update_controls()
        status.set_label("Ищем песни…")
        if not active:
            start_fetch()

    def start_fetch():
        nonlocal active
        key, query, query_function, _render = wanted
        active = True
        trace("catalog_request_started", kind=key[0], query=query)

        def finish(recordings, error):
            nonlocal active, wanted
            active = False
            if closed:
                return GLib.SOURCE_REMOVE
            if wanted is not None and wanted[0] == key:
                render = wanted[3]
                wanted = None
                render(recordings, error)
                trace("catalog_request_applied", kind=key[0], query=query, count=len(recordings),
                      outcome="error" if error else "found" if recordings else "empty")
            else:
                trace("catalog_request_stale", kind=key[0], query=query)
            update_controls()
            if wanted is not None:
                start_fetch()
            return GLib.SOURCE_REMOVE

        def run():
            try:
                recordings = query_function()
            except CatalogError:
                GLib.idle_add(finish, (), "Каталог песен временно недоступен.")
            else:
                GLib.idle_add(finish, recordings, "")

        Thread(target=run, daemon=True).start()

    def find(_widget):
        local_search()
        query = visible_query
        if not query:
            return
        hide_history()
        history.remember(query)
        if recording := _recording_query(query):
            show_exact(recording, query)
        else:
            fetch(
                "catalogue", query,
                lambda: search_picker_recordings(query),
                lambda recordings, error: show_search_results(query, recordings, error),
            )

    def discover(_widget):
        hide_history()
        if not seeds:
            status.set_label("Подборка появится, когда для понравившихся песен найдутся похожие.")
            return

        def render(recordings, error):
            clear()
            status.set_label(error or ("Похожие песни пока не найдены." if not recordings else ""))
            for recording in recordings:
                add_recording(recording)
            if on_discovery and not error:
                on_discovery(recordings)

        fetch("listenbrainz", "", lambda: similar_recordings(seeds), render)

    window.connect("close-request", close)
    results.connect("row-activated", play_row)
    suggestions.connect("row-activated", use_suggestion)
    suggestion_keys = Gtk.EventControllerKey()
    suggestion_keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    suggestion_keys.connect("key-pressed", suggest_keys)
    search_box.add_controller(suggestion_keys)
    history_focus = Gtk.EventControllerFocus()
    history_focus.connect("enter", lambda _controller: show_history(search_box.get_text().strip()))
    history_focus.connect("leave", lambda _controller: hide_history())
    search_box.add_controller(history_focus)
    search_box.connect("changed", local_search)
    search_box.connect("activate", find)
    find_button.connect("clicked", find)
    discover_button.connect("clicked", discover)
    window.present()
    if discovery:
        discover(discover_button)
    else:
        local_search()
        search_box.grab_focus()
    return window


def _key(track):
    return recording_key(track.get("artist") or "", track.get("title") or "")
