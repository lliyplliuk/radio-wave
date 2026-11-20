package org.radiowave.android;

import android.content.Context;
import android.net.Uri;
import android.util.Log;

import com.frostwire.jlibtorrent.AddTorrentParams;
import com.frostwire.jlibtorrent.AlertListener;
import com.frostwire.jlibtorrent.FileStorage;
import com.frostwire.jlibtorrent.Priority;
import com.frostwire.jlibtorrent.SessionHandle;
import com.frostwire.jlibtorrent.SessionManager;
import com.frostwire.jlibtorrent.TorrentFlags;
import com.frostwire.jlibtorrent.TorrentHandle;
import com.frostwire.jlibtorrent.TorrentInfo;
import com.frostwire.jlibtorrent.alerts.Alert;
import com.frostwire.jlibtorrent.alerts.AlertType;
import com.frostwire.jlibtorrent.alerts.CacheFlushedAlert;
import com.frostwire.jlibtorrent.swig.add_torrent_params;
import com.frostwire.jlibtorrent.swig.byte_vector;
import com.frostwire.jlibtorrent.swig.error_code;
import com.frostwire.jlibtorrent.swig.int_string_map;

import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.StandardCopyOption;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collection;
import java.util.Collections;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.Callable;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.function.BooleanSupplier;
import java.util.regex.Pattern;

public final class TorrentSources implements AutoCloseable {
    private static final String TAG = "RadioWaveTorrent";
    private static final int MAX_FILES = 12_000;
    private static final long MAX_AUDIO_BYTES = 128L * 1024 * 1024;
    private static final long NO_PROGRESS_NS = TimeUnit.SECONDS.toNanos(8);
    private static final long TRACK_TIMEOUT_NS = TimeUnit.SECONDS.toNanos(32);
    private static final long CACHE_GRACE_MS = TimeUnit.SECONDS.toMillis(4);
    private static final Pattern STORE_KEY = Pattern.compile("[0-9a-f]{40}|[0-9a-f]{64}");
    private static final Pattern STORE_MEDIA = Pattern.compile("(?:[0-9a-f]{40}|[0-9a-f]{64})-[0-9]{1,5}\\.[a-z0-9]{2,4}");
    private static final Pattern AUDIO = Pattern.compile("(?i)\\.(?:mp3|flac|wav|ogg|opus|m4a|aac|m4b|alac)$");
    private static final Pattern SPLIT = Pattern.compile("\\s+[-–—]\\s+");
    private static final Pattern NUMBER = Pattern.compile("^\\s*\\d{1,3}(?:\\s*[.\\-_)]+\\s*|\\s+)");
    private static final Pattern LIVE = Pattern.compile("(?iu)(?<![\\p{L}])(?:live|unplugged|концерт\\p{L}*|живой концерт)(?![\\p{L}])");
    private static final Pattern COLLECTION = Pattern.compile("(?iu)(?:сборник|сборка|дискотек|various artists|soundtrack|хиты|русский хит)");
    private static final Pattern TITLE_FORMAT = Pattern.compile("(?i)\\s+(?:\\[|\\()?(?:MP3|FLAC|WAV|lossless|APE|ALAC)(?:\\b.*)$");
    private static final String[] RUSSIAN = {"Мельница", "Алиса", "Машина времени", "Сектор Газа", "Земфира",
            "СерьГа", "Сплин", "Ария", "Би-2", "Пикник", "Чайф", "Браво", "Мегаполис", "МакSим",
            "Иван Дорн", "Гости из будущего", "Кастa", "Слот", "Noize MC", "Моя Мишель"};
    private static final String[] FOREIGN = {"Adele", "Radiohead", "Stevie Wonder", "Daft Punk", "Nina Simone",
            "Nirvana", "Florence and the Machine", "Stromae", "Green Day", "Evanescence", "Massive Attack",
            "ABBA", "The Cranberries", "Gorillaz", "Björk", "Hozier", "Arctic Monkeys", "The Prodigy"};
    private static final Map<String, List<String>> GENRE_QUERIES = genreQueries();

    private static Map<String, List<String>> genreQueries() {
        var genres = new HashMap<String, List<String>>();
        genres.put("rock", Arrays.asList("Алиса", "Foo Fighters", "Наутилус Помпилиус", "Muse"));
        genres.put("pop", Arrays.asList("Моя Мишель", "Dua Lipa", "Гости из будущего", "ABBA"));
        genres.put("metal", Arrays.asList("Ария", "Metallica", "Мастер", "Nightwish"));
        genres.put("folk", Arrays.asList("Мельница", "The Dubliners", "Пелагея", "Wardruna"));
        genres.put("hip-hop", Arrays.asList("Каста", "A Tribe Called Quest", "Элджей", "Outkast"));
        genres.put("electronic", Arrays.asList("ППК", "Daft Punk", "Нейромонах Феофан", "The Chemical Brothers"));
        genres.put("jazz", Arrays.asList("Игорь Бутман", "Miles Davis", "Алексей Козлов", "Nina Simone"));
        genres.put("chanson", Arrays.asList("Михаил Круг", "Charles Aznavour", "Любэ", "Édith Piaf"));
        genres.put("blues", Arrays.asList("Алексей Белов", "B.B. King", "Воскресение", "Etta James"));
        genres.put("reggae", Arrays.asList("Комитет охраны тепла", "Bob Marley", "Джа Дивижн", "Jimmy Cliff"));
        return genres;
    }

