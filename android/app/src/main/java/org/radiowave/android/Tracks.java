package org.radiowave.android;

import android.content.Context;
import android.content.SharedPreferences;
import android.media.MediaExtractor;
import android.media.MediaFormat;
import android.media.MediaMetadataRetriever;
import android.net.Uri;
import android.provider.OpenableColumns;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.text.Normalizer;
import java.util.ArrayList;
import java.util.Base64;
import java.util.Collection;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.regex.Pattern;

public final class Tracks {
    private static final Object CATALOGUE_LOCK = new Object();
    private static final String CATALOGUE = "catalogue";
    private static final String SEARCH_HISTORY = "search_history";
    private static final String UNKNOWN = "unknown";
    private static final Pattern TAG_SUFFIX = Pattern.compile(
            "(?iu)\\s*[\\[(](?:(?:re)?master(?:ed)?(?: \\d{4})?|radio edit|(?:official )?audio|flac|mp3)[\\])]$");
    private static final Pattern LIVE_SUFFIX = Pattern.compile(
            "(?iu)\\s*(?:[\\[(]\\s*(?:live(?:\\s+[^()\\[\\]]+)?|концерт[^()\\[\\]]*|вживую)[\\])]"
                    + "|\\s+[-–—]\\s+(?:live(?:\\s+.*)?|концерт.*|вживую))$");
    private static final Pattern RUSSIAN_WORD = wordPattern(
            "любов[ьи]|свет|сердце|ноч[ьи]|зв[её]зды?|осень|песн[яи]|снег|дождь|ветер|солнце|дорога|время|"
                    + "тишина|мечта|небо|земля|кровь|жду|снова|ещ[её]|лето|зима|весна|счастье|кукушка|свободен|"
                    + "свобода|мы|ты|моя|тебя|это|всегда|никогда|город|м[её]ртвая");
    private static final Pattern ENGLISH_WORD = wordPattern(
            "all|again|always|alone|away|believe|goodbye|forever|heart|hello|home|life|love|night|"
                    + "rain|roads|song|stars|the|time|tonight|world|with|you|your|my|our|yesterday");
    private static final Pattern FRENCH_PHRASE = wordPattern(
            "la vie en rose|ne me quitte pas|je t'aime|l'amour|chanson française|toujours avec toi|"
                    + "comme d'habitude|rien de rien|chanson|amour|bonjour|toujours");
    private static final Pattern SPANISH_WORD = wordPattern(
            "b[eé]same|coraz[oó]n|gracias|noche|mucho|mañana|sueños|canci[oó]n");
    private static final Pattern GERMAN_WORD = wordPattern(
            "träume|liebe|schön|schöne|heimweh|nacht|immer");
    private static final Pattern PORTUGUESE_WORD = wordPattern(
            "saudade|coração|porquê");

    public record Track(String id, String artist, String title, String genre, String language,
                        String uri, String torrentKey, int fileIndex, boolean live) {
        public Track {
            id = clean(id);
            artist = clean(artist);
            title = clean(title);
            genre = clean(genre);
            language = languageCode(language);
            uri = clean(uri);
            torrentKey = clean(torrentKey);
            live = live || LIVE_SUFFIX.matcher(TAG_SUFFIX.matcher(title).replaceFirst("")).find();
        }

        public boolean isLocal() { return torrentKey.isEmpty(); }

        public String effectiveLanguage() { return UNKNOWN.equals(language) ? titleLanguage(title) : language; }

        String recordingKey() {
            String base = TAG_SUFFIX.matcher(title).replaceFirst("");
            base = LIVE_SUFFIX.matcher(base).replaceFirst("");
            String artistKey = normalized(artist);
            String titleKey = normalized(base);
            return artistKey.isEmpty() || titleKey.isEmpty() ? id : artistKey + "\u001f" + titleKey;
        }
    }

    private final Context context;
    private final SharedPreferences storage;
    private final Map<String, Track> entries = new LinkedHashMap<>();
    private final Map<String, String> values = new LinkedHashMap<>();
    private String loadedCatalogue;

