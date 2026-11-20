package org.radiowave.android;

import static org.junit.Assert.*;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;
import org.junit.Test;

public class WaveTest {
    private final Tracks tracks = new Tracks();
    private final Wave wave = new Wave(tracks);

    @Test public void keepsVisiblePrefixAndFillsWithFourArtistsAndTwoLanguages() {
        Tracks.Track pinned = add("Adele", "Hello", "pop", "en");
        add("Adele", "Love Song", "pop", "en");
        add("Кино", "Кукушка", "rock", "ru");
        add("Кино", "Пачка сигарет", "rock", "ru");
        add("Garbage", "Only Happy When It Rains", "alternative", "en");
        add("Алиса", "Трасса Е-95", "rock", "ru");

        List<Tracks.Track> queue = wave.fill(Collections.singletonList(pinned), Collections.emptySet(), null, 4);

        assertEquals(pinned, queue.get(0));
        assertEquals(4, queue.size());
        assertEquals(4, queue.stream().map(Tracks.Track::artist).distinct().count());
        assertEquals(2, queue.stream().filter(t -> t.effectiveLanguage().equals("ru")).count());
    }

    @Test public void sameFamilyDislikeDoesNotSuppressAnotherLanguage() {
        for (int i = 0; i < 8; i++) {
            wave.rate(add("Исполнитель " + i, "Дорога " + i, "chanson", "ru"), -1);
        }

        Tracks.Track russian = add("Музыкант", "Ночь", "chanson", "ru");
        Tracks.Track french = add("Édith Piaf", "La vie en rose", "chanson", "");
        Tracks.Track russianRock = add("Алиса", "Небо", "rock", "ru");
        List<Tracks.Track> queue = wave.fill(Collections.emptyList(), Collections.emptySet(), null, 2);

        assertTrue(queue.contains(french));
        assertTrue(queue.contains(russianRock));
        assertFalse(queue.contains(russian));
    }

    @Test public void weakSkipDoesNotChangeLikeAndHistorySurvivesNewWave() {
        Tracks.Track track = add("Adele", "Hello", "pop", "en");
        wave.rate(track, 1);
        wave.earlySkip(track);
        wave.played(track);

        Wave restarted = new Wave(tracks);
        assertEquals(1, restarted.rating(track));
        assertEquals(Collections.singletonList(track), restarted.recent());
        assertFalse(restarted.rate(track, -1));
        assertEquals(-1, restarted.rating(track));
    }

    @Test public void confirmedTagsPreserveFeedbackAndShareItWithAlternateCopies() {
        Tracks.Track guessed = add("Search Release", "File Name", "", "");
        wave.rate(guessed, 1);
        wave.earlySkip(guessed);
        Tracks.Track tagged = new Tracks.Track(guessed.id(), "Кино", "Кукушка", "rock", "ru",
                guessed.uri(), guessed.torrentKey(), guessed.fileIndex(), false);
        tracks.update(tagged);

        Tracks.Track copy = add("Кино", "Кукушка (Remastered 2020)", "rock", "ru");
        Wave restarted = new Wave(tracks);
        assertEquals(1, restarted.rating(tagged));
        assertEquals(1, restarted.rating(copy));
        restarted.rate(copy, 0);
        assertEquals(0, restarted.rating(tagged));
    }

    @Test public void knownForeignSkipsMakeRussianDiscoveryLikelyWithoutForbiddingForeign() {
        for (int i = 0; i < 16; i++) {
            wave.earlySkip(add("Artist " + i, "My Heart " + i, "rock", "en"));
        }
        assertFalse(wave.preferForeign());

        Tracks.Track available = add("Yann Tiersen", "Toujours avec toi", "instrumental", "fr");
        List<String> unavailable = new ArrayList<>();
        for (Tracks.Track track : tracks.all()) if (!track.equals(available)) unavailable.add(track.id());
        assertEquals(Collections.singletonList(available), wave.fill(Collections.emptyList(), unavailable, null, 5));
    }

    @Test public void prefersStudioAndAvoidsAlternateCopiesInSameWindow() {
        Tracks.Track live = add("Artist", "Hello (Live at Wembley)", "rock", "en");
        Tracks.Track studio = add("Artist", "Hello", "rock", "en");
        Tracks.Track another = add("Another Artist", "World", "pop", "en");

        List<Tracks.Track> queue = wave.fill(Collections.emptyList(), Collections.emptySet(), null, 4);
        assertEquals(Arrays.asList(studio, another), queue);
        assertTrue(live.live());
        wave.played(studio);
        assertEquals(Collections.singletonList(another),
                wave.fill(Collections.emptyList(), Collections.emptySet(), null, 1));
    }

    @Test public void diversifiesGenresWhenTasteIsEmpty() {
        add("A", "Song A", "rock", "en");
        add("B", "Song B", "rock", "en");
        add("C", "Song C", "rock", "en");
        add("D", "Song D", "pop", "en");
        add("E", "Song E", "dance", "en");
        add("F", "Song F", "jazz", "en");
        List<Tracks.Track> queue = wave.fill(Collections.emptyList(), Collections.emptySet(), null, 4);
        assertEquals(4, queue.stream().map(Tracks.Track::genre).distinct().count());
    }

    @Test public void titleLanguageAndSearchHistoryUseExplicitTagsAndLatestSpelling() {
        assertEquals("fr", add("a", "La vie en rose", "", "").effectiveLanguage());
        assertEquals("ru", add("a", "Мёртвая Алёна", "", "").effectiveLanguage());
        assertEquals("uk", add("a", "Пісня", "", "").effectiveLanguage());
        assertEquals("unknown", add("a", "X7", "", "").effectiveLanguage());
        assertEquals("de", add("a", "La vie en rose", "", "deu").effectiveLanguage());

        tracks.rememberSearch("Nagart");
        tracks.rememberSearch("  Кино  ");
        tracks.rememberSearch("nagart");
        assertEquals(Arrays.asList("nagart", "Кино"), tracks.searchHistory());
    }

    private Tracks.Track add(String artist, String title, String genre, String language) {
        Tracks.Track track = new Tracks.Track(Integer.toString(tracks.all().size()), artist, title, genre,
                language, "file:///test.wav", "", -1, false);
        tracks.addAll(Collections.singletonList(track));
        return track;
    }
}