    record Recording(String artist, String title, boolean live) {
        boolean matches(Collection<String> query) {
            var terms = new HashSet<>(PublicIndex.words(artist + " " + title));
            return !query.isEmpty() && terms.containsAll(query);
        }
    }

    private record Candidate(String key, byte[] metadata, String magnet, List<Tracks.Track> tracks) { }
    private record Discovery(String query, String genre, String language) { }

    private static final class Download {
        final TorrentHandle handle;
        final File directory;
        final TorrentInfo info;
        final Map<Integer, Waiters> files = new LinkedHashMap<>();

        Download(TorrentHandle handle, File directory, TorrentInfo info) {
            this.handle = handle;
            this.directory = directory;
            this.info = info;
        }
    }

    private static final class Waiters {
        int total;
        int foreground;
    }

    private final Context context;
    private final Tracks tracks;
    private final PublicIndex index = new PublicIndex();
    private final File metadata;
    private final File work;
    private final File audio;
    private final Object lock = new Object();
    private final Object metadataLock = new Object();
    private final Map<String, Integer> resolving = new HashMap<>();
    private final Map<String, Set<String>> alternateCache = new HashMap<>();
    private final Map<String, Download> downloads = new HashMap<>();
    private final ExecutorService lookup = Executors.newFixedThreadPool(6, runnable -> {
        Thread thread = new Thread(runnable, "RadioWave sources");
        thread.setDaemon(true);
        return thread;
    });
    private volatile boolean closed;
    private SessionManager magnetSession;
    private SessionManager downloadSession;
    private int rotation;

    public TorrentSources(Context context, Tracks tracks) {
        this.context = context.getApplicationContext();
        this.tracks = tracks;
        metadata = new File(this.context.getFilesDir(), "torrent-metadata");
        work = new File(this.context.getCacheDir(), "torrent-staging");
        audio = new File(this.context.getCacheDir(), "torrent-audio");
    }

    public List<Tracks.Track> importMagnet(String value) throws IOException {
        String magnet = value.trim();
        if (PublicIndex.infoHash(magnet).isEmpty()) throw new IOException("Некорректная magnet-ссылка");
        Candidate candidate = fromMagnet(magnet, "", "", "", 8);
        if (candidate == null) throw new IOException("Не удалось получить список файлов раздачи");
        return save(candidate, candidate.tracks());
    }

    public List<Tracks.Track> importTorrent(Uri uri) throws IOException {
        byte[] bytes;
        try (InputStream stream = context.getContentResolver().openInputStream(uri)) {
            if (stream == null) throw new IOException("Не удалось открыть файл раздачи");
            bytes = PublicIndex.bounded(stream, PublicIndex.TORRENT_LIMIT,
                    System.nanoTime() + TimeUnit.SECONDS.toNanos(12));
        }
        Candidate candidate = candidate(bytes, "", "", "", "");
        return save(candidate, candidate.tracks());
    }

