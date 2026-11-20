package org.radiowave.android;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ClipData;
import android.content.ComponentName;
import android.content.Intent;
import android.content.res.ColorStateList;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.view.WindowInsets;
import android.view.inputmethod.EditorInfo;
import android.view.inputmethod.InputMethodManager;
import android.widget.ArrayAdapter;
import android.widget.AutoCompleteTextView;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ListView;
import android.widget.ScrollView;
import android.widget.SeekBar;
import android.widget.TextView;
import android.widget.Toast;

import androidx.media3.common.C;
import androidx.media3.common.Player;
import androidx.media3.common.util.UnstableApi;
import androidx.media3.session.MediaController;
import androidx.media3.session.SessionCommand;
import androidx.media3.session.SessionResult;
import androidx.media3.session.SessionToken;

import com.google.common.util.concurrent.Futures;
import com.google.common.util.concurrent.ListenableFuture;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.function.Consumer;

@UnstableApi
public final class MainActivity extends Activity {
    private static final int AUDIO = 1;
    private static final int TORRENT = 2;
    private static final int BASE = Color.rgb(21, 19, 33);
    private static final int PANEL = Color.rgb(38, 32, 55);
    private static final int INK = Color.rgb(246, 239, 255);
    private static final int SUBTLE = Color.rgb(190, 177, 207);
    private static final int ACCENT = Color.rgb(212, 188, 255);

