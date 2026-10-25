package io.github.jmgotemp;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.PixelFormat;
import android.graphics.drawable.GradientDrawable;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.provider.Settings;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.WindowManager;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.Locale;

/**
 * Фоновое наблюдение: после выхода из приложения (оно остается в памяти) раз в 5 с читает те же данные
 * и при троттлинге, перегреве или остановке вентилятора на 20 с показывает предупреждение поверх фильма.
 * Пока открыт экран приложения или включен компактный режим, предупреждение не нужно — там все видно.
 */
public class WatchService extends Service implements Monitor.Listener {
    static volatile boolean running;
    /** экран приложения открыт (onResume..onPause) */
    static volatile boolean appVisible;

    static final int THR = 1, DMD = 2, LASER = 4, FAN = 8, CPU = 16;
    /** ключи настроек (по умолчанию все включено); «watch» — следить в фоне вообще */
    // «вентилятор стоит» в фоне убрано в 1.4.1 (см. problems), поэтому w_fan здесь больше нет
    static final String[] KEYS = {"w_thr", "w_dmd", "w_las", "w_cpu"};
    static final int[] BITS = {THR, DMD, LASER, CPU};

    /** Процессор «перегрет» с той же температуры, с какой экран приложения красит его красным, но не ближе 10 °C к троттлингу. */
    static int cpuHot(int trip) { return Math.min(100, trip - 10); }
    private static final long SHOW_MS = 20_000, REPEAT_MS = 10 * 60_000;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private WindowManager wm;
    private TextView banner;
    private Monitor monitor;
    private int pending, pendingCount, shownMask;
    private long shownAt;

    @Override public IBinder onBind(Intent i) { return null; }