    public List<Tracks.Track> search(String query) throws IOException {
        List<String> terms = PublicIndex.words(query);
        if (terms.isEmpty()) return Collections.emptyList();
        String canonical = String.join(" ", terms);
        var queries = new LinkedHashSet<String>();
        queries.add(query.trim());
        if (terms.size() > 1) {
            String[] artistAndTitle = SPLIT.split(query, 2);
            if (artistAndTitle.length > 1) {
                queries.add(artistAndTitle[0].trim());
            } else {
                if (terms.size() > 2) queries.add(String.join(" ", terms.subList(0, 2)));
                queries.add(terms.get(0));
            }
        }
        List<Callable<List<PublicIndex.Release>>> network = new ArrayList<>();
        for (String phrase : queries) {
            network.add(() -> index.searchRuTor(phrase));
            if (phrase.equals(query.trim())) network.add(() -> index.searchJacRed(phrase));
        }
        var releases = new ArrayList<PublicIndex.Release>();
        boolean answered = false;
        for (List<PublicIndex.Release> fromProvider : run(network, 7)) {
            if (fromProvider != null) {
                answered = true;
                releases.addAll(fromProvider);
            }
        }
        if (!answered) throw new IOException("Индексы поиска не ответили");

        List<PublicIndex.Release> ordered = PublicIndex.ordered(releases);
        if (ordered.isEmpty()) return Collections.emptyList();

        Set<String> requested = new HashSet<>(terms);
        Comparator<PublicIndex.Release> relevance = Comparator.<PublicIndex.Release>comparingInt(release ->
                new HashSet<>(PublicIndex.words(release.title())).containsAll(requested) ? 0 : 1)
                .thenComparing(release -> LIVE.matcher(release.title()).find() && !LIVE.matcher(canonical).find())
                .thenComparing(Comparator.comparingInt(PublicIndex.Release::seeders).reversed());
        var candidates = materialize(ordered.stream().sorted(relevance).limit(8).collect(java.util.stream.Collectors.toList()), "", "");
        var result = new ArrayList<Tracks.Track>();
        for (Candidate candidate : candidates) {
            List<Tracks.Track> matches = new ArrayList<>();
            for (Tracks.Track track : candidate.tracks()) {
                if (new Recording(track.artist(), track.title(), track.live()).matches(terms)) matches.add(track);
            }
            if (!matches.isEmpty()) result.addAll(save(candidate, matches));
        }
        result.sort(Comparator.comparing(Tracks.Track::live));
        return result.size() > 100 ? new ArrayList<>(result.subList(0, 100)) : result;
    }

    public List<Tracks.Track> discover(boolean preferForeign, Set<String> artistsToAvoid,
                                        List<String> genres) throws IOException {
        int turn;
        synchronized (lock) {
            turn = rotation++;
        }
        Set<String> avoided = new HashSet<>();
        for (String artist : artistsToAvoid) avoided.add(artistKey(artist));
        List<Discovery> queries = discoveryQueries(preferForeign, avoided, genres, turn);
        List<Callable<List<PublicIndex.Release>>> jobs = new ArrayList<>();
        for (Discovery discovery : queries) jobs.add(() -> lookupDiscovery(discovery.query()));
        var hits = run(jobs, 7);
        var ranked = new ArrayList<PublicIndex.Release>();
        var hints = new HashMap<String, Discovery>();
        for (int i = 0; i < queries.size(); i++) {
            Discovery discovery = queries.get(i);
            List<PublicIndex.Release> releases = hits.get(i);
            if (releases == null) continue;
            int used = 0;
            for (PublicIndex.Release release : releases) {
                if (release.seeders() < 1 || LIVE.matcher(release.title()).find()) continue;
                if (!new HashSet<>(PublicIndex.words(release.title())).containsAll(PublicIndex.words(discovery.query()))) continue;
                if (hints.putIfAbsent(release.key(), discovery) == null) ranked.add(release);
                if (++used == 2) break;
            }
        }
        if (ranked.isEmpty()) {
            IOException original = null;
            try {
                for (PublicIndex.Release release : index.latest()) {
                    if (release.seeders() > 0 && !LIVE.matcher(release.title()).find()) ranked.add(release);
                    if (ranked.size() == 8) break;
                }
            } catch (IOException error) {
                original = error;
            }
            if (ranked.isEmpty() && original != null) throw original;
        }

        List<Callable<Candidate>> metadataJobs = new ArrayList<>();
        for (PublicIndex.Release release : ranked.subList(0, Math.min(8, ranked.size()))) {
            Discovery hint = hints.get(release.key());
            metadataJobs.add(() -> fromRelease(release, hint == null ? "" : hint.genre(), hint == null ? "" : hint.language()));
        }
        var selected = new ArrayList<Tracks.Track>();
        Set<String> offered = new HashSet<>();
        var offeredArtists = new HashMap<String, Integer>();
        for (Candidate candidate : run(metadataJobs, 11)) {
            if (candidate == null) continue;
            List<Tracks.Track> fresh = new ArrayList<>();
            for (Tracks.Track track : candidate.tracks()) {
                String artist = artistKey(track.artist());
                if (artist.isEmpty() || avoided.contains(artist) || track.live()
                        || offeredArtists.getOrDefault(artist, 0) >= 2
                        || tracks.get(track.id()) != null || !offered.add(track.id())) continue;
                offeredArtists.put(artist, offeredArtists.getOrDefault(artist, 0) + 1);
                fresh.add(track);
                if (selected.size() + fresh.size() >= 16) break;
            }
            if (!fresh.isEmpty()) selected.addAll(save(candidate, fresh));
            if (selected.size() >= 16) break;
        }
        return selected;
    }

    private List<PublicIndex.Release> lookupDiscovery(String query) throws IOException {
        try {
            List<PublicIndex.Release> releases = index.searchRuTor(query);
            if (!releases.isEmpty()) return releases;
        } catch (IOException error) {
            Log.d(TAG, "Основной индекс недоступен для подбора", error);
        }
        return index.searchJacRed(query);
    }

