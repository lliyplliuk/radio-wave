package org.radiowave.android;

import android.os.Bundle;

import androidx.media3.session.SessionCommand;

import java.util.ArrayList;
import java.util.List;

final class PhoneCommands {
    static final SessionCommand STATE = command("state");
    static final SessionCommand UPDATE = command("update");
    static final SessionCommand COLLECT = command("collection");
    static final SessionCommand SEARCH = command("search");
    static final SessionCommand HISTORY = command("history");
    static final SessionCommand PLAY_TRACK = command("play_track");
    static final SessionCommand RATE = command("rate");
    static final SessionCommand LOCAL = command("local");
    static final SessionCommand MAGNET = command("magnet");
    static final SessionCommand TORRENT = command("torrent");
    static final List<SessionCommand> CLIENT_COMMANDS = List.of(
            STATE, COLLECT, SEARCH, HISTORY, PLAY_TRACK, RATE, LOCAL, MAGNET, TORRENT);

    private PhoneCommands() {}

    private static SessionCommand command(String name) {
        return new SessionCommand("org.radiowave.android." + name, Bundle.EMPTY);
    }

    static Bundle text(String value) {
        Bundle data = new Bundle();
        data.putString("value", value);
        return data;
    }

    static Bundle track(Tracks.Track track) {
        Bundle data = new Bundle();
        if (track != null) {
            data.putString("id", track.id());
            data.putString("title", track.title());
            data.putString("artist", track.artist());
            data.putString("genre", track.genre());
            data.putString("language", track.effectiveLanguage());
            data.putBoolean("live", track.live());
        }
        return data;
    }

    static Bundle rows(List<Tracks.Track> tracks) {
        Bundle data = new Bundle();
        ArrayList<Bundle> rows = new ArrayList<>();
        tracks.forEach(track -> rows.add(track(track)));
        data.putParcelableArrayList("rows", rows);
        return data;
    }

    @SuppressWarnings("deprecation")
    static List<Bundle> rows(Bundle data) {
        ArrayList<Bundle> result = data.getParcelableArrayList("rows");
        return result == null ? List.of() : result;
    }
}
