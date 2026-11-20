package org.radiowave.android;

import org.radiowave.android.Tracks.Track;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collection;
import java.util.Collections;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

public final class Wave {
    private static final String RATINGS = "wave_ratings";
    private static final String EARLY_SKIPS = "wave_early_skips";
    private static final String HISTORY = "wave_history";
    private static final int REPEAT_COOLDOWN = 20;
    private static final int MAX_HISTORY = 200;
    private static final int MAX_SKIPS_PER_RECORDING = 4;
    private static final List<String> DISCOVERY_GENRES = Arrays.asList(
            "rock", "pop", "electronic", "hip hop", "jazz", "metal", "folk",
            "soul", "classical", "country", "chanson", "blues", "punk", "reggae");

    private final Tracks tracks;
    private final Map<String, Integer> ratings;
    private final Map<String, Integer> earlySkips;
    private final List<String> history;

    public Wave(Tracks tracks) {
        this.tracks = tracks;
        ratings = readVotes(RATINGS, -1, 1);
        earlySkips = readVotes(EARLY_SKIPS, 1, MAX_SKIPS_PER_RECORDING);
        history = Tracks.decodeStrings(tracks.read(HISTORY, ""));
    }

    public synchronized int rating(Track track) {
        Integer direct = ratings.get(track.id());
        return direct == null ? feedback(tracks.all()).rating(track) : direct;
    }

    public synchronized boolean rate(Track track, int value) {
        String key = track.recordingKey();
        int vote = Integer.signum(value);
        ratings.remove(track.id());
        for (Track alias : tracks.all()) {
            if (alias.recordingKey().equals(key)) ratings.remove(alias.id());
        }
        if (vote != 0) ratings.put(track.id(), vote);
        storeVotes(RATINGS, ratings);
        return vote > 0;
    }

    public synchronized void earlySkip(Track track) {
        int count = earlySkips.getOrDefault(track.id(), 0);
        if (count >= MAX_SKIPS_PER_RECORDING) return;
        earlySkips.put(track.id(), count + 1);
        storeVotes(EARLY_SKIPS, earlySkips);
    }

    public synchronized void played(Track track) {
        if (!history.isEmpty() && history.get(0).equals(track.id())) return;
        history.add(0, track.id());
        if (history.size() > MAX_HISTORY) history.subList(MAX_HISTORY, history.size()).clear();
        tracks.write(HISTORY, Tracks.encodeStrings(history));
    }

    public synchronized List<Track> recent() {
        List<Track> result = new ArrayList<>();
        Map<String, Track> catalogue = byId(tracks.all());
        for (String id : history) {
            Track track = catalogue.get(id);
            if (track != null) result.add(track);
        }
        return result;
    }

    public synchronized List<Track> fill(List<Track> visible, Collection<String> excluded,
                                         Track current, int limit) {
        List<Track> queue = new ArrayList<>(visible);
        if (queue.size() >= limit) return queue;
        List<Track> catalogue = tracks.all();
        Map<String, Track> byId = byId(catalogue);
        Map<String, Integer> recency = recency(byId);
        Feedback feedback = feedback(catalogue);
        Taste taste = taste(catalogue, feedback);
        Set<String> unavailable = new HashSet<>(excluded);
        Window window = new Window();
        if (current != null) window.add(current);
        for (Track track : queue) window.add(track);
        String previousGenre = queue.isEmpty() ? (current == null ? "" : genre(current.genre()))
                : genre(queue.get(queue.size() - 1).genre());

        List<Candidate> candidates = new ArrayList<>();
        for (Track track : catalogue) {
            int rating = feedback.rating(track);
            if (unavailable.contains(track.id()) || rating < 0) continue;
            String key = track.recordingKey();
            String artist = Tracks.normalized(track.artist());
            String family = genre(track.genre());
            String language = track.effectiveLanguage();
            double preference = taste.artistScore(artist) + taste.genreScore(track, family, language)
                    + 0.7 * rating - 0.42 * feedback.skips(track)
                    - (track.live() ? 1.3 : 0.0);
            candidates.add(new Candidate(track, key, artist, family, language,
                    recency.getOrDefault(key, Integer.MAX_VALUE), preference));
        }
        while (queue.size() < limit) {
            Candidate next = choose(candidates, window, previousGenre, taste.foreignShare());
            if (next == null) break;
            queue.add(next.track());
            window.add(next.track());
            previousGenre = next.family();
        }
        return queue;
    }