    private static List<Discovery> discoveryQueries(boolean foreign, Set<String> avoid,
                                                        List<String> genres, int turn) {
        var result = new ArrayList<Discovery>();
        Set<String> used = new HashSet<>(avoid);
        for (String genre : genres) {
            List<String> pairs = GENRE_QUERIES.get(genre.toLowerCase(Locale.ROOT));
            if (pairs == null) continue;
            int foregroundSide = (foreign ? 1 : 0) + (turn % 2 * 2);
            int otherSide = (foreign ? 0 : 1) + ((turn + 1) % 2 * 2);
            addDiscovery(result, used, pairs.get(foregroundSide), genre, foreign ? "" : "ru");
            addDiscovery(result, used, pairs.get(otherSide), genre, foreign ? "ru" : "");
            if (result.size() >= 4) break;
        }
        for (int i = 0; i < RUSSIAN.length && result.size() < 6; i++) {
            if (foreign) addDiscovery(result, used, FOREIGN[(turn + i) % FOREIGN.length], "", "");
            addDiscovery(result, used, RUSSIAN[(turn + i) % RUSSIAN.length], "", "ru");
            if (!foreign && result.size() < 6) addDiscovery(result, used, FOREIGN[(turn + i) % FOREIGN.length], "", "");
        }
        return result;
    }

    private static void addDiscovery(List<Discovery> target, Set<String> used, String name, String genre,
                                     String language) {
        if (target.size() < 6 && used.add(artistKey(name))) target.add(new Discovery(name, genre, language));
    }

    private List<Candidate> materialize(List<PublicIndex.Release> releases, String genre, String language) throws IOException {
        List<Callable<Candidate>> jobs = new ArrayList<>();
        for (PublicIndex.Release release : releases) jobs.add(() -> fromRelease(release, genre, language));
        var sources = new ArrayList<Candidate>();
        for (Candidate candidate : run(jobs, 11)) if (candidate != null) sources.add(candidate);
        return sources;
    }

    private Candidate fromRelease(PublicIndex.Release release, String genre, String language) throws IOException {
        if (release.torrent() != null) {
            try {
                return candidate(index.fetch(release), release.magnet(), release.title(), genre, language);
            } catch (IOException error) {
                if (release.magnet().isEmpty()) throw error;
                Log.d(TAG, "Файл раздачи недоступен, пробуем magnet", error);
            }
        }
        return fromMagnet(release.magnet(), release.title(), genre, language, 5);
    }

    private Candidate fromMagnet(String magnet, String release, String genre, String language, int timeout)
            throws IOException {
        if (magnet.isEmpty() || Thread.currentThread().isInterrupted()) return null;
        SessionManager session = session(true);
        byte[] bytes = session.fetchMagnet(magnet, timeout, directory(work));
        if (bytes == null) return null;
        return candidate(bytes, magnet, release, genre, language);
    }

    private Candidate candidate(byte[] bytes, String magnet, String release, String genre, String language)
            throws IOException {
        if (bytes.length > PublicIndex.TORRENT_LIMIT) throw new IOException("Слишком большой файл раздачи");
        TorrentInfo info = torrent(bytes);
        String key = key(info);
        String magnetHash = PublicIndex.infoHash(magnet);
        if (!magnetHash.isEmpty() && !key.equals(magnetHash)) throw new IOException("Раздача не совпадает с magnet");
        String displayRelease = release.isEmpty() ? info.name() : release;
        FileStorage files = info.files();
        List<Tracks.Track> sounds = new ArrayList<>();
        for (int number = 0; number < files.numFiles(); number++) {
            if (files.fileFlags(number).and_(FileStorage.FLAG_SYMLINK).nonZero()) {
                throw new IOException("Раздача содержит символьные ссылки");
            }
            String path = files.filePath(number);
            long length = files.fileSize(number);
            if (files.padFileAt(number) || length <= 0 || length > MAX_AUDIO_BYTES || !AUDIO.matcher(path).find()) {
                continue;
            }
            Recording recording = describe(displayRelease, path);
            String name = recording.artist();
            String sourceGenre = genre.isEmpty() ? genreIn(displayRelease) : genre;
            sounds.add(new Tracks.Track(id(key, number), name, recording.title(), sourceGenre, language,
                    "", key, number, recording.live()));
        }
        return new Candidate(key, bytes, magnet, sounds);
    }

