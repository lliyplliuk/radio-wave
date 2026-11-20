package org.radiowave.android;

import android.app.PendingIntent;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.util.Log;
import android.view.KeyEvent;

import androidx.annotation.NonNull;
import androidx.media3.common.AudioAttributes;
import androidx.media3.common.C;
import androidx.media3.common.ForwardingSimpleBasePlayer;
import androidx.media3.common.MediaItem;
import androidx.media3.common.MediaMetadata;
import androidx.media3.common.PlaybackException;
import androidx.media3.common.Player;
import androidx.media3.common.util.UnstableApi;
import androidx.media3.exoplayer.ExoPlayer;
import androidx.media3.session.DefaultMediaNotificationProvider;
import androidx.media3.session.MediaSession;
import androidx.media3.session.MediaSessionService;
import androidx.media3.session.SessionCommand;
import androidx.media3.session.SessionCommands;
import androidx.media3.session.SessionError;
import androidx.media3.session.SessionResult;

import com.google.common.util.concurrent.Futures;
import com.google.common.util.concurrent.ListenableFuture;
import com.google.common.util.concurrent.SettableFuture;

import java.io.IOException;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.Callable;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;

import org.radiowave.android.Tracks.Track;

@UnstableApi
public final class PlaybackService extends MediaSessionService {
    private static final String TAG = "RadioWave";
    private static final int VISIBLE = 4;
    private static final int WINDOW = 16;
    private static final int PREFETCH = 4;
    private static final int KEEP_PLAYED = 4;
    private static final long RETRY_SOURCE_MS = 120_000;
    private static final long DISCOVERY_RETRY_MS = 8_000;

    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService immediate = Executors.newFixedThreadPool(2);
    private final ExecutorService preload = Executors.newFixedThreadPool(2);
    private final ExecutorService remote = Executors.newFixedThreadPool(2);
    private final ExecutorService search = Executors.newSingleThreadExecutor();
    private final List<Track> queue = new ArrayList<>();
    private final ArrayDeque<Track> past = new ArrayDeque<>();
    private final ArrayDeque<String> keepPlayed = new ArrayDeque<>();
    private final PrefetchWindow prefetch = new PrefetchWindow();
    private final Map<String, Long> unavailable = new HashMap<>();
    private final Map<String, Uri> ready = new HashMap<>();
    private final Set<String> playerItems = new HashSet<>();

    private ExoPlayer audio;
    private MediaSession session;
    private Tracks tracks;
    private Wave wave;
    private TorrentSources sources;
    private Future<?> pending;
    private Track current;
    private Track requested;
    private int request;
    private int succession;
    private boolean discovering;
    private boolean waitingForNext;
    private boolean refreshPending;
    private boolean wantsPlay;
    private boolean exact;
    private boolean heard;
    private boolean closed;
    private String status = "Нажмите «Пуск», чтобы включить волну";
    private long audibleMs;
    private long audioStartedAt;
    private String lastSnapshot = "";

    static final class Prefetch {
        Future<?> work;
        void cancel() { if (work != null) work.cancel(true); }
    }

    static final class PrefetchWindow {
        final Map<String, Prefetch> jobs = new HashMap<>();
        final Map<String, Long> retryAt = new HashMap<>();

        void retainJobs(Set<String> upcoming, String requestedId) {
            jobs.entrySet().removeIf(entry -> {
                String id = entry.getKey();
                if (upcoming.contains(id) || id.equals(requestedId)) return false;
                entry.getValue().cancel();
                return true;
            });
        }

        long nextRetry(Set<String> upcoming, long now) {
            retryAt.values().removeIf(time -> time <= now);
            long next = Long.MAX_VALUE;
            for (String id : upcoming) next = Math.min(next, retryAt.getOrDefault(id, Long.MAX_VALUE));
            return next;
        }
    }

