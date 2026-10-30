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
 * Компактный режим: маленькое окно поверх фильма. Не перехватывает пульт и касания.
 * Строка 1: DMD, самый горячий лазер, процессор (цвет = запас до порога защиты).
 * Строка 2: вентиляторы и яркость. Строка 3: HDMI. Что показывать, размер и фон — в настройках (с 1.6.1).
 */
final class BuildInfo {
    static String version(android.content.Context c) {
        try { return c.getPackageManager().getPackageInfo(c.getPackageName(), 0).versionName; } catch (Exception e) { return "?"; }
    }
}

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
        nm.createNotificationChannel(new NotificationChannel("overlay", ru() ? "Компактный режим" : "Compact mode", NotificationManager.IMPORTANCE_MIN));
        Notification n = new Notification.Builder(this, "overlay")
                .setContentTitle("JMGO Temp").setContentText("JMGO Temp v" + BuildInfo.version(this))
                .setSmallIcon(android.R.drawable.stat_sys_warning).build();
        startForeground(1, n);
        if (!Settings.canDrawOverlays(this)) { stopSelf(); return; }
        wm = (WindowManager) getSystemService(WINDOW_SERVICE);
        view = new TextView(this);
        view.setTextColor(Color.WHITE);
        view.setTypeface(Typeface.MONOSPACE, Typeface.BOLD);
        applyStyle();
        view.setText("JMGO Temp…");
        wm.addView(view, lp = params());
        if (hidden) view.setVisibility(android.view.View.GONE);
        self = this;
        running = true;
        monitor = Monitor.get(this);
        monitor.addListener(this);
        monitor.acquire();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && "reposition".equals(intent.getAction()) && view != null) {
            minW = minH = 0; view.setMinWidth(0); view.setMinHeight(0);
            applyStyle();
            if (monitor != null && monitor.latest() != null) view.setText(format(monitor.latest()));
            wm.updateViewLayout(view, lp = params());
        }
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

    private int minW, minH;
    private WindowManager.LayoutParams lp;

    static final String DEF_ITEMS = "dmd,las,cpu,fan,br,hdmi";

    private android.content.SharedPreferences prefs() { return getSharedPreferences("overlay", MODE_PRIVATE); }

    /** Размер текста (sp) и прозрачность фона (%) из настроек. */
    private void applyStyle() {
        int sp = Math.max(8, Math.min(12, prefs().getInt("ov_size", 10))), bgPct = Math.max(0, Math.min(100, prefs().getInt("ov_bg", 80)));
        view.setTextSize(TypedValue.COMPLEX_UNIT_SP, sp);
        int p = Math.round(dp(8) * sp / 15f), ph = Math.round(dp(12) * sp / 15f);
        view.setPadding(ph, p, ph, p);
        GradientDrawable bg = new GradientDrawable();
        bg.setColor((Math.round(bgPct * 2.55f) << 24) | 0x101216);
        bg.setCornerRadius(dp(10));
        view.setBackground(bg);
        // без фона текст читается поверх светлой картинки только с тенью
        if (bgPct == 0) view.setShadowLayer(dp(2), 0, 0, 0xFF000000); else view.setShadowLayer(0, 0, 0, 0);
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
        if (v == null || Double.isNaN(limit)) return Color.WHITE;
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
        ui.post(() -> {
            if (view == null) return;
            view.setText(text);
            // окно только растет: если строка стала короче (пропала пометка, другое число), размер не меняется
            view.measure(android.view.View.MeasureSpec.UNSPECIFIED, android.view.View.MeasureSpec.UNSPECIFIED);
            boolean grow = false;
            if (view.getMeasuredWidth() > minW) { minW = view.getMeasuredWidth(); view.setMinWidth(minW); grow = true; }
            if (view.getMeasuredHeight() > minH) { minH = view.getMeasuredHeight(); view.setMinHeight(minH); }
            // WRAP_CONTENT окно система сначала меряет «шириной диалога» (~880 px), и длинная строка переносится: ширину задаем сами
            if (grow && lp != null) { lp.width = minW; try { wm.updateViewLayout(view, lp); } catch (Exception ignored) {} }
        });
    }

    private boolean ru() {
        String l = getSharedPreferences("overlay", MODE_PRIVATE).getString("lang", "");
        if (l.isEmpty()) l = Locale.getDefault().getLanguage();
        return l.startsWith("ru");
    }

    private CharSequence format(JSONObject d) {
        boolean ru = ru();
        SpannableStringBuilder sb = new SpannableStringBuilder();
        if (d.has("error")) { add(sb, ru ? "JMGO Temp: нет данных" : "JMGO Temp: no data", 0xFFF16B68); return sb; }
        JSONObject th = d.optJSONObject("thermal");
        JSONObject lim = d.optJSONObject("limits");
        if (th == null || lim == null) { add(sb, "JMGO Temp…", Color.WHITE); return sb; }
        java.util.Set<String> it = new java.util.HashSet<>(java.util.Arrays.asList(prefs().getString("ov_items", DEF_ITEMS).split(",")));
        Double dmd = th.isNull("dmd") ? null : th.optDouble("dmd");
        Double lm = th.isNull("laser_max") ? null : th.optDouble("laser_max");
        int grey = 0xFFA3AAB6;
        // строка 1: температуры; троттлинг показываем всегда, даже если процессор не выбран
        SpannableStringBuilder l1 = new SpannableStringBuilder();
        if (it.contains("dmd")) {
            add(l1, "DMD ", grey);
            add(l1, dmd == null ? "—" : String.format(Locale.US, "%.1f°", dmd), colorFor(dmd, lim.optDouble("dmd_shutdown", Double.NaN)));
        }
        if (it.contains("las")) {
            add(l1, (l1.length() > 0 ? "  " : "") + (ru ? "Лазер " : "Laser "), grey);
            add(l1, lm == null ? "—" : String.format(Locale.US, "%.1f°", lm), colorFor(lm, lim.optDouble("laser_derate", Double.NaN)));
        }
        boolean cT = it.contains("cpu") && d.has("temp"), cL = it.contains("load") && d.optInt("cpu_usage", -1) >= 0, cF = it.contains("freq") && d.optInt("freq", 0) > 0;
        if (cT || cL || cF) {
            add(l1, (l1.length() > 0 ? "  " : "") + "CPU", grey);
            if (cT) { int cpu = d.optInt("temp"); add(l1, " " + cpu + "°", cpu >= 100 ? 0xFFF16B68 : cpu >= 90 ? 0xFFF2BD55 : 0xFF7BE495); }
            // числа дополняем пробелами до постоянной ширины (шрифт моноширинный), чтобы текст справа не прыгал
            if (cL) add(l1, String.format(Locale.US, " %2d%%", d.optInt("cpu_usage")), Color.WHITE);
            if (cF) { add(l1, " " + d.optInt("freq"), Color.WHITE); add(l1, ru ? " МГц" : " MHz", grey); }
        }
        if (d.optBoolean("throttled", false)) add(l1, (l1.length() > 0 ? " " : "") + (ru ? "⚠ троттлинг " : "⚠ throttling ") + d.optInt("freq_cap") + (ru ? " МГц" : " MHz"), 0xFFF16B68);
        // строка 2: вентиляторы и яркость
        SpannableStringBuilder l2 = new SpannableStringBuilder();
        if (it.contains("fan")) {
            JSONArray duty = th.optJSONArray("fan_duty");
            int fan = -1;
            if (duty != null) for (int i = 0; i < duty.length(); i++) fan = Math.max(fan, duty.optInt(i));
            int rpm = Math.max(d.optInt("fan1", -1), d.optInt("fan2", -1));
            add(l2, ru ? "Вент " : "Fans ", grey);
            add(l2, rpm > 0 ? String.format(Locale.US, "%4d", rpm) + (ru ? " об/мин" : " rpm") : "—", Color.WHITE);
            if (fan >= 0) add(l2, String.format(Locale.US, " (%2d%%)", fan), grey);
        }
        if (it.contains("br")) {
            int mode = d.optInt("brightness_mode", -1);
            boolean fixed = mode == 15 || mode == 16 || mode == 22 || mode == 23;
            add(l2, (l2.length() > 0 ? "  " : "") + (ru ? "Яркость " : "Brightness "), grey);
            if (fixed) add(l2, ru ? "макс." : "max", Color.WHITE);
            else if (d.has("brightness_menu")) {
                add(l2, (d.optBoolean("brightness_exact", true) ? "" : "≈") + d.optInt("brightness_menu") + "/10", Color.WHITE);
                if (d.has("brightness_actual")) add(l2, " (AL ≈" + d.optInt("brightness_actual") + ")", 0xFF72AAFF);
            }
            else add(l2, "—", Color.WHITE);
            if (d.has("aperture")) {
                double a = d.optDouble("aperture");
                add(l2, "  F" + (a == Math.floor(a) ? String.valueOf((int) a) : String.format(Locale.US, "%.1f", a)), Color.WHITE);
            }
        }
        // строка 3: входной сигнал
        SpannableStringBuilder l3 = new SpannableStringBuilder();
        String rs = d.optString("input_rslt", "");
        boolean hd = it.contains("hdmi");
        if (hd && !rs.isEmpty() && !"null".equals(rs)) {
            add(l3, "HDMI ", grey);
            Matcher m = Pattern.compile("(\\d+)\\s*[xX×]\\s*(\\d+)(?:[^@\\d]*)?(?:@\\s*([\\d.]+))?").matcher(rs);
            String txt;
            if (m.find()) {
                String hz = m.group(3) != null ? m.group(3) : (d.has("input_fps") ? String.valueOf(d.optInt("input_fps")) : null);
                txt = shortRes(Integer.parseInt(m.group(1)), Integer.parseInt(m.group(2))) + (hz != null ? " " + roundHz(hz) + (ru ? " Гц" : " Hz") : "");
            } else txt = rs;
            add(l3, txt, Color.WHITE);
            if (d.optInt("input_vrr", 0) > 0) add(l3, " VRR", 0xFF72AAFF);
        } else if (hd && d.has("output_width")) {
            add(l3, ru ? "Вывод " : "Output ", grey);
            add(l3, shortRes(d.optInt("output_width"), d.optInt("output_height")) + " " + d.optInt("output_hz") + (ru ? " Гц" : " Hz"), Color.WHITE);
        }
        for (SpannableStringBuilder l : new SpannableStringBuilder[]{l1, l2, l3}) {
            if (l.length() == 0) continue;
            if (sb.length() > 0) sb.append("\n");
            sb.append(l);
        }
        if (sb.length() == 0) add(sb, "JMGO Temp", grey);
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
