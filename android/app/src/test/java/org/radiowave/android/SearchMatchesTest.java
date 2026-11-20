package org.radiowave.android;

import org.junit.Test;
import org.radiowave.android.Tracks.Track;
import java.util.List;
import static org.junit.Assert.assertEquals;

public class SearchMatchesTest {
    @Test
    public void matchesArtistAndTitleAndMergesRemoteWithoutHidingStudio() {
        Track local = new Track("local", "НаГарт", "Пугало", "", "ru", "file:///sample.wav", "", -1, false);
        Track studioCopy = new Track("copy", "Нагарт", "Пугало", "", "ru", "", "hash", 0, false);
        Track live = new Track("live", "Нагарт", "Пугало (live)", "", "ru", "", "hash", 1, true);
        Track other = new Track("other", "Нагарт", "Второй трек", "", "ru", "", "other", 1, false);
        List<Track> existing = SearchMatches.inCatalogue("Нагарт — Пугало", List.of(local, other));
        assertEquals(List.of(local), existing);
        assertEquals(List.of(local), SearchMatches.merge(existing, List.of()));
        assertEquals(List.of(local, live), SearchMatches.merge(existing, List.of(live, studioCopy)));
        assertEquals(List.of(), SearchMatches.inCatalogue("???", List.of(local, other)));
    }
}