    private final Handler main = new Handler(Looper.getMainLooper());
    private ListenableFuture<MediaController> connection;
    private MediaController controller;
    private Bundle state = new Bundle();
    private TextView title;
    private TextView artist;
    private TextView status;
    private TextView progress;
    private TextView duration;
    private SeekBar seek;
    private Button play;
    private Button previous;
    private Button next;
    private Button like;
    private Button dislike;
    private LinearLayout queue;
    private String queueKey = "";
    private boolean dragging;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        if (android.os.Build.VERSION.SDK_INT >= 30) getWindow().setDecorFitsSystemWindows(false);
        createScreen();
        SessionToken token = new SessionToken(this, new ComponentName(this, PlaybackService.class));
        connection = new MediaController.Builder(this, token)
                .setListener(new MediaController.Listener() {
                    @Override
                    public ListenableFuture<SessionResult> onCustomCommand(
                            MediaController player, SessionCommand command, Bundle args) {
                        if (command.equals(PhoneCommands.UPDATE)) render(args);
                        return Futures.immediateFuture(new SessionResult(SessionResult.RESULT_SUCCESS));
                    }
                })
                .buildAsync();
        connection.addListener(() -> {
            if (isDestroyed()) return;
            try {
                controller = connection.get();
                controller.addListener(new Player.Listener() {
                    @Override
                    public void onEvents(Player player, Player.Events events) {
                        renderPlayer();
                    }
                });
                send(PhoneCommands.STATE, Bundle.EMPTY, MainActivity.this::render);
                renderPlayer();
            } catch (Exception error) {
                status.setText("Не удалось запустить плеер");
            }
        }, getMainExecutorCompat());
    }

    @Override
    protected void onStart() {
        super.onStart();
        main.post(clock);
    }

    @Override
    protected void onStop() {
        main.removeCallbacks(clock);
        super.onStop();
    }

    @Override
    protected void onDestroy() {
        main.removeCallbacksAndMessages(null);
        if (connection != null) MediaController.releaseFuture(connection);
        controller = null;
        super.onDestroy();
    }

    private java.util.concurrent.Executor getMainExecutorCompat() {
        return command -> main.post(command);
    }

    private final Runnable clock = new Runnable() {
        @Override
        public void run() {
            renderPosition();
            main.postDelayed(this, 500);
        }
    };

    private void createScreen() {
        LinearLayout root = column();
        root.setBackgroundColor(BASE);
        root.setOnApplyWindowInsetsListener((view, insets) -> {
            if (android.os.Build.VERSION.SDK_INT >= 30) {
                android.graphics.Insets safe = insets.getInsets(WindowInsets.Type.systemBars()
                        | WindowInsets.Type.displayCutout());
                view.setPadding(safe.left, safe.top, safe.right, safe.bottom);
                return WindowInsets.CONSUMED;
            }
            return insets;
        });
        setContentView(root);

        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        root.addView(scroll, new LinearLayout.LayoutParams(-1, 0, 1));

        LinearLayout page = column();
        page.setPadding(dp(18), dp(14), dp(18), dp(24));
        scroll.addView(page, new ScrollView.LayoutParams(-1, -2));

        LinearLayout toolbar = row();
        TextView name = label("RadioWave", 22, INK, true);
        toolbar.addView(name, new LinearLayout.LayoutParams(0, -2, 1));
        Button searchButton = button("Поиск", () -> showSearch());
        toolbar.addView(searchButton, new LinearLayout.LayoutParams(-2, dp(48)));
        Button sources = button("Источники", () -> showSources());
        LinearLayout.LayoutParams sourceParams = new LinearLayout.LayoutParams(-2, dp(48));
        sourceParams.leftMargin = dp(6);
        toolbar.addView(sources, sourceParams);
        page.addView(toolbar);

        LinearLayout playing = column();
        playing.setMinimumHeight(dp(156));
        playing.setGravity(Gravity.CENTER_VERTICAL);
        playing.setPadding(dp(18), dp(16), dp(18), dp(16));
        playing.setBackground(panel(PANEL, 22));
        LinearLayout.LayoutParams playingParams = new LinearLayout.LayoutParams(-1, -2);
        playingParams.topMargin = dp(12);
        page.addView(playing, playingParams);
        artist = label("", 15, ACCENT, true);
        playing.addView(artist);
        title = label("Моя волна", 28, INK, true);
        title.setMaxLines(3);
        playing.addView(title);
        status = label("Нажмите «Пуск», чтобы включить волну", 14, SUBTLE, false);
        status.setPadding(0, dp(10), 0, 0);
        playing.addView(status);

        LinearLayout heading = row();
        TextView more = label("Дальше", 20, INK, true);
        heading.addView(more, new LinearLayout.LayoutParams(0, -2, 1));
        heading.addView(button("Коллекция", () -> showCollection()), new LinearLayout.LayoutParams(-2, dp(48)));
        LinearLayout.LayoutParams headingParams = new LinearLayout.LayoutParams(-1, -2);
        headingParams.topMargin = dp(10);
        page.addView(heading, headingParams);
        queue = column();
        page.addView(queue);

        LinearLayout dock = column();
        dock.setBackgroundColor(PANEL);
        dock.setPadding(dp(12), dp(4), dp(12), dp(10));
        root.addView(dock);
        LinearLayout time = row();
        progress = label("0:00", 12, SUBTLE, false);
        duration = label("0:00", 12, SUBTLE, false);
        time.addView(progress);
        seek = new SeekBar(this);
        seek.setMax(1000);
        seek.setContentDescription("Позиция воспроизведения");
        seek.setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener() {
            @Override public void onProgressChanged(SeekBar bar, int value, boolean user) {
                if (user && controller != null && controller.getDuration() > 0) {
                    progress.setText(format(controller.getDuration() * value / bar.getMax()));
                }
            }

            @Override public void onStartTrackingTouch(SeekBar bar) { dragging = true; }

            @Override public void onStopTrackingTouch(SeekBar bar) {
                dragging = false;
                if (controller != null && controller.isCommandAvailable(Player.COMMAND_SEEK_IN_CURRENT_MEDIA_ITEM)
                        && controller.getDuration() > 0) {
                    controller.seekTo(controller.getDuration() * bar.getProgress() / bar.getMax());
                }
            }
        });
        time.addView(seek, new LinearLayout.LayoutParams(0, dp(40), 1));
        time.addView(duration);
        dock.addView(time);

        LinearLayout keys = row();
        like = transport("♡", "Лайк", () -> rate(1));
        previous = transport("⏮", "Назад", () -> {
            if (controller != null && controller.isCommandAvailable(Player.COMMAND_SEEK_TO_PREVIOUS)) {
                controller.seekToPrevious();
            }
        });
        play = transport("▶", "Пуск", () -> {
            if (controller == null) return;
            if ((state.getBoolean("requested") && state.getBoolean("wantsPlay")) || controller.getPlayWhenReady()) {
                controller.pause();
            } else {
                controller.play();
            }
        });
        play.setBackground(panel(ACCENT, 16));
        play.setTextColor(BASE);
        next = transport("⏭", "Дальше", () -> {
            if (controller != null && controller.isCommandAvailable(Player.COMMAND_SEEK_TO_NEXT)) {
                controller.seekToNext();
            }
        });
        dislike = transport("⊘", "Дизлайк", () -> rate(-1));
        for (Button key : List.of(like, previous, play, next, dislike)) {
            LinearLayout.LayoutParams keyParams = new LinearLayout.LayoutParams(0, dp(48), 1);
            keyParams.leftMargin = dp(3);
            keyParams.rightMargin = dp(3);
            keys.addView(key, keyParams);
        }
        dock.addView(keys);
        render(state);
    }

    private void render(Bundle update) {
        if (isDestroyed()) return;
        state = update;
        Bundle track = update.getBundle("current");
        artist.setText(track == null ? "" : track.getString("artist", ""));
        artist.setVisibility(artist.length() == 0 ? View.GONE : View.VISIBLE);
        title.setText(track == null ? "Моя волна" : track.getString("title", "Моя волна"));
        String info = update.getString("status", "");
        if (info.isEmpty() && track != null) info = details(track);
        status.setText(info);
        status.setVisibility(info.isEmpty() ? View.GONE : View.VISIBLE);
        List<Bundle> items = PhoneCommands.rows(update);
        StringBuilder key = new StringBuilder();
        for (Bundle item : items) {
            key.append(item.getString("id", "")).append('\n')
                    .append(display(item)).append('\n')
                    .append(details(item)).append(item.getBoolean("ready")).append('\n');
        }
        if (!key.toString().equals(queueKey)) {
            queueKey = key.toString();
            queue.removeAllViews();
            if (items.isEmpty()) {
                TextView empty = label("Здесь появятся следующие треки", 14, SUBTLE, false);
                empty.setPadding(dp(4), dp(10), dp(4), dp(10));
                queue.addView(empty);
            }
            for (Bundle item : items) {
                LinearLayout itemView = column();
                itemView.setMinimumHeight(dp(57));
                itemView.setPadding(dp(12), dp(9), dp(12), dp(9));
                itemView.setGravity(Gravity.CENTER_VERTICAL);
                itemView.setBackground(panel(PANEL, 12));
                itemView.addView(label(display(item), 15, INK, true));
                String meta = details(item);
                if (item.getBoolean("ready")) meta = meta.isEmpty() ? "Готов" : meta + " · Готов";
                if (!meta.isEmpty()) itemView.addView(label(meta, 12, SUBTLE, false));
                itemView.setFocusable(true);
                itemView.setOnClickListener(view -> select(item));
                LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, -2);
                params.topMargin = dp(6);
                queue.addView(itemView, params);
            }
        }
        renderPlayer();
    }

    private void renderPlayer() {
        boolean connected = controller != null;
        boolean pending = state.getBoolean("requested");
        boolean pause = (pending && state.getBoolean("wantsPlay")) || (connected && controller.getPlayWhenReady());
        play.setText(pause ? "Ⅱ" : "▶");
        play.setContentDescription(pause ? "Пауза" : "Пуск");
        play.setEnabled(connected);
        previous.setEnabled(connected && controller.isCommandAvailable(Player.COMMAND_SEEK_TO_PREVIOUS));
        next.setEnabled(connected && controller.isCommandAvailable(Player.COMMAND_SEEK_TO_NEXT));
        int rating = state.getInt("rating");
        like.setText(rating > 0 ? "♥" : "♡");
        like.setContentDescription(rating > 0 ? "Снять лайк" : "Лайк");
        like.setEnabled(connected && state.getBoolean("rated"));
        dislike.setTextColor(rating < 0 ? ACCENT : INK);
        dislike.setContentDescription(rating < 0 ? "Снять дизлайк" : "Дизлайк");
        dislike.setEnabled(connected && state.getBoolean("rated"));
        renderPosition();
    }

    private void renderPosition() {
        boolean valid = controller != null && !state.getBoolean("requested");
        long total = valid ? controller.getDuration() : C.TIME_UNSET;
        seek.setEnabled(valid && total > 0 && controller.isCommandAvailable(Player.COMMAND_SEEK_IN_CURRENT_MEDIA_ITEM));
        if (dragging) return;
        long position = valid && total > 0 ? controller.getCurrentPosition() : 0;
        progress.setText(format(position));
        duration.setText(format(total));
        seek.setProgress(total > 0 ? (int) Math.min(seek.getMax(), Math.round(position * (double) seek.getMax() / total)) : 0);
    }

    private String format(long milliseconds) {
        long seconds = Math.max(0, milliseconds / 1000);
        return String.format(Locale.ROOT, "%d:%02d", seconds / 60, seconds % 60);
    }

    private void rate(int value) {
        Bundle args = new Bundle();
        args.putInt("value", value);
        send(PhoneCommands.RATE, args, this::render);
    }

    private void select(Bundle row) {
        send(PhoneCommands.PLAY_TRACK, PhoneCommands.text(row.getString("id", "")), this::render);
    }

    private void showSearch() {
        if (controller == null) return;
        LinearLayout body = column();
        body.setPadding(dp(18), dp(4), dp(18), dp(8));
        AutoCompleteTextView input = new AutoCompleteTextView(this);
        input.setSingleLine(true);
        input.setThreshold(1);
        input.setHint("Трек или исполнитель");
        input.setImeOptions(EditorInfo.IME_ACTION_SEARCH);
        body.addView(input, new LinearLayout.LayoutParams(-1, dp(52)));
        TextView feedback = label("", 14, SUBTLE, false);
        body.addView(feedback);
        ListView matches = new ListView(this);
        body.addView(matches, new LinearLayout.LayoutParams(-1, dp(260)));
        AlertDialog dialog = new AlertDialog.Builder(this).setTitle("Поиск").setView(body)
                .setNeutralButton("Закрыть", null).setPositiveButton("Найти", null).create();
        ArrayAdapter<String> suggestions = new ArrayAdapter<>(this, android.R.layout.simple_dropdown_item_1line,
                new ArrayList<>());
        input.setAdapter(suggestions);
        final List<Bundle> rows = new ArrayList<>();
        final int[] serial = {0};
        Runnable search = () -> {
            String text = input.getText().toString().trim();
            if (text.isEmpty()) return;
            int request = ++serial[0];
            rows.clear();
            matches.setAdapter(null);
            feedback.setText("Ищем…");
            input.dismissDropDown();
            ((InputMethodManager) getSystemService(INPUT_METHOD_SERVICE)).hideSoftInputFromWindow(input.getWindowToken(), 0);
            send(PhoneCommands.SEARCH, PhoneCommands.text(text), response -> {
                if (!dialog.isShowing() || request != serial[0]) return;
                rows.addAll(PhoneCommands.rows(response));
                feedback.setText(rows.isEmpty() ? "Треки не найдены" : "");
                bindRows(matches, rows, item -> {
                    dialog.dismiss();
                    select(item);
                });
            }, message -> {
                if (dialog.isShowing() && request == serial[0]) feedback.setText(message);
            });
        };
        dialog.setOnShowListener(ignored -> {
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(view -> search.run());
            input.setOnEditorActionListener((view, action, event) -> {
                if (action != EditorInfo.IME_ACTION_SEARCH) return false;
                search.run();
                return true;
            });
            input.setOnItemClickListener((parent, view, position, id) -> search.run());
            send(PhoneCommands.HISTORY, Bundle.EMPTY, response -> {
                ArrayList<String> entries = response.getStringArrayList("history");
                if (entries != null) {
                    suggestions.addAll(entries);
                    if (input.enoughToFilter()) {
                        suggestions.getFilter().filter(input.getText(), count -> {
                            if (count > 0 && input.hasFocus() && dialog.isShowing()) input.showDropDown();
                        });
                    }
                }
            });
        });
        dialog.show();
    }

    private void showCollection() {
        if (controller == null) return;
        send(PhoneCommands.COLLECT, Bundle.EMPTY, response -> {
            List<Bundle> rows = PhoneCommands.rows(response);
            if (rows.isEmpty()) {
                new AlertDialog.Builder(this).setTitle("Коллекция")
                        .setMessage("Включите волну или добавьте аудио в разделе «Источники».")
                        .setPositiveButton("ОК", null).show();
                return;
            }
            ListView items = new ListView(this);
            AlertDialog dialog = new AlertDialog.Builder(this).setTitle("Коллекция")
                    .setView(items).setNegativeButton("Закрыть", null).create();
            bindRows(items, rows, selected -> {
                dialog.dismiss();
                select(selected);
            });
            dialog.show();
        });
    }

    private void bindRows(ListView view, List<Bundle> rows, Consumer<Bundle> onClick) {
        List<String> names = new ArrayList<>();
        for (Bundle row : rows) names.add(display(row));
        view.setAdapter(new ArrayAdapter<>(this, android.R.layout.simple_list_item_1, names));
        view.setOnItemClickListener((parent, item, position, id) -> onClick.accept(rows.get(position)));
    }

    private void showSources() {
        if (controller == null) return;
        String[] actions = {"Добавить аудио", "Импортировать .torrent", "Добавить magnet-ссылку"};
        new AlertDialog.Builder(this).setTitle("Источники").setItems(actions, (dialog, which) -> {
            if (which == 2) showMagnet();
            else openPicker(which == 0 ? AUDIO : TORRENT);
        }).show();
    }

    private void openPicker(int request) {
        Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE)
                .setType(request == AUDIO ? "audio/*" : "*/*")
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION);
        if (request == AUDIO) intent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
        startActivityForResult(intent, request);
    }

    private void showMagnet() {
        EditText input = new EditText(this);
        input.setHint("magnet:?");
        input.setSingleLine(true);
        input.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        FrameLayout frame = new FrameLayout(this);
        frame.setPadding(dp(20), 0, dp(20), 0);
        frame.addView(input);
        new AlertDialog.Builder(this).setTitle("Добавить magnet-ссылку").setView(frame)
                .setNegativeButton("Отмена", null)
                .setPositiveButton("Добавить", (dialog, which) -> importSource(PhoneCommands.MAGNET,
                        PhoneCommands.text(input.getText().toString().trim())))
                .show();
    }

    @Override
    @SuppressWarnings("deprecation")
    protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (result != RESULT_OK || data == null) return;
        if (request == TORRENT && data.getData() != null) {
            importSource(PhoneCommands.TORRENT, PhoneCommands.text(data.getData().toString()));
            return;
        }
        if (request != AUDIO) return;
        ArrayList<String> files = new ArrayList<>();
        ClipData clip = data.getClipData();
        if (clip != null) {
            for (int index = 0; index < clip.getItemCount(); index++) addFile(files, clip.getItemAt(index).getUri());
        } else if (data.getData() != null) {
            addFile(files, data.getData());
        }
        Bundle args = new Bundle();
        args.putStringArrayList("uris", files);
        importSource(PhoneCommands.LOCAL, args);
    }

    private void addFile(List<String> files, Uri uri) {
        if (uri == null) return;
        try {
            getContentResolver().takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION);
            files.add(uri.toString());
        } catch (SecurityException error) {
            message("Источник не разрешил сохранить доступ к файлу");
        }
    }

    private void importSource(SessionCommand command, Bundle args) {
        message("Добавляем источник…");
        send(command, args, response -> message("Добавлено треков: " + PhoneCommands.rows(response).size()));
    }

    private void send(SessionCommand command, Bundle args, Consumer<Bundle> success) {
        send(command, args, success, this::message);
    }

    private void send(SessionCommand command, Bundle args, Consumer<Bundle> success, Consumer<String> failure) {
        if (controller == null) return;
        ListenableFuture<SessionResult> request = controller.sendCustomCommand(command, args);
        request.addListener(() -> {
            if (isDestroyed()) return;
            try {
                SessionResult response = request.get();
                if (response.resultCode == SessionResult.RESULT_SUCCESS) success.accept(response.extras);
                else failure.accept(response.extras.getString("value", "Запрос не выполнен"));
            } catch (Exception error) {
                failure.accept("Плеер не ответил");
            }
        }, getMainExecutorCompat());
    }

    private void message(String text) {
        Toast.makeText(this, text, Toast.LENGTH_SHORT).show();
    }

    private String display(Bundle item) {
        String name = item.getString("title", "");
        String performer = item.getString("artist", "");
        return performer.isEmpty() ? name : performer + " — " + name;
    }

    private String details(Bundle item) {
        List<String> values = new ArrayList<>();
        String genre = item.getString("genre", "");
        String language = item.getString("language", "");
        if (!genre.isEmpty()) values.add(genre);
        if (language.equals("ru")) values.add("русский");
        else if (language.equals("en")) values.add("английский");
        else if (!language.isEmpty() && !language.equals("unknown")) {
            String translated = Locale.forLanguageTag(language).getDisplayLanguage(Locale.forLanguageTag("ru"));
            values.add(translated.isEmpty() ? language : translated);
        }
        if (item.getBoolean("live")) values.add("концерт");
        return String.join(" · ", values);
    }

    private Button transport(String icon, String description, Runnable action) {
        Button button = button(icon, action);
        button.setPadding(0, 0, 0, 0);
        button.setTextSize(22);
        button.setContentDescription(description);
        return button;
    }

    private Button button(String caption, Runnable action) {
        Button button = new Button(this);
        button.setText(caption);
        button.setAllCaps(false);
        button.setMinWidth(0);
        button.setTextSize(14);
        button.setTextColor(INK);
        button.setBackground(panel(PANEL, 12));
        button.setPadding(dp(10), 0, dp(10), 0);
        button.setOnClickListener(view -> action.run());
        return button;
    }

    private TextView label(String text, int size, int color, boolean bold) {
        TextView label = new TextView(this);
        label.setText(text);
        label.setTextSize(size);
        label.setTextColor(color);
        if (bold) label.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        return label;
    }

    private LinearLayout column() {
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        return layout;
    }

    private LinearLayout row() {
        LinearLayout layout = new LinearLayout(this);
        layout.setGravity(Gravity.CENTER_VERTICAL);
        return layout;
    }

    private android.graphics.drawable.Drawable panel(int color, int radius) {
        GradientDrawable background = new GradientDrawable();
        background.setColor(color);
        background.setCornerRadius(dp(radius));
        return new android.graphics.drawable.RippleDrawable(ColorStateList.valueOf(0x38FFFFFF), background, null);
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }
}