    public Tracks(Context context) {
        this.context = context.getApplicationContext();
        storage = this.context.getSharedPreferences("radio_wave", Context.MODE_PRIVATE);
    }

    // Uses the same catalogue and preference semantics without Android in JVM tests.
    Tracks() {
        context = null;
        storage = null;
    }

    public List<Track> all() {
        synchronized (CATALOGUE_LOCK) {
            refreshCatalogue();
            return new ArrayList<>(entries.values());
        }
    }

    public Track get(String id) {
        synchronized (CATALOGUE_LOCK) {
            refreshCatalogue();
            return entries.get(id);
        }
    }

    public void addAll(Collection<Track> tracks) { saveTracks(tracks, false); }

    public void update(Track track) { saveTracks(Collections.singletonList(track), true); }

    private void saveTracks(Collection<Track> tracks, boolean replaceExisting) {
        synchronized (CATALOGUE_LOCK) {
            refreshCatalogue();
            boolean changed = false;
            for (Track track : tracks) {
                if (track.id().isEmpty() || track.title().isEmpty()) continue;
                Track previous = entries.get(track.id());
                if (previous == null || (replaceExisting && !previous.equals(track))) {
                    entries.put(track.id(), track);
                    changed = true;
                }
            }
            if (changed && storage != null) {
                JSONArray catalogue = new JSONArray();
                for (Track track : entries.values()) catalogue.put(toJson(track));
                loadedCatalogue = catalogue.toString();
                write(CATALOGUE, loadedCatalogue);
            }
        }
    }

    public Track addLocal(Uri uri) {
        try {
            context.getContentResolver().takePersistableUriPermission(uri,
                    android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION);
        } catch (SecurityException ignored) {
            // Providers may have granted access through another mechanism.
        }
        String title = localTitle(uri);
        String artist = "";
        int separator = title.indexOf(" - ");
        if (separator > 0 && separator + 3 < title.length()) {
            artist = title.substring(0, separator).trim();
            title = title.substring(separator + 3).trim();
        }
        return enrich(new Track("local:" + uri, artist, title, "", UNKNOWN,
                uri.toString(), "", -1, false), uri);
    }

    public Track enrich(Track track, Uri verifiedAudio) {
        AudioTags tags = readAudioTags(verifiedAudio);
        String language = languageCode(embeddedLanguage(verifiedAudio));
        Track enriched = new Track(track.id(), tagOr(tags.artist(), track.artist()),
                tagOr(tags.title(), track.title()), tagOr(tags.genre(), track.genre()),
                UNKNOWN.equals(language) ? track.language() : language, track.uri(),
                track.torrentKey(), track.fileIndex(), track.live());
        update(enriched);
        return enriched;
    }

    private AudioTags readAudioTags(Uri uri) {
        MediaMetadataRetriever metadata = new MediaMetadataRetriever();
        try {
            metadata.setDataSource(context, uri);
            return new AudioTags(clean(metadata.extractMetadata(MediaMetadataRetriever.METADATA_KEY_TITLE)),
                    clean(metadata.extractMetadata(MediaMetadataRetriever.METADATA_KEY_ARTIST)),
                    clean(metadata.extractMetadata(MediaMetadataRetriever.METADATA_KEY_GENRE)));
        } catch (RuntimeException ignored) {
            return new AudioTags("", "", "");
        } finally {
            try {
                metadata.release();
            } catch (IOException | RuntimeException ignored) {
                // Release failures do not invalidate metadata already read.
            }
        }
    }

    private record AudioTags(String title, String artist, String genre) {}

    private static String tagOr(String tag, String fallback) { return tag.isEmpty() ? fallback : tag; }