    private List<Tracks.Track> save(Candidate candidate, List<Tracks.Track> sounds) throws IOException {
        if (sounds.isEmpty()) return Collections.emptyList();
        ensureOpen();
        synchronized (metadataLock) {
            File torrent = metadataFile(candidate.key());
            if (!torrent.isFile()) atomicWrite(torrent, candidate.metadata());
            if (!candidate.magnet().isEmpty()) {
                File magnet = magnetFile(candidate.key());
                if (!magnet.isFile()) atomicWrite(magnet, candidate.magnet().getBytes(StandardCharsets.UTF_8));
            }
            tracks.addAll(sounds);
        }
        return sounds;
    }

    public Uri resolve(Tracks.Track track, BooleanSupplier cancelled) throws IOException {
        return resolve(track, cancelled, false);
    }

    public Uri resolve(Tracks.Track track, BooleanSupplier cancelled, boolean foreground) throws IOException {
        if (track.isLocal()) {
            if (track.uri().isEmpty()) throw new IOException("У композиции нет файла");
            return Uri.parse(track.uri());
        }
        long deadline = System.nanoTime() + TRACK_TIMEOUT_NS;
        IOException failure = null;
        var choices = new ArrayList<Tracks.Track>();
        choices.add(track);
        String key = recordingKey(track);
        for (Tracks.Track candidate : tracks.all()) {
            if (!candidate.isLocal() && !candidate.id().equals(track.id())
                    && candidate.live() == track.live() && recordingKey(candidate).equals(key)) choices.add(candidate);
        }
        try {
            for (Tracks.Track source : choices.subList(0, Math.min(3, choices.size()))) {
                if (expired(deadline, cancelled)) break;
                try {
                    return resolveOne(source, track.id(), cancelled, deadline, foreground);
                } catch (IOException error) {
                    failure = error;
                }
            }
        } catch (RuntimeException error) {
            throw new IOException("Ошибка загрузки раздачи", error);
        }
        if (failure != null) throw failure;
        throw new IOException("Загрузка отменена");
    }

    private Uri resolveOne(Tracks.Track track, String requestedId, BooleanSupplier cancelled, long timeout,
                           boolean foreground) throws IOException {
        String key = track.torrentKey();
        int index = track.fileIndex();
        if (!STORE_KEY.matcher(key).matches()) throw new IOException("Некорректный идентификатор раздачи");
        TorrentInfo info = torrent(metadataFile(key));
        FileStorage files = info.files();
        if (index < 0 || index >= files.numFiles()) throw new IOException("Трек отсутствует в раздаче");
        String filename = files.fileName(index);
        var extension = AUDIO.matcher(filename);
        long expected = files.fileSize(index);
        if (!extension.find() || expected <= 0 || expected > MAX_AUDIO_BYTES) {
            throw new IOException("Раздача не содержит подходящего аудиофайла");
        }
        String cachedName = id(key, index) + extension.group().toLowerCase(Locale.ROOT);

        File cachedFile = new File(directory(audio), cachedName);
        String id = track.id();
        synchronized (lock) {
            ensureOpen();
            resolving.put(id, resolving.getOrDefault(id, 0) + 1);
        }
        Download download = null;
        try {
            // Keep the cache check and attachment atomic with respect to another
            // waiter publishing the same file and releasing the native handle.
            synchronized (lock) {
                if (cachedFile.isFile() && cachedFile.length() == expected) {
                    cachedFile.setLastModified(System.currentTimeMillis());
                    recordResolved(requestedId, id);
                    return Uri.fromFile(cachedFile);
                }
                download = acquire(key, info, index, foreground);
            }
            File output = new File(download.directory, workName(index));
            long lastChange = System.nanoTime();
            long lastAmount = 0;
            while (!expired(timeout, cancelled)) {
                if (!download.handle.isValid()) throw new IOException("Источник отключился");
                var status = download.handle.status();
                if (status.errorCode().isError()) throw new IOException("Раздача остановилась: " + status.errorCode().message());
                long[] progress = download.handle.fileProgress(TorrentHandle.PIECE_GRANULARITY);
                long downloaded = index < progress.length ? progress[index] : 0;
                if (downloaded >= expected && output.isFile() && output.length() == expected) {
                    awaitFlush(download, cancelled, timeout);
                    Uri uri = publish(output, cachedFile, expected);
                    if (expired(timeout, cancelled)) throw new IOException("Загрузка отменена");
                    tracks.enrich(track, uri);
                    recordResolved(requestedId, id);
                    return uri;
                }
                long amount = Math.max(downloaded, status.totalPayloadDownload());
                if (amount > lastAmount) {
                    lastAmount = amount;
                    lastChange = System.nanoTime();
                } else if (System.nanoTime() - lastChange >= NO_PROGRESS_NS) {
                    throw new IOException(downloaded == 0 && status.numPeers() == 0
                            ? "Нет активных раздающих" : "Загрузка перестала продвигаться");
                }
                pause(160);
            }
            throw new IOException(closed || cancelled.getAsBoolean() ? "Загрузка отменена" : "Не удалось дождаться файла");
        } finally {
            synchronized (lock) {
                if (download != null) release(key, download, index, foreground);
                int count = resolving.getOrDefault(id, 0) - 1;
                if (count > 0) resolving.put(id, count);
                else resolving.remove(id);
            }
        }
    }