    @Override
    public void onCreate() {
        super.onCreate();
        tracks = new Tracks(this);
        wave = new Wave(tracks);
        List<Track> recent = wave.recent();
        for (int index = Math.min(KEEP_PLAYED, recent.size()) - 1; index >= 0; index--) {
            String id = recent.get(index).id();
            keepPlayed.remove(id);
            keepPlayed.addLast(id);
        }
        sources = new TorrentSources(this, tracks);

        audio = new ExoPlayer.Builder(this)
                .setAudioAttributes(new AudioAttributes.Builder()
                        .setUsage(C.USAGE_MEDIA).setContentType(C.AUDIO_CONTENT_TYPE_MUSIC).build(), true)
                .setHandleAudioBecomingNoisy(true)
                .setWakeMode(C.WAKE_MODE_LOCAL)
                .build();
        audio.addListener(new Player.Listener() {
            @Override
            public void onIsPlayingChanged(boolean playing) {
                markAudible(playing);
                if (playing && current != null && !heard) {
                    heard = true;
                    succession = 0;
                    wave.played(current);
                    keepPlayed.remove(current.id());
                    keepPlayed.addLast(current.id());
                    while (keepPlayed.size() > KEEP_PLAYED) keepPlayed.removeFirst();
                    maintainCache();
                }
                publish();
            }

            @Override
            public void onPlaybackStateChanged(int state) {
                if (state == Player.STATE_ENDED && current != null && requested == null && wantsPlay) {
                    advance(false);
                }
                publish();
            }

            @Override
            public void onPlayerError(@NonNull PlaybackException error) {
                Track failed = current;
                if (requested != null || failed == null) return;
                unavailable.put(failed.id(), SystemClock.elapsedRealtime() + RETRY_SOURCE_MS);
                ready.remove(failed.id());
                boolean manual = exact;
                main.post(() -> {
                    if (current != failed || requested != null) return;
                    Log.w(TAG, "Player error for " + failed.id(), error);
                    if (manual) {
                        wantsPlay = false;
                        setStatus("«" + failed.title() + "» не удалось воспроизвести");
                    } else {
                        advance(false);
                    }
                });
            }
        });

        Intent open = new Intent(this, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent content = PendingIntent.getActivity(this, 0, open,
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        setMediaNotificationProvider(new DefaultMediaNotificationProvider.Builder(this)
                .setChannelName(R.string.app_name).build());
        session = new MediaSession.Builder(this, new SessionPlayer(audio))
                .setSessionActivity(content)
                .setCallback(new Commands())
                .build();
        refreshQueue();
    }

    @Override
    public MediaSession onGetSession(@NonNull MediaSession.ControllerInfo controllerInfo) {
        return session;
    }

    @Override
    public void onDestroy() {
        closed = true;
        main.removeCallbacksAndMessages(null);
        cancelRequest();
        prefetch.retainJobs(Set.of(), null);
        immediate.shutdownNow();
        preload.shutdownNow();
        remote.shutdownNow();
        search.shutdownNow();
        if (session != null) {
            session.release();
            session = null;
        }
        if (audio != null) {
            audio.release();
            audio = null;
        }
        Thread cleanup = new Thread(() -> {
            try {
                sources.close();
            } catch (Exception error) {
                Log.w(TAG, "Torrent shutdown failed", error);
            }
        }, "radiowave-cleanup");
        cleanup.start();
        super.onDestroy();
    }

    private final class SessionPlayer extends ForwardingSimpleBasePlayer {
        SessionPlayer(Player player) {
            super(player);
        }

        @Override
        protected State getState() {
            State state = super.getState();
            Player.Commands commands = state.availableCommands.buildUpon().addAll(COMMAND_PLAY_PAUSE,
                    COMMAND_SEEK_TO_NEXT, COMMAND_SEEK_TO_NEXT_MEDIA_ITEM,
                    COMMAND_SEEK_TO_PREVIOUS, COMMAND_SEEK_TO_PREVIOUS_MEDIA_ITEM)
                    .remove(COMMAND_CHANGE_MEDIA_ITEMS)
                    .remove(COMMAND_SET_MEDIA_ITEM)
                    .remove(COMMAND_SET_REPEAT_MODE)
                    .remove(COMMAND_SET_SHUFFLE_MODE)
                    .build();
            return state.buildUpon().setAvailableCommands(commands).build();
        }

        @Override
        protected ListenableFuture<?> handleSetPlayWhenReady(boolean playWhenReady) {
            if (playWhenReady) start();
            else suspendPlayback();
            return Futures.immediateVoidFuture();
        }

        @Override
        protected ListenableFuture<?> handleSeek(int mediaItemIndex, long positionMs, int seekCommand) {
            switch (seekCommand) {
                case COMMAND_SEEK_TO_NEXT, COMMAND_SEEK_TO_NEXT_MEDIA_ITEM -> advance(true);
                case COMMAND_SEEK_TO_PREVIOUS, COMMAND_SEEK_TO_PREVIOUS_MEDIA_ITEM -> previous();
                default -> { return super.handleSeek(mediaItemIndex, positionMs, seekCommand); }
            }
            return Futures.immediateVoidFuture();
        }

        @Override
        protected ListenableFuture<?> handleStop() {
            wantsPlay = false;
            waitingForNext = false;
            cancelRequest();
            setStatus("");
            return super.handleStop();
        }
    }

    private final class Commands implements MediaSession.Callback {
        @Override
        @SuppressWarnings("deprecation")
        public boolean onMediaButtonEvent(MediaSession mediaSession, MediaSession.ControllerInfo controller, Intent intent) {
            KeyEvent event = intent.getParcelableExtra(Intent.EXTRA_KEY_EVENT);
            if ((requested != null || waitingForNext) && event != null
                    && (event.getKeyCode() == KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE
                    || event.getKeyCode() == KeyEvent.KEYCODE_HEADSETHOOK)) {
                if (event.getAction() == KeyEvent.ACTION_DOWN && event.getRepeatCount() == 0) {
                    if (wantsPlay) suspendPlayback();
                    else start();
                }
                return true;
            }
            return MediaSession.Callback.super.onMediaButtonEvent(mediaSession, controller, intent);
        }

        @NonNull
        @Override
        public MediaSession.ConnectionResult onConnect(
                @NonNull MediaSession mediaSession, @NonNull MediaSession.ControllerInfo controller) {
            if (!isOwn(controller)) return MediaSession.Callback.super.onConnect(mediaSession, controller);
            SessionCommands.Builder commands = MediaSession.ConnectionResult.DEFAULT_SESSION_COMMANDS.buildUpon();
            PhoneCommands.CLIENT_COMMANDS.forEach(commands::add);
            return new MediaSession.ConnectionResult.AcceptedResultBuilder(mediaSession)
                    .setAvailableSessionCommands(commands.build()).build();
        }

        @NonNull
        @Override
        public ListenableFuture<SessionResult> onCustomCommand(
                @NonNull MediaSession mediaSession,
                @NonNull MediaSession.ControllerInfo controller,
                @NonNull SessionCommand command,
                @NonNull Bundle args) {
            if (!isOwn(controller)) return Futures.immediateFuture(new SessionResult(SessionError.ERROR_PERMISSION_DENIED));
            String text = args.getString("value", "");
            if (command.equals(PhoneCommands.STATE)) return result(snapshot());
            if (command.equals(PhoneCommands.COLLECT)) return result(PhoneCommands.rows(firstRows(tracks.all())));
            if (command.equals(PhoneCommands.HISTORY)) {
                Bundle data = new Bundle();
                data.putStringArrayList("history", new ArrayList<>(tracks.searchHistory()));
                return result(data);
            }
            if (command.equals(PhoneCommands.PLAY_TRACK)) {
                Track selected = tracks.get(text);
                if (selected == null) return error("Трек больше недоступен");
                open(selected, true, true);
                refreshQueue();
                return result(snapshot());
            }
            if (command.equals(PhoneCommands.RATE)) {
                if (current != null && heard && requested == null) {
                    int rating = args.getInt("value");
                    int value = rating == wave.rating(current) ? 0 : rating;
                    wave.rate(current, value);
                    if (value < 0) advance(false);
                    else refreshQueue();
                }
                return result(snapshot());
            }
            if (command.equals(PhoneCommands.SEARCH)) {
                if (text.trim().isEmpty()) return result(new Bundle());
                return run(search, () -> {
                    tracks.rememberSearch(text);
                    List<Track> local = SearchMatches.inCatalogue(text, tracks.all());
                    List<Track> online;
                    try {
                        online = sources.search(text);
                    } catch (IOException failure) {
                        if (local.isEmpty()) throw failure;
                        Log.w(TAG, "Online lookup failed; keeping catalogue matches", failure);
                        online = List.of();
                    }
                    return PhoneCommands.rows(firstRows(SearchMatches.merge(local, online)));
                });
            }
            if (command.equals(PhoneCommands.LOCAL)) {
                List<String> uris = args.getStringArrayList("uris");
                if (uris == null || uris.isEmpty()) return error("Не выбран файл");
                return run(remote, () -> {
                    ArrayList<Track> added = new ArrayList<>();
                    for (String uri : uris) {
                        Track track = tracks.addLocal(Uri.parse(uri));
                        if (track != null) added.add(track);
                    }
                    if (added.isEmpty()) throw new IOException("Не удалось прочитать аудио");
                    return imported(added);
                });
            }
            if (command.equals(PhoneCommands.MAGNET)) return run(remote, () -> imported(sources.importMagnet(text)));
            if (command.equals(PhoneCommands.TORRENT)) return run(remote, () -> imported(sources.importTorrent(Uri.parse(text))));
            return Futures.immediateFuture(new SessionResult(SessionError.ERROR_NOT_SUPPORTED));
        }
    }

    private boolean isOwn(MediaSession.ControllerInfo controller) {
        return getPackageName().equals(controller.getPackageName()) && controller.getUid() == getApplicationInfo().uid;
    }

    private Bundle imported(List<Track> items) throws IOException {
        if (items.isEmpty()) throw new IOException("В источнике не найдено аудио");
        main.post(() -> {
            if (closed) return;
            setStatus("Добавлено треков: " + items.size());
            refreshQueue();
            if (current == null && requested == null && wantsPlay) advance(false);
        });
        return PhoneCommands.rows(firstRows(items));
    }

    private List<Track> firstRows(List<Track> items) {
        return items.subList(0, Math.min(items.size(), 120));
    }

    private ListenableFuture<SessionResult> result(Bundle data) {
        return Futures.immediateFuture(new SessionResult(SessionResult.RESULT_SUCCESS, data));
    }

    private ListenableFuture<SessionResult> error(String error) {
        return Futures.immediateFuture(new SessionResult(SessionError.ERROR_UNKNOWN, PhoneCommands.text(error)));
    }

    private ListenableFuture<SessionResult> run(ExecutorService executor, Callable<Bundle> task) {
        SettableFuture<SessionResult> response = SettableFuture.create();
        Future<?> running = executor.submit(() -> {
            try {
                response.set(new SessionResult(SessionResult.RESULT_SUCCESS, task.call()));
            } catch (Exception error) {
                Log.w(TAG, "Request failed", error);
                String description = error instanceof IOException ? error.getLocalizedMessage() : null;
                response.set(new SessionResult(SessionError.ERROR_UNKNOWN,
                        PhoneCommands.text(description == null ? "Не удалось обработать запрос" : description)));
            }
        });
        response.addListener(() -> {
            if (response.isCancelled()) running.cancel(true);
        }, Runnable::run);
        return response;
    }

    private void start() {
        if (closed) return;
        wantsPlay = true;
        if (requested != null) {
            maintainCache();
            setStatus("«" + requested.title() + "» · загружаем трек");
            return;
        }
        if (discovering && waitingForNext) {
            setStatus("Ищем музыку для волны…");
            publish();
            return;
        }
        if (current == null || audio.getPlayerError() != null || audio.getPlaybackState() == Player.STATE_ENDED) {
            succession = 0;
            advance(false);
        } else {
            if (audio.getPlaybackState() == Player.STATE_IDLE) audio.prepare();
            audio.play();
            maintainCache();
            setStatus("");
        }
    }

    private void suspendPlayback() {
        if (closed) return;
        wantsPlay = false;
        audio.pause();
        setStatus(requested != null ? "Трек загружается · воспроизведение на паузе" : "");
    }

    private void advance(boolean manual) {
        if (closed) return;
        if (manual) {
            succession = 0;
            long duration = audio.getDuration();
            if (requested == null && current != null && heard && duration > 0
                    && totalAudible() < duration / 4) wave.earlySkip(current);
        }
        Track abandoned = requested;
        cancelRequest();
        if (abandoned != null) {
            unavailable.put(abandoned.id(), SystemClock.elapsedRealtime() + RETRY_SOURCE_MS);
            queue.removeIf(track -> track.id().equals(abandoned.id()));
        }
        wantsPlay = true;
        if (succession++ >= WINDOW) {
            waitingForNext = false;
            wantsPlay = false;
            audio.pause();
            setStatus("Треки сейчас недоступны. Нажмите «Пуск», чтобы повторить");
            return;
        }
        queue.removeIf(track -> unavailable.getOrDefault(track.id(), 0L) > SystemClock.elapsedRealtime());
        if (queue.isEmpty()) refreshQueue();
        if (!queue.isEmpty()) {
            Track next = queue.remove(0);
            open(next, false, true);
            refreshQueue();
            return;
        }
        waitingForNext = true;
        audio.pause();
        setStatus("Ищем музыку для волны…");
        startDiscovery(true);
    }

    private void previous() {
        if (closed) return;
        Track target = past.pollLast();
        if (target == null) {
            if (current != null && requested == null && audio.isCurrentMediaItemSeekable()) audio.seekTo(0);
            return;
        }
        if (current != null) {
            Track interrupted = current;
            queue.removeIf(track -> track.id().equals(interrupted.id()));
            queue.add(0, interrupted);
        }
        open(target, false, false);
        refreshQueue();
    }

    private void open(Track track, boolean manuallySelected, boolean keepHistory) {
        cancelRequest();
        if (current != null && heard && keepHistory && !current.id().equals(track.id())) {
            past.addLast(current);
            while (past.size() > 40) past.removeFirst();
        }
        current = null;
        heard = false;
        audibleMs = 0;
        audioStartedAt = 0;
        exact = manuallySelected;
        waitingForNext = false;
        if (manuallySelected) succession = 0;
        wantsPlay = true;
        requested = track;
        queue.removeIf(item -> item.id().equals(track.id()));
        audio.pause();
        setStatus("«" + track.title() + "» · " + (track.isLocal() ? "открываем" : "загружаем трек"));
        int generation = request;
        Uri uri = ready.get(track.id());
        if (uri == null && track.isLocal()) uri = Uri.parse(track.uri());
        if (uri != null) {
            beginAudio(track, uri, generation);
            return;
        }
        // A foreground resolver joins the same torrent transfer at higher priority.
        // Leave its prefetch waiter attached so promotion cannot discard staged pieces.
        pending = immediate.submit(() -> {
            try {
                Uri resolved = sources.resolve(track, () -> Thread.currentThread().isInterrupted(), true);
                if (resolved == null) throw new IOException("Нет доступной раздачи");
                main.post(() -> beginAudio(track, resolved, generation));
            } catch (Exception failure) {
                main.post(() -> loadFailed(track, generation, failure));
            }
        });
    }

    private void beginAudio(Track selected, Uri uri, int generation) {
        if (closed || generation != request) return;
        Track updated = tracks.get(selected.id());
        Track track = updated == null ? selected : updated;
        pending = null;
        requested = null;
        ready.put(track.id(), uri);
        current = track;
        playerItems.clear();
        playerItems.add(track.id());
        MediaMetadata metadata = new MediaMetadata.Builder().setTitle(track.title())
                .setArtist(track.artist()).setGenre(track.genre()).build();
        MediaItem item = new MediaItem.Builder().setMediaId(track.id()).setUri(uri)
                .setMediaMetadata(metadata).build();
        audio.setMediaItem(item);
        audio.prepare();
        audio.setPlayWhenReady(wantsPlay);
        setStatus("");
        refreshQueue();
    }

    private void loadFailed(Track track, int generation, Exception failure) {
        if (closed || generation != request) return;
        pending = null;
        requested = null;
        Log.w(TAG, "Audio unavailable " + track.id(), failure);
        unavailable.put(track.id(), SystemClock.elapsedRealtime() + RETRY_SOURCE_MS);
        if (exact) {
            wantsPlay = false;
            playerItems.clear();
            audio.clearMediaItems();
            setStatus("«" + track.title() + "» · нет доступного файла");
        } else if (wantsPlay) {
            refreshQueue();
            advance(false);
        } else {
            setStatus("«" + track.title() + "» · нет доступного файла");
        }
    }

    private void cancelRequest() {
        request++;
        if (pending != null) pending.cancel(true);
        pending = null;
        requested = null;
    }

    private void refreshQueue() {
        unavailable.entrySet().removeIf(entry -> entry.getValue() <= SystemClock.elapsedRealtime());
        Set<String> excluded = new HashSet<>(unavailable.keySet());
        Track active = requested == null ? current : requested;
        if (active != null) excluded.add(active.id());
        int count = Math.min(VISIBLE, queue.size());
        List<Track> prefix = new ArrayList<>(queue.subList(0, count));
        queue.clear();
        queue.addAll(wave.fill(prefix, excluded, active, WINDOW));
        maintainCache();
        if (wantsPlay && (queue.size() < 10 || artists(queue, VISIBLE).size() < 4)) startDiscovery(false);
        publish();
    }

    private void maintainCache() {
        Set<String> top = new LinkedHashSet<>();
        queue.stream().limit(PREFETCH).forEach(track -> top.add(track.id()));
        prefetch.retainJobs(top, requested == null ? null : requested.id());
        Set<String> keep = new HashSet<>(keepPlayed);
        keep.addAll(top);
        keep.addAll(playerItems);
        if (requested != null) keep.add(requested.id());
        ready.keySet().retainAll(keep);
        main.removeCallbacks(retryPrefetch);
        if (!wantsPlay || (current == null && requested == null)) return;
        long now = SystemClock.elapsedRealtime();
        long nextRetry = prefetch.nextRetry(top, now);
        for (int index = 0; index < Math.min(PREFETCH, queue.size()); index++) {
            Track track = queue.get(index);
            if (!track.isLocal() && !ready.containsKey(track.id()) && !unavailable.containsKey(track.id())
                    && !prefetch.jobs.containsKey(track.id()) && !prefetch.retryAt.containsKey(track.id())) {
                enqueuePrefetch(track);
            }
        }
        if (nextRetry != Long.MAX_VALUE) main.postDelayed(retryPrefetch, nextRetry - now);
        // Coalesce eviction work on the main thread so it never queues ahead of audio.
        main.removeCallbacks(trimCache);
        main.postDelayed(trimCache, 1_000);
    }

    private final Runnable retryPrefetch = this::maintainCache;

    private final Runnable trimCache = () -> {
        if (closed) return;
        Set<String> keep = new HashSet<>(keepPlayed);
        keep.addAll(playerItems);
        queue.stream().limit(PREFETCH).forEach(track -> keep.add(track.id()));
        if (requested != null) keep.add(requested.id());
        remote.submit(() -> {
            try {
                sources.retain(keep);
            } catch (Exception error) {
                Log.w(TAG, "Could not clear the cache", error);
            }
        });
    };

    private void enqueuePrefetch(Track track) {
        Prefetch job = new Prefetch();
        prefetch.jobs.put(track.id(), job);
        job.work = preload.submit(() -> {
            try {
                Uri resolved = sources.resolve(track, () -> Thread.currentThread().isInterrupted());
                if (!Thread.currentThread().isInterrupted()) main.post(() -> {
                    if (closed || !prefetch.jobs.remove(track.id(), job)) return;
                    if (resolved != null) {
                        if (requested != null && requested.id().equals(track.id())) {
                            Track selected = requested;
                            cancelRequest();
                            beginAudio(selected, resolved, request);
                            return;
                        }
                        ready.put(track.id(), resolved);
                    }
                    publish();
                });
            } catch (Exception failure) {
                if (Thread.currentThread().isInterrupted()) return;
                main.post(() -> {
                    if (closed || !prefetch.jobs.remove(track.id(), job)) return;
                    prefetch.retryAt.put(track.id(), SystemClock.elapsedRealtime() + RETRY_SOURCE_MS);
                    Log.d(TAG, "Prefetch unavailable " + track.id(), failure);
                    maintainCache();
                });
            }
        });
    }

    private void startDiscovery(boolean foreground) {
        if (closed || discovering || refreshPending) return;
        discovering = true;
        if (foreground) setStatus("Ищем музыку для волны…");
        boolean foreign = wave.preferForeign();
        Set<String> avoid = new HashSet<>(artists(queue, WINDOW));
        if (current != null) avoid.add(current.artist());
        if (requested != null) avoid.add(requested.artist());
        List<String> genres = wave.discoveryGenres();
        remote.submit(() -> {
            List<Track> found = List.of();
            try {
                found = sources.discover(foreign, avoid, genres);
                if (found.isEmpty() && foreground && !Thread.currentThread().isInterrupted()) {
                    found = sources.discover(!foreign, avoid, genres);
                }
                if (!found.isEmpty()) tracks.addAll(found);
            } catch (Exception error) {
                Log.w(TAG, "Wave discovery failed", error);
            }
            boolean empty = found.isEmpty();
            main.post(() -> {
                if (closed) return;
                discovering = false;
                refreshPending = true;
                refreshQueue();
                if (wantsPlay && waitingForNext && requested == null) {
                    if (!queue.isEmpty()) advance(false);
                    else {
                        waitingForNext = false;
                        wantsPlay = false;
                        setStatus("Не удалось найти доступные треки. Можно попробовать поиск");
                    }
                } else if (empty && wantsPlay && audio.getPlaybackState() == Player.STATE_ENDED) {
                    setStatus("Нет доступного следующего трека");
                }
                main.postDelayed(() -> {
                    refreshPending = false;
                    if (wantsPlay && waitingForNext) startDiscovery(true);
                    else if (wantsPlay && (current != null || requested != null)
                            && (queue.size() < 10 || artists(queue, VISIBLE).size() < 4)) startDiscovery(false);
                }, DISCOVERY_RETRY_MS);
            });
        });
    }

    private Set<String> artists(List<Track> items, int limit) {
        Set<String> names = new HashSet<>();
        items.stream().limit(limit).map(Track::artist).filter(name -> !name.trim().isEmpty()).forEach(names::add);
        return names;
    }

    private void markAudible(boolean playing) {
        long now = SystemClock.elapsedRealtime();
        if (audioStartedAt != 0) audibleMs += now - audioStartedAt;
        audioStartedAt = playing ? now : 0;
    }

    private long totalAudible() {
        return audibleMs + (audioStartedAt == 0 ? 0 : SystemClock.elapsedRealtime() - audioStartedAt);
    }

    private void setStatus(String value) {
        status = value;
        publish();
    }

    private Bundle snapshot() {
        int end = Math.min(VISIBLE, queue.size());
        Bundle data = PhoneCommands.rows(queue.subList(0, end));
        List<Bundle> rows = PhoneCommands.rows(data);
        for (int i = 0; i < end; i++) {
            Track track = queue.get(i);
            rows.get(i).putBoolean("ready", track.isLocal() || ready.containsKey(track.id()));
        }
        data.putBundle("current", PhoneCommands.track(requested == null ? current : requested));
        data.putString("status", status);
        data.putBoolean("requested", requested != null || waitingForNext);
        data.putBoolean("wantsPlay", wantsPlay);
        data.putInt("rating", current == null ? 0 : wave.rating(current));
        data.putBoolean("rated", heard && current != null && requested == null);
        return data;
    }

    private void publish() {
        if (closed || session == null) return;
        Bundle state = snapshot();
        // Includes only four visible rows and UI properties, independent of playback position.
        String key = state.toString();
        if (key.equals(lastSnapshot)) return;
        lastSnapshot = key;
        for (MediaSession.ControllerInfo controller : session.getConnectedControllers()) {
            if (isOwn(controller)) session.sendCustomCommand(controller, PhoneCommands.UPDATE, state);
        }
    }
}