    private static Candidate choose(List<Candidate> candidates, Window window,
                                    String previousGenre, double foreignShare) {
        boolean hasFresh = false;
        boolean hasFreshArtist = false;
        boolean hasArtist = false;
        for (Candidate candidate : candidates) {
            if (window.recordings.contains(candidate.key())) continue;
            boolean newArtist = window.artistCount(candidate.artist()) == 0;
            hasArtist |= newArtist;
            if (candidate.recentPosition() < REPEAT_COOLDOWN) continue;
            hasFresh = true;
            hasFreshArtist |= newArtist;
        }

        Candidate selected = null;
        double bestScore = Double.NEGATIVE_INFINITY;
        for (Candidate candidate : candidates) {
            if (window.recordings.contains(candidate.key())) continue;
            if (hasFresh && candidate.recentPosition() < REPEAT_COOLDOWN) continue;
            if ((hasFreshArtist || (!hasFresh && hasArtist))
                    && window.artistCount(candidate.artist()) > 0) continue;
            double score = candidate.preference()
                    + window.languageScore(candidate.language(), foreignShare)
                    - 1.25 * window.artistCount(candidate.artist())
                    - 0.9 * window.genreCount(candidate.family())
                    - (!candidate.family().isEmpty() && candidate.family().equals(previousGenre) ? 1.25 : 0)
                    - 1.4 * Math.max(0, REPEAT_COOLDOWN - candidate.recentPosition()) / REPEAT_COOLDOWN;
            if (score > bestScore) {
                bestScore = score;
                selected = candidate;
            }
        }
        return selected;
    }

    public synchronized boolean preferForeign() {
        List<Track> catalogue = tracks.all();
        Feedback feedback = feedback(catalogue);
        return preferForeign(catalogue, feedback, taste(catalogue, feedback).foreignShare());
    }

    private boolean preferForeign(List<Track> catalogue, Feedback feedback, double foreignShare) {
        Map<String, Track> byId = byId(catalogue);
        double known = 0.0;
        double foreign = 0.0;
        for (int i = 0; i < history.size() && i < REPEAT_COOLDOWN; i++) {
            Track track = byId.get(history.get(i));
            if (track == null || !knownSide(track.effectiveLanguage())) continue;
            double weight = 1.0 - (double) i / (REPEAT_COOLDOWN + 1);
            known += weight;
            if (foreignLanguage(track.effectiveLanguage())) foreign += weight;
        }
        if (known == 0) {
            Set<String> russianArtists = new HashSet<>();
            Set<String> foreignArtists = new HashSet<>();
            for (Track track : catalogue) {
                String language = track.effectiveLanguage();
                if (!knownSide(language) || feedback.rating(track) < 0) continue;
                (foreignLanguage(language) ? foreignArtists : russianArtists).add(Tracks.normalized(track.artist()));
            }
            foreign = foreignArtists.size();
            known = foreign + russianArtists.size();
        }
        return (known + 1) * foreignShare - foreign > 0.5;
    }

    public synchronized List<String> discoveryGenres() {
        List<Track> catalogue = tracks.all();
        Feedback feedback = feedback(catalogue);
        Taste taste = taste(catalogue, feedback);
        boolean foreign = preferForeign(catalogue, feedback, taste.foreignShare());
        List<String> result = new ArrayList<>(DISCOVERY_GENRES);
        List<String> extraGenres = new ArrayList<>(taste.genres.keySet());
        Collections.sort(extraGenres);
        for (String family : extraGenres) if (!result.contains(family)) result.add(family);
        result.sort(Comparator.comparingDouble((String family) -> taste.discoveryScore(family, foreign)).reversed());
        return result;
    }