    private void recordResolved(String requested, String actual) {
        synchronized (lock) {
            if (!requested.equals(actual)) {
                alternateCache.computeIfAbsent(requested, ignored -> new HashSet<>()).add(actual);
            }
        }
    }

    private Download acquire(String key, TorrentInfo info, int selected, boolean foreground) throws IOException {
        synchronized (lock) {
            ensureOpen();
            Download existing = downloads.get(key);
            if (existing != null) {
                addWaiter(existing, selected, foreground);
                prioritize(existing);
                return existing;
            }

            SessionManager session = session(false);
            File target = Files.createTempDirectory(directory(work).toPath(), key + "-").toFile();
            add_torrent_params params = info.swigParams();
            var names = new int_string_map();
            var flags = params.getFlags().and_(TorrentFlags.AUTO_MANAGED.inv())
                    .and_(TorrentFlags.PAUSED.inv()).or_(TorrentFlags.SEQUENTIAL_DOWNLOAD);
            params.setFlags(flags);
            params.setSave_path(target.getAbsolutePath());
            FileStorage files = info.files();
            try {
                for (int i = 0; i < files.numFiles(); i++) {
                    if (!files.padFileAt(i)) names.put(i, workName(i));
                }
                params.set_renamed_files(names);
                byte[] initial = new byte[files.numFiles()];
                initial[selected] = foreground ? (byte) 7 : (byte) 4;
                var priorities = new byte_vector(initial);
                try {
                    params.set_file_priorities2(priorities);
                } finally {
                    priorities.delete();
                }
                addMagnetPeers(params, key);
                var error = new error_code();
                try {
                    TorrentHandle handle = new TorrentHandle(session.swig().add_torrent(params, error));
                    if (error.value() != 0 || !handle.isValid() || !target.getAbsolutePath().equals(handle.savePath())) {
                        throw new IOException("Не удалось подключить раздачу: " + error.message());
                    }
                    var download = new Download(handle, target, info);
                    addWaiter(download, selected, foreground);
                    downloads.put(key, download);
                    return download;
                } finally {
                    error.delete();
                }
            } catch (RuntimeException | IOException error) {
                target.delete();
                throw error;
            } finally {
                names.delete();
            }
        }
    }

    private void addMagnetPeers(add_torrent_params params, String key) throws IOException {
        File file = magnetFile(key);
        if (!file.isFile() || file.length() > 8192) return;
        String magnet = new String(Files.readAllBytes(file.toPath()), StandardCharsets.UTF_8);
        AddTorrentParams peerSource = AddTorrentParams.parseMagnetUri(magnet);
        params.set_peers(peerSource.swig().get_peers());
    }

    private static void addWaiter(Download download, int index, boolean foreground) {
        Waiters waiters = download.files.computeIfAbsent(index, ignored -> new Waiters());
        waiters.total++;
        if (foreground) waiters.foreground++;
    }

    private void release(String key, Download download, int index, boolean foreground) {
        Waiters waiters = download.files.get(index);
        waiters.total--;
        if (foreground) waiters.foreground--;
        if (waiters.total == 0) download.files.remove(index);
        if (download.files.isEmpty()) {
            downloads.remove(key);
            if (!closed && download.handle.isValid()) downloadSession.remove(download.handle, SessionHandle.DELETE_FILES);
        } else if (!closed && download.handle.isValid()) {
            prioritize(download);
        }
    }

    private static void prioritize(Download download) {
        Priority[] priorities = Priority.array(Priority.IGNORE, download.info.numFiles());
        for (Map.Entry<Integer, Waiters> active : download.files.entrySet()) {
            priorities[active.getKey()] = active.getValue().foreground > 0 ? Priority.SEVEN : Priority.FOUR;
        }
        download.handle.prioritizeFiles(priorities);
    }

    private void awaitFlush(Download download, BooleanSupplier cancelled, long timeout) throws IOException {
        String hash = download.handle.infoHash().toHex();
        var signal = new CountDownLatch(1);
        AlertListener listener = new AlertListener() {
            @Override
            public int[] types() {
                return new int[]{AlertType.CACHE_FLUSHED.swig()};
            }

            @Override
            public void alert(Alert<?> alert) {
                if (alert instanceof CacheFlushedAlert finished
                        && finished.handle().isValid() && finished.handle().infoHash().toHex().equals(hash)) {
                    signal.countDown();
                }
            }
        };
        downloadSession.addListener(listener);
        try {
            download.handle.flushCache();
            long limit = Math.min(timeout, System.nanoTime() + TimeUnit.SECONDS.toNanos(3));
            while (!expired(limit, cancelled)) {
                if (signal.await(100, TimeUnit.MILLISECONDS)) return;
            }
            throw new IOException("Не удалось завершить запись аудиофайла");
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
            throw new IOException("Загрузка отменена", error);
        } finally {
            downloadSession.removeListener(listener);
        }
    }