    @Override
    public void onCreate() {
        super.onCreate();
        NotificationManager nm = getSystemService(NotificationManager.class);
        nm.createNotificationChannel(new NotificationChannel("watch", ru() ? "Предупреждения о перегреве" : "Overheat warnings", NotificationManager.IMPORTANCE_MIN));
        startForeground(2, new Notification.Builder(this, "watch")
                .setContentTitle("JMGO Temp")
                .setContentText(ru() ? "Слежу за температурами" : "Watching temperatures")
                .setSmallIcon(android.R.drawable.stat_sys_warning).build());
        wm = (WindowManager) getSystemService(WINDOW_SERVICE);
        running = true;
        monitor = Monitor.get(this);
        monitor.addListener(this);
        monitor.acquire(false);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && "test".equals(intent.getAction())) {
            boolean ru = ru();
            ui.post(() -> show(ru ? "⚠ JMGO Temp (пример)\nЛазеры 56.0 °C — близко к снижению яркости (порог 56.5 °C)"
                    : "⚠ JMGO Temp (example)\nLasers 56.0 °C — close to dimming (limit 56.5 °C)"));
            if (!getSharedPreferences("overlay", MODE_PRIVATE).getBoolean("watch", true)) ui.postDelayed(this::stopSelf, SHOW_MS + 500);
        }
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        running = false;
        if (monitor != null) { monitor.removeListener(this); monitor.release(false); }
        ui.post(this::hide);
        super.onDestroy();
    }

    /** Что из проблем есть сейчас. Пороги — те же, что на экране приложения, но с меньшим запасом, чтобы не мешать фильму. */
    static int problems(Double dmd, double dmdOff, Double laser, double laserDim, boolean throttled,
                        int fan1, int fan2, int dutyMax, int cpu, int trip) {
        int m = 0;
        if (throttled) m |= THR;
        if (cpu > 0 && cpu >= cpuHot(trip)) m |= CPU;
        if (dmd != null && !Double.isNaN(dmdOff) && dmd >= dmdOff - 3) m |= DMD;
        if (laser != null && !Double.isNaN(laserDim) && laser >= laserDim - 1.5) m |= LASER;
        // вентиляторы в фоне не проверяем (с 1.4.1): обороты в фоне не читаются, чтобы не сбивать проверку
        // вентиляторов прошивки (ошибка 4001); остановку вентилятора прошивка ловит сама
        return m;
    }

    @Override
    public void onUpdate(JSONObject d) {
        if (d.has("error")) return;
        JSONObject th = d.optJSONObject("thermal"), lim = d.optJSONObject("limits");
        if (th == null || lim == null) return;
        int duty = -1;
        JSONArray a = th.optJSONArray("fan_duty");
        if (a != null) for (int i = 0; i < a.length(); i++) duty = Math.max(duty, a.optInt(i));
        Double dmd = th.isNull("dmd") ? null : th.optDouble("dmd");
        Double las = th.isNull("laser_max") ? null : th.optDouble("laser_max");
        int m = problems(dmd, lim.optDouble("dmd_shutdown", Double.NaN), las, lim.optDouble("laser_derate", Double.NaN),
                d.optBoolean("throttled", false), d.optInt("fan1", -1), d.optInt("fan2", -1), duty,
                d.optInt("temp", -1), d.optInt("trip_passive0", 123));
        // что пользователь выключил в «Предупреждениях», не показываем
        android.content.SharedPreferences p = getSharedPreferences("overlay", MODE_PRIVATE);
        for (int i = 0; i < KEYS.length; i++) if (!p.getBoolean(KEYS[i], true)) m &= ~BITS[i];
        // проблема должна держаться 2 опроса подряд (~10 с), чтобы не реагировать на одиночный всплеск
        pendingCount = m != 0 && m == pending ? pendingCount + 1 : (m != 0 ? 1 : 0);
        pending = m;
        if (m == 0) { shownMask = 0; return; }
        if (pendingCount < 2 || appVisible || OverlayService.running) return;
        long now = System.currentTimeMillis();
        boolean fresh = (m & ~shownMask) != 0;  // появилась новая проблема
        if (!fresh && now - shownAt < REPEAT_MS) return;
        shownMask = m;
        shownAt = now;
        String text = message(m, d, dmd, las, lim);
        ui.post(() -> show(text));
    }

    private String message(int m, JSONObject d, Double dmd, Double las, JSONObject lim) {
        boolean ru = ru();
        StringBuilder sb = new StringBuilder("⚠ JMGO Temp");
        if ((m & LASER) != 0) {
            double dim = lim.optDouble("laser_derate");
            sb.append('\n').append(ru
                    ? (las >= dim ? String.format(Locale.US, "Лазеры %.1f °C — прошивка снижает яркость (порог %s °C)", las, num(dim))
                                  : String.format(Locale.US, "Лазеры %.1f °C — близко к снижению яркости (порог %s °C)", las, num(dim)))
                    : (las >= dim ? String.format(Locale.US, "Lasers %.1f °C — firmware is dimming (limit %s °C)", las, num(dim))
                                  : String.format(Locale.US, "Lasers %.1f °C — close to dimming (limit %s °C)", las, num(dim))));
        }
        if ((m & DMD) != 0)
            sb.append('\n').append(String.format(Locale.US, ru ? "DMD %.1f °C — близко к отключению проектора (порог %s °C)"
                    : "DMD %.1f °C — close to projector shutdown (limit %s °C)", dmd, num(lim.optDouble("dmd_shutdown"))));
        if ((m & FAN) != 0)
            sb.append('\n').append(ru ? "Вентилятор не крутится при поданной мощности — проверьте охлаждение"
                    : "A fan is not spinning while powered — check the cooling");
        if ((m & THR) != 0)
            sb.append('\n').append(String.format(Locale.US, ru ? "Процессор %d °C — троттлинг, частота до %d МГц"
                    : "CPU %d °C — throttling, frequency capped at %d MHz", d.optInt("temp"), d.optInt("freq_cap")));
        else if ((m & CPU) != 0)
            sb.append('\n').append(String.format(Locale.US, ru ? "Процессор %d °C — перегрев (троттлинг с %d °C)"
                    : "CPU %d °C — overheating (throttling from %d °C)", d.optInt("temp"), d.optInt("trip_passive0", 123)));
        return sb.toString();
    }

    private static String num(double v) { return v == Math.floor(v) ? String.valueOf((int) v) : String.valueOf(v); }

    private void show(String text) {
        if (!Settings.canDrawOverlays(this)) {  // нет разрешения на окно поверх — хотя бы всплывающая подсказка
            Toast.makeText(this, text, Toast.LENGTH_LONG).show();
            return;
        }
        if (banner == null) {
            banner = new TextView(this);
            banner.setTextColor(Color.WHITE);
            banner.setTextSize(TypedValue.COMPLEX_UNIT_SP, 17);
            banner.setLineSpacing(0, 1.15f);
            int p = dp(14);
            banner.setPadding(dp(22), p, dp(22), p);
            GradientDrawable bg = new GradientDrawable();
            bg.setColor(0xFF2A1517);  // непрозрачный: сквозь плашку не просвечивает картинка
            bg.setStroke(dp(2), 0xFFF16B68);
            bg.setCornerRadius(dp(12));
            banner.setBackground(bg);
            WindowManager.LayoutParams lp = new WindowManager.LayoutParams(
                    WindowManager.LayoutParams.WRAP_CONTENT, WindowManager.LayoutParams.WRAP_CONTENT,
                    WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
                    WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE | WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE
                            | WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
                    PixelFormat.TRANSLUCENT);
            // ширина по самой длинной строке: иначе окно поверх сужается и переносит текст
            int screen = getResources().getDisplayMetrics().widthPixels;
            float w = 0;
            for (String line : text.split("\n")) w = Math.max(w, banner.getPaint().measureText(line));
            lp.width = Math.min((int) w + dp(48), (int) (screen * 0.9f));
            lp.gravity = Gravity.TOP | Gravity.START;  // слева сверху: не закрывает центр кадра
            lp.x = dp(24);
            lp.y = dp(20);
            try { wm.addView(banner, lp); } catch (Exception e) { banner = null; return; }
        }
        banner.setText(text);
        banner.setVisibility(View.VISIBLE);
        WindowManager.LayoutParams cur = (WindowManager.LayoutParams) banner.getLayoutParams();
        if (cur != null) {
            float w = 0;
            for (String line : text.split("\n")) w = Math.max(w, banner.getPaint().measureText(line));
            cur.width = Math.min((int) w + dp(48), (int) (getResources().getDisplayMetrics().widthPixels * 0.9f));
            try { wm.updateViewLayout(banner, cur); } catch (Exception ignored) {}
        }
        ui.removeCallbacks(hideTask);
        ui.postDelayed(hideTask, SHOW_MS);
    }

    private final Runnable hideTask = this::hide;

    private void hide() {
        if (banner != null) { try { wm.removeView(banner); } catch (Exception ignored) {} banner = null; }
    }

    private int dp(int v) { return Math.round(v * getResources().getDisplayMetrics().density); }

    private boolean ru() {
        String l = getSharedPreferences("overlay", MODE_PRIVATE).getString("lang", "");
        if (l.isEmpty()) l = Locale.getDefault().getLanguage();
        return l.startsWith("ru");
    }
}