    private static Taste taste(List<Track> catalogue, Feedback feedback) {
        Taste taste = new Taste();
        Set<String> seen = new HashSet<>();
        for (Track track : catalogue) {
            String identity = track.recordingKey();
            if (!seen.add(identity)) continue;
            int vote = feedback.rating(track);
            int skips = feedback.skips(track);
            if (vote == 0 && skips == 0) continue;
            double positive = Math.max(0, vote);
            double negative = Math.max(0, -vote) + 0.5 * skips;
            String artist = Tracks.normalized(track.artist());
            String family = genre(track.genre());
            String language = track.effectiveLanguage();
            double confidence = track.language().equals("unknown") ? 0.6 : 1.0;
            if (!artist.isEmpty()) taste.artists.computeIfAbsent(artist, key -> new Votes()).add(positive, negative);
            if (!family.isEmpty()) {
                taste.genres.computeIfAbsent(family, key -> new HashMap<>())
                        .computeIfAbsent(language, key -> new Votes()).add(positive * confidence, negative * confidence);
            }
            if (knownSide(language)) {
                Votes side = foreignLanguage(language) ? taste.foreign : taste.russian;
                side.add(positive * confidence, negative * confidence);
            }
        }
        return taste;
    }

    private Feedback feedback(List<Track> catalogue) {
        Map<String, Integer> recordingRatings = new HashMap<>();
        Map<String, Integer> recordingSkips = new HashMap<>();
        for (Track track : catalogue) {
            Integer rating = ratings.get(track.id());
            Integer skips = earlySkips.get(track.id());
            if (rating == null && skips == null) continue;
            String key = track.recordingKey();
            if (rating != null) recordingRatings.put(key, rating);
            if (skips != null) recordingSkips.merge(key, skips, Integer::sum);
        }
        return new Feedback(recordingRatings, recordingSkips);
    }

    private record Feedback(Map<String, Integer> recordingRatings, Map<String, Integer> recordingSkips) {
        int rating(Track track) { return recordingRatings.getOrDefault(track.recordingKey(), 0); }

        int skips(Track track) {
            return Math.min(MAX_SKIPS_PER_RECORDING, recordingSkips.getOrDefault(track.recordingKey(), 0));
        }
    }

    private Map<String, Integer> recency(Map<String, Track> catalogue) {
        Map<String, Integer> result = new HashMap<>();
        for (int i = 0; i < history.size(); i++) {
            Track track = catalogue.get(history.get(i));
            if (track != null) result.putIfAbsent(track.recordingKey(), i);
        }
        return result;
    }

    private Map<String, Integer> readVotes(String storageKey, int minimum, int maximum) {
        Map<String, Integer> result = new HashMap<>();
        List<String> pairs = Tracks.decodeStrings(tracks.read(storageKey, ""));
        for (int i = 0; i + 1 < pairs.size(); i += 2) {
            try {
                int vote = Integer.parseInt(pairs.get(i + 1));
                if (vote >= minimum && vote <= maximum && vote != 0) result.put(pairs.get(i), vote);
            } catch (NumberFormatException ignored) {
                // Ignore individual malformed entries.
            }
        }
        return result;
    }

    private void storeVotes(String storageKey, Map<String, Integer> votes) {
        List<String> pairs = new ArrayList<>(2 * votes.size());
        votes.forEach((key, value) -> {
            pairs.add(key);
            pairs.add(value.toString());
        });
        tracks.write(storageKey, Tracks.encodeStrings(pairs));
    }

    private static Map<String, Track> byId(List<Track> catalogue) {
        Map<String, Track> result = new LinkedHashMap<>();
        for (Track track : catalogue) result.put(track.id(), track);
        return result;
    }

    private static boolean knownSide(String language) {
        return !language.isEmpty() && !language.equals("unknown");
    }

    private static boolean foreignLanguage(String language) {
        return knownSide(language) && !language.equals("ru");
    }