    private Uri publish(File source, File target, long size) throws IOException {
        File staging = File.createTempFile("incoming-", ".tmp", directory(audio));
        try {
            Files.copy(source.toPath(), staging.toPath(), StandardCopyOption.REPLACE_EXISTING);
            if (staging.length() != size) throw new IOException("Аудиофайл ещё не готов");
            synchronized (lock) {
                ensureOpen();
                if (!target.isFile() || target.length() != size) {
                    Files.move(staging.toPath(), target.toPath(), StandardCopyOption.REPLACE_EXISTING);
                }
            }
            return Uri.fromFile(target);
        } finally {
            staging.delete();
        }
    }

    public void retain(Set<String> trackIds) {
        long now = System.currentTimeMillis();
        synchronized (lock) {
            Set<String> retained = new HashSet<>(trackIds);
            for (String id : trackIds) {
                Set<String> alternates = alternateCache.get(id);
                if (alternates != null) retained.addAll(alternates);
            }
            alternateCache.keySet().retainAll(trackIds);
            File[] cached = audio.listFiles();
            if (cached != null) {
                for (File file : cached) {
                    String name = file.getName();
                    if (!STORE_MEDIA.matcher(name).matches()) continue;
                    String id = name.substring(0, name.lastIndexOf('.'));
                    if (!retained.contains(id) && !resolving.containsKey(id) && now - file.lastModified() > CACHE_GRACE_MS) {
                        if (!file.delete()) Log.w(TAG, "Не удалось очистить файл аудиокэша");
                    }
                }
            }
            File[] staging = work.listFiles();
            if (staging == null) return;
            Set<File> active = new HashSet<>();
            for (Download download : downloads.values()) active.add(download.directory);
            for (File directory : staging) {
                if (!active.contains(directory) && directory.isDirectory()
                        && directory.getName().matches("[0-9a-f]{40,64}-[0-9]+")
                        && now - directory.lastModified() > TimeUnit.MINUTES.toMillis(2)) {
                    clearStaging(directory);
                }
            }
        }
    }

    private static void clearStaging(File directory) {
        File[] files = directory.listFiles();
        if (files == null) return;
        for (File file : files) {
            if (file.isFile()) file.delete();
        }
        directory.delete();
    }

    private synchronized SessionManager session(boolean magnetOnly) throws IOException {
        ensureOpen();
        if (magnetOnly && magnetSession != null) return magnetSession;
        if (!magnetOnly && downloadSession != null) return downloadSession;
        try {
            var manager = new SessionManager();
            manager.start();
            if (!manager.isRunning()) throw new IOException("Не удалось запустить BitTorrent");
            if (magnetOnly) magnetSession = manager;
            else downloadSession = manager;
            return manager;
        } catch (LinkageError error) {
            throw new IOException("Движок BitTorrent недоступен на устройстве", error);
        }
    }

    @Override
    public void close() {
        SessionManager magnets;
        SessionManager media;
        synchronized (lock) {
            if (closed) return;
            closed = true;
            lookup.shutdownNow();
            synchronized (this) {
                magnets = magnetSession;
                media = downloadSession;
            }
        }
        if (magnets != null) magnets.stop();
        if (media != null) media.stop();
    }

    private <T> List<T> run(List<Callable<T>> jobs, int seconds) throws IOException {
        ensureOpen();
        if (jobs.isEmpty()) return Collections.emptyList();
        try {
            List<Future<T>> futures = lookup.invokeAll(jobs, seconds, TimeUnit.SECONDS);
            List<T> results = new ArrayList<>();
            for (Future<T> future : futures) {
                T value = null;
                if (!future.isCancelled()) {
                    try {
                        value = future.get();
                    } catch (ExecutionException error) {
                        Log.d(TAG, "Один источник не ответил", error.getCause());
                    }
                }
                results.add(value);
            }
            return results;
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
            throw new IOException("Поиск отменён", error);
        }
    }

    private boolean expired(long deadline, BooleanSupplier cancelled) {
        return closed || cancelled.getAsBoolean() || Thread.currentThread().isInterrupted() || System.nanoTime() - deadline >= 0;
    }

    private void ensureOpen() throws IOException {
        if (closed) throw new IOException("Загрузчик отключён");
    }

    private static void pause(int milliseconds) throws IOException {
        try {
            Thread.sleep(milliseconds);
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
            throw new IOException("Загрузка отменена", error);
        }
    }