    private String localTitle(Uri uri) {
        String name = uri.getLastPathSegment();
        try (android.database.Cursor cursor = context.getContentResolver().query(uri,
                new String[]{OpenableColumns.DISPLAY_NAME}, null, null, null)) {
            if (cursor != null && cursor.moveToFirst()) name = cursor.getString(0);
        } catch (RuntimeException ignored) {
            // A file URI or a basic provider may only supply a path.
        }
        String title = clean(name).replaceFirst("(?i)\\.[a-z0-9]{2,5}$", "");
        return title.isEmpty() ? "Аудиозапись" : title;
    }

    private String embeddedLanguage(Uri uri) {
        MediaExtractor extractor = new MediaExtractor();
        try {
            extractor.setDataSource(context, uri, null);
            for (int index = 0; index < extractor.getTrackCount(); index++) {
                MediaFormat format = extractor.getTrackFormat(index);
                String mime = format.getString(MediaFormat.KEY_MIME);
                if (mime == null || !mime.startsWith("audio/")) continue;
                return format.getString(MediaFormat.KEY_LANGUAGE);
            }
        } catch (IOException | RuntimeException ignored) {
            // Fall back to the song title when the container has no usable language.
        } finally {
            extractor.release();
        }
        return UNKNOWN;
    }

    public List<String> searchHistory() {
        synchronized (CATALOGUE_LOCK) {
            return readSearchHistory();
        }
    }

    public void rememberSearch(String query) {
        String term = clean(query);
        if (term.isEmpty()) return;
        if (term.length() > 240) term = term.substring(0, 240);
        synchronized (CATALOGUE_LOCK) {
            List<String> searches = readSearchHistory();
            String key = term.toLowerCase(Locale.ROOT);
            searches.removeIf(previous -> previous.toLowerCase(Locale.ROOT).equals(key));
            searches.add(0, term);
            if (searches.size() > 20) searches.subList(20, searches.size()).clear();
            write(SEARCH_HISTORY, encodeStrings(searches));
        }
    }

    private List<String> readSearchHistory() {
        return decodeStrings(read(SEARCH_HISTORY, ""));
    }

    static String encodeStrings(Collection<String> values) {
        Base64.Encoder encoder = Base64.getUrlEncoder();
        List<String> tokens = new ArrayList<>();
        for (String value : values) {
            tokens.add(encoder.encodeToString(value.getBytes(StandardCharsets.UTF_8)));
        }
        return String.join(",", tokens);
    }

    static List<String> decodeStrings(String content) {
        Base64.Decoder decoder = Base64.getUrlDecoder();
        List<String> result = new ArrayList<>();
        for (String token : content.split(",")) {
            if (token.isEmpty()) continue;
            try {
                result.add(new String(decoder.decode(token), StandardCharsets.UTF_8));
            } catch (IllegalArgumentException ignored) {
                // Skip an invalid stored field independently of the rest of the list.
            }
        }
        return result;
    }

    String read(String key, String fallback) {
        synchronized (CATALOGUE_LOCK) {
            if (storage != null) return storage.getString(key, fallback);
            return values.getOrDefault(key, fallback);
        }
    }

    void write(String key, String value) {
        synchronized (CATALOGUE_LOCK) {
            if (storage != null) storage.edit().putString(key, value).apply();
            else values.put(key, value);
        }
    }

    private void refreshCatalogue() {
        if (storage == null) return;
        String stored = read(CATALOGUE, "");
        if (stored.equals(loadedCatalogue)) return;
        loadedCatalogue = stored;
        entries.clear();
        if (stored.isEmpty()) return;
        try {
            JSONArray items = new JSONArray(stored);
            for (int i = 0; i < items.length(); i++) {
                JSONObject item = items.optJSONObject(i);
                if (item == null) continue;
                Track track = fromJson(item);
                if (!track.id().isEmpty() && !track.title().isEmpty()) entries.put(track.id(), track);
            }
        } catch (JSONException ignored) {
            // A corrupt catalogue can be replaced by a subsequent import.
        }
    }

