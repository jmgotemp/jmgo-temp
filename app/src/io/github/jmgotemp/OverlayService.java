package io.github.jmgotemp;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.PixelFormat;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.provider.Settings;
import android.text.SpannableStringBuilder;
import android.text.Spanned;
import android.text.style.ForegroundColorSpan;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.WindowManager;
import android.widget.TextView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.Locale;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Маленькое окно поверх фильма. Не перехватывает пульт и касания.
 * Строка 1: DMD и самый горячий лазер (цвет = запас до порога защиты).
 * Строка 2: мощность вентиляторов и уровень яркости.
 */
public class OverlayService extends Service implements Monitor.Listener {
    static volatile boolean running;
    private static volatile boolean hidden;
    private static OverlayService self;

    static void setHidden(boolean h) {
        hidden = h;
        OverlayService s = self;
        if (s != null) s.ui.post(() -> { if (s.view != null) s.view.setVisibility(h ? android.view.View.GONE : android.view.View.VISIBLE); });
    }
    private WindowManager wm;
    private TextView view;
    private Monitor monitor;
    private final Handler ui = new Handler(Looper.getMainLooper());

    @Override public IBinder onBind(Intent i) { return null; }

    @Override
    public void onCreate() {
        super.onCreate();
        NotificationManager nm = getSystemService(NotificationManager.class);
        nm.createNotificationChannel(new NotificationChannel("overlay", "Окно температур", NotificationManager.IMPORTANCE_MIN));
        Notification n = new Notification.Builder(this, "overlay")
                .setContentTitle("JMGO Temp").setContentText("Окно температур включено")
                .setSmallIcon(android.R.drawable.stat_sys_warning).build();
        startForeground(1, n);
        if (!Settings.canDrawOverlays(this)) { stopSelf(); return; }
        wm = (WindowManager) getSystemService(WINDOW_SERVICE);
        view = new TextView(this);
        view.setTextColor(Color.WHITE);
        view.setTypeface(Typeface.MONOSPACE, Typeface.BOLD);
        view.setTextSize(TypedValue.COMPLEX_UNIT_SP, 15);
        int p = dp(8);
        view.setPadding(dp(12), p, dp(12), p);
        GradientDrawable bg = new GradientDrawable();
        bg.setColor(0xE6101216);
        bg.setCornerRadius(dp(10));
        view.setBackground(bg);
        view.setText("JMGO Temp…");
        wm.addView(view, params());
        if (hidden) view.setVisibility(android.view.View.GONE);
        self = this;
        running = true;
        monitor = Monitor.get(this);
        monitor.addListener(this);
        monitor.acquire();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && "reposition".equals(intent.getAction()) && view != null) wm.updateViewLayout(view, params());
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        running = false;
        self = null;
        if (monitor != null) { monitor.removeListener(this); monitor.release(); }
        if (view != null) { try { wm.removeView(view); } catch (Exception ignored) {} }
        super.onDestroy();
    }

    private int dp(int v) { return Math.round(v * getResources().getDisplayMetrics().density); }

    private WindowManager.LayoutParams params() {
        WindowManager.LayoutParams lp = new WindowManager.LayoutParams(
                WindowManager.LayoutParams.WRAP_CONTENT, WindowManager.LayoutParams.WRAP_CONTENT,
                WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE | WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE
                        | WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
                PixelFormat.TRANSLUCENT);
        String c = getSharedPreferences("overlay", MODE_PRIVATE).getString("corner", "tr");
        int g = (c.charAt(0) == 't' ? Gravity.TOP : Gravity.BOTTOM) | (c.charAt(1) == 'r' ? Gravity.END : Gravity.START);
        lp.gravity = g;
        lp.x = dp(24); lp.y = dp(20);
        return lp;
    }

    private static int colorFor(Double v, double limit) {
        if (v == null) return Color.WHITE;
        double m = limit - v;
        return m <= 2 ? 0xFFF16B68 : m <= 6 ? 0xFFF2BD55 : 0xFF7BE495;
    }

    private static void add(SpannableStringBuilder sb, String text, int color) {
        int s = sb.length();
        sb.append(text);
        sb.setSpan(new ForegroundColorSpan(color), s, sb.length(), Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
    }

    @Override
    public void onUpdate(JSONObject d) {
        final CharSequence text = format(d);
        ui.post(() -> { if (view != null) view.setText(text); });
    }

    private CharSequence format(JSONObject d) {
        SpannableStringBuilder sb = new SpannableStringBuilder();
        if (d.has("error")) { add(sb, "JMGO Temp: нет данных", 0xFFF16B68); return sb; }
        JSONObject th = d.optJSONObject("thermal");
        JSONObject lim = d.optJSONObject("limits");
        if (th == null || lim == null) { add(sb, "JMGO Temp…", Color.WHITE); return sb; }
        Double dmd = th.isNull("dmd") ? null : th.optDouble("dmd");
        Double lm = th.isNull("laser_max") ? null : th.optDouble("laser_max");
        int grey = 0xFFA3AAB6;
        add(sb, "DMD ", grey);
        add(sb, dmd == null ? "—" : String.format(Locale.US, "%.1f°", dmd), colorFor(dmd, lim.optDouble("dmd_shutdown", 55)));
        add(sb, "  Лазер ", grey);
        add(sb, lm == null ? "—" : String.format(Locale.US, "%.1f°", lm), colorFor(lm, lim.optDouble("laser_derate", 56.5)));
        if (d.has("temp")) {
            int cpu = d.optInt("temp");
            add(sb, "  CPU ", grey);
            add(sb, cpu + "°", cpu >= 100 ? 0xFFF16B68 : cpu >= 90 ? 0xFFF2BD55 : 0xFF7BE495);
        }
        sb.append("\n");
        // вентиляторы: обороты (их удобно сравнивать между аппаратами) и мощность в %
        JSONArray duty = th.optJSONArray("fan_duty");
        int fan = -1;
        if (duty != null) for (int i = 0; i < duty.length(); i++) fan = Math.max(fan, duty.optInt(i));
        int rpm = Math.max(d.optInt("fan1", -1), d.optInt("fan2", -1));
        add(sb, "Вент ", grey);
        add(sb, rpm > 0 ? rpm + " об/мин" : "—", Color.WHITE);
        if (fan >= 0) add(sb, " (" + fan + "%)", grey);
        if (d.has("brightness_menu")) {
            add(sb, "  Яркость ", grey);
            add(sb, (d.optBoolean("brightness_exact", true) ? "" : "≈") + d.optInt("brightness_menu") + "/10", Color.WHITE);
        }
        // разрешение: сигнал с HDMI, если он есть, иначе режим вывода проектора
        sb.append("\n");
        String rs = d.optString("input_rslt", "");
        if (!rs.isEmpty() && !"null".equals(rs)) {
            add(sb, "HDMI ", grey);
            // прошивка отдаёт строку вида "3840x2160@59Hz" — приводим к одному виду
            Matcher m = Pattern.compile("(\\d+)\\s*[xX×]\\s*(\\d+)(?:[^@\\d]*)?(?:@\\s*([\\d.]+))?").matcher(rs);
            String txt;
            if (m.find()) {
                String hz = m.group(3) != null ? m.group(3) : (d.has("input_fps") ? String.valueOf(d.optInt("input_fps")) : null);
                txt = shortRes(Integer.parseInt(m.group(1)), Integer.parseInt(m.group(2))) + (hz != null ? " " + roundHz(hz) + " Гц" : "");
            } else txt = rs;
            add(sb, txt, Color.WHITE);
            if (d.optInt("input_vrr", 0) > 0) add(sb, " VRR", 0xFF72AAFF);
        } else if (d.has("output_width")) {
            add(sb, "Вывод ", grey);
            add(sb, shortRes(d.optInt("output_width"), d.optInt("output_height")) + " " + d.optInt("output_hz") + " Гц", Color.WHITE);
        }
        return sb;
    }

    /** 3840×2160 → 4K, 2560×1440 → 2K, 1920×1080 → 1080p … */
    static String shortRes(int w, int h) {
        if (w >= 7680) return "8K";
        if (w >= 3840 || h >= 2160) return "4K";
        if (w >= 2560 || h >= 1440) return "2K";
        if (h >= 1080) return "1080p";
        if (h >= 720) return "720p";
        return h + "p";
    }

    /** 59.94 → 60, 23.976 → 24. Прошивка отбрасывает дробную часть (59.94 → «59»), поэтому 23/29/59/119 тоже поднимаем. */
    static String roundHz(String hz) {
        try {
            long v = Math.round(Double.parseDouble(hz));
            if (v == 23 || v == 29 || v == 47 || v == 59 || v == 119) v += 1;
            return String.valueOf(v);
        } catch (Exception e) { return hz; }
    }
}