    private static String key(TorrentInfo info) throws IOException {
        String key = info.infoHashType().has_v1() ? info.infoHashV1().toHex() : info.infoHashV2().toHex();
        if (!STORE_KEY.matcher(key).matches()) throw new IOException("Неверный хеш раздачи");
        return key;
    }

    private static TorrentInfo torrent(byte[] bytes) throws IOException {
        try {
            return verified(new TorrentInfo(bytes));
        } catch (IllegalArgumentException error) {
            throw new IOException("Некорректный файл раздачи", error);
        } catch (LinkageError error) {
            throw new IOException("Движок BitTorrent недоступен на устройстве", error);
        }
    }

    private static TorrentInfo torrent(File file) throws IOException {
        if (!file.isFile() || file.length() > PublicIndex.TORRENT_LIMIT) {
            throw new IOException("Метаданные раздачи отсутствуют");
        }
        return torrent(Files.readAllBytes(file.toPath()));
    }

    private static TorrentInfo verified(TorrentInfo info) throws IOException {
        if (!info.isValid() || info.numFiles() < 1 || info.numFiles() > MAX_FILES) {
            throw new IOException("Некорректная раздача или слишком много файлов");
        }
        return info;
    }

    static Recording describe(String release, String relative) {
        String file = relative.substring(Math.max(relative.lastIndexOf('/'), relative.lastIndexOf('\\')) + 1);
        String plain = AUDIO.matcher(file).replaceFirst("").trim();
        plain = NUMBER.matcher(plain).replaceFirst("").trim();
        String[] filePair = SPLIT.split(plain, 2);
        String[] releasePair = SPLIT.split(release, 2);
        String artist = cleanArtist(releasePair[0]);
        String title = plain;
        if (filePair.length > 1 && !artistKey(filePair[0]).isEmpty() && !filePair[1].trim().isEmpty()) {
            if (releasePair.length == 1 || COLLECTION.matcher(release).find()
                    || artistKey(filePair[0]).equals(artistKey(artist))) {
                artist = cleanArtist(filePair[0]);
                title = filePair[1].trim();
            }
        }
        if (artist.isEmpty() || COLLECTION.matcher(artist).find()) artist = "Неизвестный исполнитель";
        return new Recording(artist, title.isEmpty() ? file : title, LIVE.matcher(release + " " + relative).find());
    }

    private static String cleanArtist(String value) {
        return TITLE_FORMAT.matcher(value).replaceFirst("").replaceAll("^\\s*[\\[(]+|[\\])]+\\s*$", "").trim();
    }

    private static String genreIn(String release) {
        var words = new HashSet<>(PublicIndex.words(release));
        if (words.contains("шансон") || words.contains("chanson")) return "chanson";
        if (words.contains("metal") || words.contains("метал")) return "metal";
        if (words.contains("рок") || words.contains("rock")) return "rock";
        if (words.contains("pop") || words.contains("поп") || words.contains("эстрада")) return "pop";
        if (words.contains("jazz") || words.contains("джаз")) return "jazz";
        if (words.contains("folk") || words.contains("фолк")) return "folk";
        if (words.contains("рэп") || words.contains("rap") || (words.contains("hip") && words.contains("hop"))) return "hip-hop";
        if (words.contains("electronic") || words.contains("электроника")) return "electronic";
        if (words.contains("blues") || words.contains("блюз")) return "blues";
        if (words.contains("reggae") || words.contains("регги")) return "reggae";
        return "";
    }

    private static String recordingKey(Tracks.Track track) {
        return artistKey(track.artist()) + "|" + String.join(" ", PublicIndex.words(track.title()));
    }

    private static String artistKey(String value) {
        return String.join(" ", PublicIndex.words(value));
    }

    private static String id(String key, int index) {
        return key + "-" + index;
    }

    private static String workName(int index) {
        return "audio-" + index + ".data";
    }

    private File metadataFile(String key) {
        return new File(metadata, key + ".torrent");
    }

    private File magnetFile(String key) {
        return new File(metadata, key + ".magnet.txt");
    }

    private static File directory(File directory) throws IOException {
        if (!directory.isDirectory() && !directory.mkdirs() && !directory.isDirectory()) {
            throw new IOException("Не удалось создать каталог для скачивания");
        }
        return directory;
    }

    private static void atomicWrite(File destination, byte[] contents) throws IOException {
        File folder = directory(destination.getParentFile());
        File staging = File.createTempFile("metadata-", ".tmp", folder);
        try {
            Files.write(staging.toPath(), contents);
            Files.move(staging.toPath(), destination.toPath(), StandardCopyOption.REPLACE_EXISTING);
        } finally {
            staging.delete();
        }
    }
}