    private static JSONObject toJson(Track track) {
        try {
            return new JSONObject().put("id", track.id()).put("artist", track.artist()).put("title", track.title())
                    .put("genre", track.genre()).put("language", track.language()).put("uri", track.uri())
                    .put("torrentKey", track.torrentKey()).put("fileIndex", track.fileIndex()).put("live", track.live());
        } catch (JSONException failure) {
            throw new IllegalStateException("Cannot serialize track", failure);
        }
    }

    private static Track fromJson(JSONObject item) {
        return new Track(item.optString("id"), item.optString("artist"), item.optString("title"),
                item.optString("genre"), item.optString("language"), item.optString("uri"), item.optString("torrentKey"),
                item.optInt("fileIndex", -1), item.optBoolean("live"));
    }

    private static String languageCode(String value) {
        String label = clean(value).toLowerCase(Locale.ROOT);
        switch (label) {
            case "русский": case "russian": case "rus": return "ru";
            case "английский": case "english": case "eng": return "en";
            case "французский": case "french": case "fra": case "fre": return "fr";
            case "немецкий": case "german": case "deu": case "ger": return "de";
            case "испанский": case "spanish": case "spa": return "es";
            case "португальский": case "portuguese": case "por": return "pt";
            case "украинский": case "ukrainian": case "ukr": return "uk";
            case "белорусский": case "belarusian": case "bel": return "be";
            case "итальянский": case "italian": case "ita": return "it";
            default:
                String code = Locale.forLanguageTag(label.replace('_', '-')).getLanguage();
                if ("und".equals(code) || "zxx".equals(code) || "mul".equals(code)) return UNKNOWN;
                if (code.length() == 2 || code.length() == 3) return code;
                return "other".equals(label) ? label : UNKNOWN;
        }
    }

    private static Pattern wordPattern(String words) {
        return Pattern.compile("(?<![\\p{L}\\p{N}_])(?:" + words + ")(?![\\p{L}\\p{N}_])");
    }

    private static String titleLanguage(String value) {
        String title = TAG_SUFFIX.matcher(LIVE_SUFFIX.matcher(clean(value)).replaceFirst("")).replaceFirst("");
        title = Normalizer.normalize(title, Normalizer.Form.NFKC).toLowerCase(Locale.ROOT).replace('’', '\'');
        boolean cyrillic = false;
        boolean latin = false;
        boolean other = false;
        for (int offset = 0; offset < title.length();) {
            int codePoint = title.codePointAt(offset);
            offset += Character.charCount(codePoint);
            if (!Character.isLetter(codePoint)) continue;
            Character.UnicodeScript script = Character.UnicodeScript.of(codePoint);
            if (script == Character.UnicodeScript.CYRILLIC) cyrillic = true;
            else if (script == Character.UnicodeScript.LATIN) latin = true;
            else other = true;
        }
        if (cyrillic && !latin && !other) {
            if (title.matches(".*[іїєґ].*")) return "uk";
            if (title.indexOf('ў') >= 0) return "be";
            if (title.matches(".*[јђљњћџѓќѕәғқңөұүһ].*")) return "other";
            return title.indexOf('ё') >= 0 || RUSSIAN_WORD.matcher(title).find() ? "ru" : UNKNOWN;
        }
        if (latin && !cyrillic && !other) {
            if (FRENCH_PHRASE.matcher(title).find()) return "fr";
            if (SPANISH_WORD.matcher(title).find()) return "es";
            if (GERMAN_WORD.matcher(title).find()) return "de";
            if (PORTUGUESE_WORD.matcher(title).find()) return "pt";
            if (ENGLISH_WORD.matcher(title).find()) return "en";
        }
        return other && !latin && !cyrillic ? "other" : UNKNOWN;
    }

    static String normalized(String value) {
        return Normalizer.normalize(clean(value), Normalizer.Form.NFKC).toLowerCase(Locale.ROOT)
                .replace('ё', 'е').replaceAll("[^\\p{L}\\p{N}]+", " ").trim();
    }

    private static String clean(String value) { return value == null ? "" : value.trim(); }
}