    private static String genre(String value) {
        String label = Tracks.normalized(value);
        if (label.isEmpty() || label.equals("unknown") || label.matches("\\d+")) return "";
        if (label.contains("chanson") || label.contains("шансон")) return "chanson";
        if (label.contains("metal") || label.contains("метал")) return "metal";
        if (label.contains("punk") || label.contains("панк")) return "punk";
        if (label.contains("rock") || label.contains("рок") || label.contains("alternative") || label.contains("indie")) return "rock";
        if (label.contains("hip hop") || label.contains("рап") || label.contains("рэп") || label.equals("rap")) return "hip hop";
        if (label.contains("pop") || label.contains("поп") || label.contains("эстрада")) return "pop";
        if (label.contains("electro") || label.contains("dance") || label.contains("house") ||
                label.contains("techno") || label.contains("электрон") || label.contains("диско")) return "electronic";
        if (label.contains("jazz") || label.contains("джаз")) return "jazz";
        if (label.contains("folk") || label.contains("фолк")) return "folk";
        if (label.contains("classical") || label.contains("классич")) return "classical";
        return label;
    }

    private record Candidate(Track track, String key, String artist, String family,
                             String language, int recentPosition, double preference) {}

    private static final class Window {
        private final Set<String> recordings = new HashSet<>();
        private final Map<String, Integer> artists = new HashMap<>();
        private final Map<String, Integer> families = new HashMap<>();
        private int knownLanguages;
        private int foreignLanguages;

        void add(Track track) {
            recordings.add(track.recordingKey());
            String artist = Tracks.normalized(track.artist());
            String family = genre(track.genre());
            if (!artist.isEmpty()) artists.merge(artist, 1, Integer::sum);
            if (!family.isEmpty()) families.merge(family, 1, Integer::sum);
            String language = track.effectiveLanguage();
            if (knownSide(language)) knownLanguages++;
            if (foreignLanguage(language)) foreignLanguages++;
        }

        int artistCount(String artist) { return artists.getOrDefault(artist, 0); }

        int genreCount(String family) { return families.getOrDefault(family, 0); }

        double languageScore(String language, double foreignShare) {
            if (!knownSide(language)) return -0.15;
            double deficit = (knownLanguages + 1) * foreignShare - foreignLanguages - 0.5;
            double impact = 2.8 * Math.max(-1, Math.min(1, deficit));
            return foreignLanguage(language) ? impact : -impact;
        }
    }

    private static final class Votes {
        private double positive;
        private double negative;

        void add(double liked, double disliked) {
            positive += liked;
            negative += disliked;
        }

        double fit() { return (positive - negative) / (3 + positive + negative); }
    }

    private static final class Taste {
        private final Votes russian = new Votes();
        private final Votes foreign = new Votes();
        private final Map<String, Votes> artists = new HashMap<>();
        private final Map<String, Map<String, Votes>> genres = new HashMap<>();

        double artistScore(String artist) {
            Votes votes = artists.get(artist);
            return votes == null ? 0 : 1.9 * votes.fit();
        }

        double genreScore(Track track, String family, String language) {
            Map<String, Votes> localized = genres.get(family);
            if (localized == null) return 0;
            Votes votes = localized.get(language);
            double confidence = track.language().equals("unknown") ? 0.6 : 1.0;
            double localizedScore = votes == null ? 0.0 : confidence * votes.fit();
            Votes unscoped = localized.get("unknown");
            if (!knownSide(language) || unscoped == null) return 2.8 * localizedScore;
            return 2.8 * localizedScore + 0.4 * unscoped.fit();
        }

        double discoveryScore(String family, boolean preferForeign) {
            Map<String, Votes> localized = genres.get(family);
            if (localized == null) return 0;
            Votes global = localized.get("unknown");
            double score = global == null ? 0 : 0.2 * global.fit();
            if (!preferForeign) {
                Votes russianVotes = localized.get("ru");
                return score + (russianVotes == null ? 0 : russianVotes.fit());
            }
            double positive = 0;
            double negative = 0;
            for (Map.Entry<String, Votes> entry : localized.entrySet()) {
                if (!foreignLanguage(entry.getKey())) continue;
                positive = Math.max(positive, entry.getValue().positive);
                negative += entry.getValue().negative;
            }
            return score + (positive - 0.25 * negative) / (3 + positive + negative);
        }

        double foreignShare() {
            double foreignWeight = (4 + foreign.positive) / (4 + foreign.negative);
            double russianWeight = (4 + russian.positive) / (4 + russian.negative);
            return Math.max(0.10, Math.min(0.90, foreignWeight / (russianWeight + foreignWeight)));
        }
    }
}
