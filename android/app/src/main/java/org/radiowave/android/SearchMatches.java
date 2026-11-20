package org.radiowave.android;

import org.radiowave.android.Tracks.Track;
import java.util.ArrayList;
import java.util.Collection;
import java.util.Comparator;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

final class SearchMatches {
    private SearchMatches() {}

    static List<Track> inCatalogue(String query, Collection<Track> tracks) {
        List<String> terms = PublicIndex.words(query);
        List<Track> result = new ArrayList<>();
        if (terms.isEmpty()) return result;
        for (Track track : tracks) {
            List<String> tokens = PublicIndex.words(track.artist() + " " + track.title());
            if (new HashSet<>(tokens).containsAll(terms)) result.add(track);
        }
        result.sort(Comparator.comparing(Track::live).thenComparing(track -> !track.isLocal()));
        return result;
    }

    static List<Track> merge(Collection<Track> local, Collection<Track> remote) {
        Map<String, Track> choices = new LinkedHashMap<>();
        for (Track track : local) save(choices, track);
        for (Track track : remote) save(choices, track);
        List<Track> ordered = new ArrayList<>(choices.values());
        ordered.sort(Comparator.comparing(Track::live));
        return ordered;
    }

    private static void save(Map<String, Track> choices, Track track) {
        choices.putIfAbsent(track.recordingKey() + (track.live() ? "/live" : "/studio"), track);
    }
}
