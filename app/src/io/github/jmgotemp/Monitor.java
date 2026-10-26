package io.github.jmgotemp;

import android.app.ActivityManager;
import android.content.Context;
import android.content.pm.ApplicationInfo;
import android.os.Build;
import android.os.Environment;
import android.os.IBinder;
import android.os.Parcel;
import android.os.StatFs;
import android.os.SystemClock;
import android.view.Display;
import android.view.WindowManager;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.FileReader;
import java.io.InputStream;
import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.ArrayDeque;
import java.util.Iterator;
import java.util.List;
import java.util.Random;
import java.util.Locale;
import java.util.TreeMap;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.zip.ZipEntry;
import java.util.zip.ZipFile;

/**
 * Данные берутся у системного сервиса JMGO (jmgomiddle-01 -> DLP manager),
 * того же, к которому обращаются штатные настройки и «Клон настроек».
 * Все только читается, кроме одного: по выбору пользователя (N5/N5S/IRIS) — цель температуры,
 * которую держат вентиляторы (set 0x11a). Прошивка не хранит ее после перезагрузки.
 */
public final class Monitor {
    private static final String SYS = "com.jmgo.middleware.service.IJmGOSystemService";
    private static final String DLP = "com.jmgo.middleware.dlp.IJmGODlpManagerService";
    private static final int T_SET = 1, T_GET = 2, T_GET_STRING = 4, T_GET_ARRAY = 6;
    private static final int IDX_TEMPS = 0x105, IDX_FAN_DUTY = 0xa7, IDX_FAN_STATUS = 0x76,
            IDX_FAN_MODE = 0x70, IDX_BRIGHTNESS = 0x200, IDX_OVERLAP_LEVEL = 0x334,
            IDX_BRIGHTNESS_MODE = 0x64, IDX_LENS = 0x138, // температура объектива (getString), −999 — нет датчика
            IDX_APERTURE = 0x213, // диафрагма: F × 100 (как в «Клоне настроек»); ≤ 0 — нет диафрагмы (N5 Pro)
            // цель PID вентиляторов поверх конфига (hippo setFactoryPIDTemperature); −1 — как в прошивке.
            // Прошивка сама ставит ее на белом экране (режим 22) и сбрасывает в −1 после него.
            IDX_FAN_TARGET = 0x11a;
    /** Модели, где цель проверена по прошивке: N5 Pro / N5 (K981, K983), N5S Ultra Max / IRIS Ultra Max (KD11), N5S Ultra / IRIS Ultra (KD21),
     *  серия N3 (K930, K931, K940, K941: CN-коды из одного пакета обновления K931CN/K941CN, глобальная N3 Ultimate K931AO).
     *  На китайской N3 Ultra запись проверена владельцем. */
    private static final String[] FAN_CTL_MODELS = {"K981", "K983", "KD11", "KD21", "K930", "K931", "K940", "K941"};
    /** С какой ступени меню яркости действует вторая цель («яркая»): 1–6 — первая, 7–10 и фиксированные яркие режимы — вторая. */
    static final int FAN_HI_LEVEL = 7;
    static final int MODE_WHITE = 22;

    private static Monitor instance;

    public static synchronized Monitor get(Context c) {
        if (instance == null) instance = new Monitor(c.getApplicationContext());
        return instance;
    }

    public interface Listener { void onUpdate(JSONObject latest); }

    private final Context ctx;
    private final ArrayDeque<JSONObject> history = new ArrayDeque<>();
    private final List<Listener> listeners = new ArrayList<>();
    private volatile JSONObject latest = new JSONObject();
    private IBinder dlp;
    private IBinder src;
    private JSONObject limits;
    private Thread thread;
    private int users, fastUsers;
    private long lastHistory;
    private final long sessionStart = System.currentTimeMillis() / 1000;
    private Double maxDmd, maxLaser; private Integer maxCpu;
    private long[] prevCpu;
    private Long throttleSince, throttleFirst;
    private Integer lastFan1, lastFan2;
    private int zeroFan1, zeroFan2;
    /** Обороты читаем не чаще раза в 5 с (± 0,5 с) и только для экрана / компактного режима.
     *  На N5S Ultra Max и IRIS каждое чтение /sys/class/jmgo_fan/tachometer перезапускает общий замер
     *  оборотов, и прошивка в своей проверке вентиляторов может получить ложный 0 об/мин (ошибка 4001). */
    private static final long TACH_PERIOD_MS = 5000;
    private long nextTachAt;
    private final Random rnd = new Random();
    private String dlpVersion;
    /** Цели, выбранные в приложении: для яркости 1–6 и 7–10; null — как в прошивке.
     *  Хранятся в настройках вместе с номером загрузки: после перезагрузки сбрасываются,
     *  если не включено «Применять после включения проектора» (fanBoot). */
    private volatile Integer fanLo, fanHi;
    private volatile boolean fanBoot, fanLoaded;
    /** Что приложение записало последним (−1 — вернуло авто); null — еще ничего. */
    private Integer fanApplied;
    private boolean sleeping;
    /** Троттлинг: потолок частоты за сеанс, текущее (сглаженное) состояние и число несовпадающих опросов подряд. */
    private Integer capBase;
    private boolean thrState;
    private int thrStreak;
    private String coolType;
    /** Штатная цель модели и разрешенные значения (из FAN_PID_CONTROL_CONFIG). */
    private int fanAuto = -1;
    private int[] fanOpts = new int[0];

    private Monitor(Context c) { ctx = c; }

    public synchronized void addListener(Listener l) { listeners.add(l); }
    public synchronized void removeListener(Listener l) { listeners.remove(l); }

    public synchronized void acquire() { acquire(true); }
    public synchronized void release() { release(true); }

    /** fast = раз в секунду (экран приложения, окно поверх фильма); иначе раз в 5 с — фоновые предупреждения. */
    public synchronized void acquire(boolean fast) {
        users++;
        if (fast) { fastUsers++; nextTachAt = 0; notifyAll(); }
        if (thread == null) {
            thread = new Thread(this::loop, "jmgo-monitor");
            thread.setDaemon(true);
            thread.start();
        }
    }

    public synchronized void release(boolean fast) {
        users = Math.max(0, users - 1);
        if (fast) fastUsers = Math.max(0, fastUsers - 1);
        if (users == 0 && thread != null) { thread.interrupt(); thread = null; }
    }

    /** Проектор выключен с пульта (сон, STR) или засыпает: hippo ставит sys.jmgo.sleep=1, экран гаснет.
     *  В это время ничего не опрашиваем и не пишем — переход в сон не должен ждать наших вызовов. */
    private boolean asleep() {
        if ("1".equals(prop("sys.jmgo.sleep").trim())) return true;
        // выключение с пульта на JMGO: Android не засыпает (экран для системы «включен»), hippo ставит
        // jmgo.system.power.state ≠ 0 (N5 Pro: 3 — «спящий режим») и останавливает вентиляторы
        String ps = prop("jmgo.system.power.state").trim();
        if (!ps.isEmpty() && !"0".equals(ps)) return true;
        try {
            android.os.PowerManager pm = (android.os.PowerManager) ctx.getSystemService(Context.POWER_SERVICE);
            return pm != null && !pm.isInteractive();
        } catch (Throwable e) { return false; }
    }

    private void loop() {
        while (!Thread.currentThread().isInterrupted()) {
            if (asleep()) {
                // проснемся — продолжим; выбранную цель Monitor поставит снова, если прошивка ее сбросила
                if (!sleeping) { sleeping = true; android.util.Log.i("JmgoTemp", "projector asleep (power.state=" + prop("jmgo.system.power.state") + "): polling paused"); }
                synchronized (this) { fanApplied = null; }
                try { synchronized (this) { wait(5000); } } catch (InterruptedException e) { return; }
                continue;
            }
            if (sleeping) { sleeping = false; android.util.Log.i("JmgoTemp", "projector awake: polling resumed"); }
            JSONObject snap;
            try { snap = snapshot(); } catch (Throwable t) {
                dlp = null;
                snap = new JSONObject();
                try {
                    String code = t instanceof IllegalStateException && t.getMessage() != null && t.getMessage().startsWith("no_") ? t.getMessage() : "service";
                    snap.put("error_code", code);
                    snap.put("error", code.equals("service") ? String.valueOf(t) : code);
                    snap.put("ts", now());
                } catch (Exception ignored) {}
            }
            latest = snap;
            List<Listener> ls;
            synchronized (this) {
                if (!snap.has("error") && System.currentTimeMillis() - lastHistory >= 5000) {
                    lastHistory = System.currentTimeMillis();
                    try {
                        JSONObject h = new JSONObject();
                        JSONObject th = snap.getJSONObject("thermal");
                        h.put("ts", snap.get("ts"));
                        h.put("temp", snap.opt("temp"));
                        h.put("fan1", snap.opt("fan1"));
                        h.put("fan2", snap.opt("fan2"));
                        h.put("dmd", th.opt("dmd"));
                        h.put("l", th.opt("lasers"));
                        h.put("load", snap.opt("cpu_usage"));
                        h.put("fpct", snap.opt("freq_pct"));
                        h.put("thr", snap.optBoolean("throttled", false));
                        history.addLast(h);
                        while (history.size() > 720) history.removeFirst();
                    } catch (Exception ignored) {}
                }
                ls = new ArrayList<>(listeners);
            }
            for (Listener l : ls) { try { l.onUpdate(snap); } catch (Throwable ignored) {} }
            try { synchronized (this) { wait(fastUsers > 0 ? 1000 : 5000); } } catch (InterruptedException e) { return; }
        }
    }

    public JSONObject latest() { return latest; }

    public synchronized String apiJson() {
        try {
            JSONObject o = new JSONObject();
            JSONObject l = latest;
            // выбор цели вентиляторов берем текущий, а не из последнего замера: замер мог быть сделан
            // до нажатия, и тогда экран на секунду показал бы прежнее значение (галочка «мигала»)
            JSONObject fc = l.optJSONObject("fan_ctl");
            if (fc != null) {
                JSONObject f = new JSONObject(fc.toString());
                Integer lo = fanLo, hi = fanHi;
                f.put("lo", lo == null ? 0 : lo);
                f.put("hi", hi == null ? 0 : hi);
                f.put("boot", fanBoot);
                Integer want = "hi".equals(f.optString("use")) ? hi : lo;
                if (want != null) f.put("custom", want); else f.remove("custom");
                l = new JSONObject(l.toString());
                l.put("fan_ctl", f);
            }
            o.put("latest", l);
            JSONArray a = new JSONArray();
            for (JSONObject h : history) a.put(h);
            o.put("history", a);
            return o.toString();
        } catch (Exception e) { return "{\"latest\":{\"error\":\"" + e + "\"},\"history\":[]}"; }
    }

    // ------------------------------------------------------------------ binder

    private IBinder dlp() throws Exception {
        if (dlp != null && dlp.isBinderAlive()) return dlp;
        Class<?> sm = Class.forName("android.os.ServiceManager");
        Method gs = sm.getDeclaredMethod("getService", String.class);
        IBinder sys = (IBinder) gs.invoke(null, "jmgomiddle-01");
        if (sys == null) throw new IllegalStateException("no_service");
        Parcel d = Parcel.obtain(), r = Parcel.obtain();
        try {
            d.writeInterfaceToken(SYS);
            sys.transact(5, d, r, 0);
            r.readException();
            dlp = r.readStrongBinder();
        } finally { d.recycle(); r.recycle(); }
        if (dlp == null) throw new IllegalStateException("no_dlp");
        return dlp;
    }

    // ------------------------------------------------------------------ HDMI-вход (сигнал источника)

    private static final String SRC = "com.jmgo.middleware.tv.IJmGOTvInputSrcService";

    private IBinder src() throws Exception {
        if (src != null && src.isBinderAlive()) return src;
        Class<?> sm = Class.forName("android.os.ServiceManager");
        IBinder sys = (IBinder) sm.getDeclaredMethod("getService", String.class).invoke(null, "jmgomiddle-01");
        Parcel d = Parcel.obtain(), r = Parcel.obtain();
        try {
            d.writeInterfaceToken(SYS);
            sys.transact(3, d, r, 0); // getTvInputSrcService
            r.readException();
            src = r.readStrongBinder();
        } finally { d.recycle(); r.recycle(); }
        return src;
    }

    /** code 1 = getIptsRslt (String), 7 = getIptsFrameRate, 15 = isSignalLocked, 14 = getVrrMode */
    private Object srcCall(int code, boolean str) {
        Parcel d = Parcel.obtain(), r = Parcel.obtain();
        try {
            IBinder b = src();
            if (b == null) return null;
            d.writeInterfaceToken(SRC);
            b.transact(code, d, r, 0); r.readException();
            return str ? r.readString() : (Object) r.readInt();
        } catch (Exception e) { return null; } finally { d.recycle(); r.recycle(); }
    }

    private String getString(int idx) throws Exception {
        Parcel d = Parcel.obtain(), r = Parcel.obtain();
        try {
            d.writeInterfaceToken(DLP); d.writeInt(idx);
            dlp().transact(T_GET_STRING, d, r, 0); r.readException();
            return r.readString();
        } finally { d.recycle(); r.recycle(); }
    }

    private int[] getArray(int idx) throws Exception {
        Parcel d = Parcel.obtain(), r = Parcel.obtain();
        try {
            d.writeInterfaceToken(DLP); d.writeInt(idx);
            dlp().transact(T_GET_ARRAY, d, r, 0); r.readException();
            return r.createIntArray();
        } finally { d.recycle(); r.recycle(); }
    }

    private Integer getInt(int idx) {
        Parcel d = Parcel.obtain(), r = Parcel.obtain();
        try {
            d.writeInterfaceToken(DLP); d.writeInt(idx);
            dlp().transact(T_GET, d, r, 0); r.readException();
            return r.readInt();
        } catch (Exception e) { return null; } finally { d.recycle(); r.recycle(); }
    }

    private int setInt(int idx, int value) throws Exception {
        Parcel d = Parcel.obtain(), r = Parcel.obtain();
        try {
            d.writeInterfaceToken(DLP); d.writeInt(idx); d.writeInt(value);
            dlp().transact(T_SET, d, r, 0); r.readException();
            return r.readInt();
        } finally { d.recycle(); r.recycle(); }
    }

    // ------------------------------------------------------------------ цель вентиляторов

    static boolean fanCtlModel(String m) {
        if (m == null) return false;
        String u = m.trim().toUpperCase(Locale.ROOT);
        for (String p : FAN_CTL_MODELS) if (u.startsWith(p)) return true;
        return false;
    }

    public boolean hasFanTarget() { loadFanPrefs(); return fanLo != null || fanHi != null; }

    private android.content.SharedPreferences prefs() { return ctx.getSharedPreferences("overlay", Context.MODE_PRIVATE); }

    /** Номер загрузки: Settings.Global.BOOT_COUNT, если его нет — время загрузки в минутах (со знаком минус). */
    private int bootId() {
        try {
            int n = android.provider.Settings.Global.getInt(ctx.getContentResolver(), "boot_count", -1);
            if (n >= 0) return n;
        } catch (Throwable ignored) {}
        return -(int) ((System.currentTimeMillis() - SystemClock.elapsedRealtime()) / 60000);
    }

    private static boolean sameBoot(int a, int b) { return a >= 0 ? a == b : b < 0 && Math.abs(a - b) <= 1; }

    /** Есть ли сохраненная цель, которую надо держать после включения (для автозапуска). */
    static boolean bootTargetSaved(Context c) {
        android.content.SharedPreferences p = c.getSharedPreferences("overlay", Context.MODE_PRIVATE);
        return p.getBoolean("fan_boot", false) && (p.getInt("fan_lo", 0) > 0 || p.getInt("fan_hi", 0) > 0);
    }

    /** Выбор из настроек: та же загрузка — восстанавливаем (приложение перезапускали); новая — только с «Применять после включения». */
    private synchronized void loadFanPrefs() {
        if (fanLoaded) return;
        fanLoaded = true;
        android.content.SharedPreferences p = prefs();
        fanBoot = p.getBoolean("fan_boot", false);
        int lo = p.getInt("fan_lo", 0), hi = p.getInt("fan_hi", 0);
        if (lo <= 0 && hi <= 0) return;
        if (fanBoot || sameBoot(bootId(), p.getInt("fan_bootid", Integer.MIN_VALUE))) {
            fanLo = lo > 0 ? lo : null;
            fanHi = hi > 0 ? hi : null;
            p.edit().putInt("fan_bootid", bootId()).apply();
        } else {
            p.edit().remove("fan_lo").remove("fan_hi").apply();
        }
    }

    /** Значение из старого списка (например, 41 из 1.5), которого нет в текущем, — к ближайшему из списка, при равенстве к холодному. */
    private Integer nearestOpt(Integer t) {
        if (t == null || allowed(t)) return t;
        Integer best = null;
        for (int v : fanOpts) if (best == null || Math.abs(v - t) < Math.abs(best - t)) best = v;
        return best;
    }

    private boolean allowed(int t) {
        if (t <= 0) return true;
        for (int v : fanOpts) if (v == t) return true;
        return false;
    }

    /** lo — для яркости 1–6, hi — для 7–10; t > 0 — своя (только из списка модели), ≤ 0 — как в прошивке. Возвращает "ok" или текст ошибки. */
    public String setFanTargets(int lo, int hi) {
        try {
            limits();
            loadFanPrefs();
            if (!fanCtlModel(model()) || fanAuto <= 0) return "unsupported";
            if (!allowed(lo) || !allowed(hi)) return "bad_value";
            fanLo = lo > 0 ? lo : null;
            fanHi = hi > 0 ? hi : null;
            prefs().edit().putInt("fan_lo", Math.max(0, lo)).putInt("fan_hi", Math.max(0, hi)).putInt("fan_bootid", bootId()).apply();
            applyFanTarget(latest, null, true);
            synchronized (this) { notifyAll(); }
            return "ok";
        } catch (Exception e) { return "error: " + e; }
    }

    public void setFanBoot(boolean on) {
        loadFanPrefs();
        fanBoot = on;
        prefs().edit().putBoolean("fan_boot", on).putInt("fan_bootid", bootId()).apply();
    }

    public boolean fanBoot() { loadFanPrefs(); return fanBoot; }

    /** Какая цель нужна сейчас: фиксированные яркие режимы, ступени 7–10 и неизвестная яркость — «яркая». */
    private static boolean useHi(JSONObject s) {
        int mode = s.optInt("brightness_mode", -1);
        if (mode == 15 || mode == 16 || mode == 22 || mode == 23) return true;
        return !s.has("brightness_menu") || s.optInt("brightness_menu", 10) >= FAN_HI_LEVEL;
    }

    /** Ставит нужную цель. Пишем, только когда нужная цель изменилась (ступень яркости, выбор в меню)
     *  или прошивка сбросила ее в −1 (после белого экрана). На белом экране приоритет у прошивки. */
    private synchronized Integer applyFanTarget(JSONObject s, Integer fcNow, boolean user) {
        if (s == null || s.optInt("brightness_mode", -1) == MODE_WHITE) return fcNow;
        try {
            if (fcNow == null) fcNow = getInt(IDX_FAN_TARGET);
            if (fcNow == null) return null;
            Integer want = useHi(s) ? fanHi : fanLo;
            int w = want == null ? -1 : want;
            boolean changed = fanApplied == null ? (want != null || fcNow > 0 && allowed(fcNow) && (fanLo != null || fanHi != null))
                    : fanApplied != w;
            if (want != null && (changed || fcNow == -1) && fcNow != w) { setInt(IDX_FAN_TARGET, w); fcNow = w; }
            // вернуть авто: только если сейчас стоит наша цель (не трогаем чужую и цель белого экрана)
            // (выбор «авто» в меню — тоже, даже если приложение перезапускали и не помнит, что ставило)
            else if (want == null && (changed || user) && fcNow > 0 && allowed(fcNow)) { setInt(IDX_FAN_TARGET, -1); fcNow = -1; }
            fanApplied = w;
        } catch (Exception ignored) {}
        return fcNow;
    }

    // ------------------------------------------------------------------ helpers

    private static long now() { return System.currentTimeMillis() / 1000; }

    private static String readFile(String path) {
        try (BufferedReader br = new BufferedReader(new FileReader(path))) {
            String s = br.readLine();
            return s == null ? null : s.trim();
        } catch (Exception e) { return null; }
    }

    private static Integer readInt(String path) {
        String s = readFile(path);
        if (s == null) return null;
        try { return Integer.parseInt(s.trim()); } catch (Exception e) { return null; }
    }

    private static Double num(String s) {
        try {
            double v = Double.parseDouble(s);
            if (v <= -100 || v > 200) return null;
            return Math.round(v * 10) / 10.0;
        } catch (Exception e) { return null; }
    }

    private static String model() {
        try {
            Class<?> sp = Class.forName("android.os.SystemProperties");
            String m = (String) sp.getMethod("get", String.class).invoke(null, "ro.boot.jmgo.model");
            if (m != null && !m.isEmpty()) return m;
        } catch (Throwable ignored) {}
        return Build.MODEL;
    }

    /** Коды, у которых название известно точно: N5 — проверено на проекторах, остальные — Google Play / FCC. */
    private static final String[][] NAMES = {
            {"K981CN", "JMGO N5 Pro"}, {"KD11CN", "JMGO N5S Ultra Max"}, {"KD11AX", "JMGO IRIS Ultra Max"},
            {"K931AO", "JMGO N3 Ultimate"}, {"S70A", "JMGO N1"}, {"H80A", "JMGO N1 Pro"},
            {"K92A", "JMGO N1S Infinity"}, {"K93A", "JMGO N1S Pro"}, {"K610AO", "JMGO N1S SE"},
            {"T91A", "JMGO O2S Ultra"}, {"m7642_JmGO_H79P", "JMGO O1"}, {"m7642_JmGO_T72A", "JMGO O1 Pro"}};

    private static String modelName(String model) {
        String sale = prop("vendor.jmgo.internal.salename").trim();
        if (!sale.isEmpty()) return sale.toUpperCase(Locale.ROOT).startsWith("JMGO") ? sale : "JMGO " + sale;
        for (String c : new String[]{model, Build.MODEL, Build.DEVICE})
            for (String[] n : NAMES) if (n[0].equalsIgnoreCase(c)) return n[1];
        return "JMGO " + model;
    }

    private static String prop(String key) {
        try {
            Class<?> sp = Class.forName("android.os.SystemProperties");
            return (String) sp.getMethod("get", String.class).invoke(null, key);
        } catch (Throwable e) { return ""; }
    }

    // ------------------------------------------------------------------ пороги из прошивки

    private JSONObject limits() {
        if (limits != null) return limits;
        JSONObject l = new JSONObject();
        try {
            l.put("dmd_shutdown", 55.0); l.put("laser_derate", 56.5); l.put("laser_shutdown", 57.0);
            l.put("pid_targets", new JSONArray().put(40.0).put(46.0).put(51.0));
            l.put("interp_r", new JSONArray(new int[]{360, 420, 480, 540, 600, 660, 720, 790, 870, 1023}));
            l.put("value_max", new JSONArray(new int[]{1023, 1023, 1023}));
            l.put("from_firmware", false);
            String xml = readHippoConfig();
            if (xml != null) xml = xml.replaceAll("(?s)<!--.*?-->", "");
            if (xml != null) {
                List<double[]> check = rows(xml, "SYSTEM_CHECK_TEMPERATURE_CONFIG");
                Double ls = null, ld = null;
                boolean hasDmd = false;
                laserIdx.clear();
                for (double[] r : check) {
                    if (r.length < 2) continue;
                    int idx = (int) r[0];
                    if (idx == 7) { l.put("dmd_shutdown", r[1]); hasDmd = true; }
                    else if (idx == 10 || idx == 11 || idx == 12 || idx == 14) {
                        laserIdx.add(idx);
                        ls = ls == null ? r[1] : Math.min(ls, r[1]);
                        if (r.length > 2 && r[2] > 0) ld = ld == null ? r[2] : Math.min(ld, r[2]);
                    }
                }
                // у модели нет такого датчика в самопроверке — порога нет (не подставляем значения N5 Pro)
                if (!hasDmd) l.remove("dmd_shutdown");
                if (ls != null) l.put("laser_shutdown", ls); else l.remove("laser_shutdown");
                if (ld != null) l.put("laser_derate", ld); else l.remove("laser_derate");
                for (double v : list(xml, "INTELLIGENT_FAN_CONTROL")) {
                    int idx = (int) v;
                    if (idx == 10 || idx == 11 || idx == 12 || idx == 14) laserIdx.add(idx);
                }
                List<double[]> pid = rows(xml, "FAN_PID_CONTROL_CONFIG");
                if (!pid.isEmpty()) {
                    JSONArray p = new JSONArray(), fx = new JSONArray();
                    for (double[] r : pid) if (r.length > 0) { p.put(r[0]); fx.put(r.length > 2 && r[1] >= r[2]); }
                    l.put("pid_targets", p);
                    l.put("pid_fixed", fx);
                    // цель вентиляторов: штатная — первая строка PID. Список — четные градусы: 4 ниже авто (не ближе 2 °C к авто,
                    // не ниже 34 °C) и 3 выше, но не ближе 4 °C к снижению яркости (если порога нет — не выше цели ярких режимов).
                    // Исключение — N5S Ultra Max: 5 ниже и 2 выше
                    double base = -1, bright = -1;
                    for (double[] r : pid) if (r.length > 2 && r[1] < r[2]) { if (base < 0) base = r[0]; bright = Math.max(bright, r[0]); }
                    if (base > 0) {
                        int b = (int) Math.round(base);
                        int cap = ld != null ? (int) Math.floor(ld - 4) : (int) Math.floor(bright);
                        int below = (b - 2) / 2 * 2, above = b / 2 * 2 + 2;   // четное не ближе 2 °C ниже авто; ближайшее четное выше
                        // N5S Ultra Max (KD11CN, авто 48): по просьбе владельцев 5 значений ниже и 2 выше — 38…46 · 50 52
                        boolean um = model().toUpperCase(Locale.ROOT).startsWith("KD11CN");
                        int nBelow = um ? 5 : 4, nAbove = um ? 2 : 3;
                        List<Integer> o = new ArrayList<>();
                        for (int v = Math.max(34, below - 2 * (nBelow - 1)); v <= below; v += 2) o.add(v);
                        for (int v = above; v <= Math.min(above + 2 * (nAbove - 1), cap); v += 2) o.add(v);
                        fanAuto = b;
                        fanOpts = new int[o.size()];
                        for (int i = 0; i < fanOpts.length; i++) fanOpts[i] = o.get(i);
                    }
                }
                dmdMux = list(xml, "TEMPERATURE_DMD_CHANNEL_MUX");
                modeCurrents.clear();
                for (double[] r : rows(xml, "BRIGHTNESS_VALUE")) if (r.length >= 5 && r[0] > 0 && r[2] >= 0) modeCurrents.add(new int[]{(int) r[0], (int) r[2], (int) r[3], (int) r[4]});
                dmdDlpCfg = list(xml, "DMD_TEMPERATURE_CHECK_CONFIG");
                List<double[]> interp = rows(xml, "DISPLAY_BRIGHTNESS_INTERPOLATOR");
                if (!interp.isEmpty() && interp.get(0).length >= 2) {
                    JSONArray a = new JSONArray();
                    for (double v : interp.get(0)) a.put((int) v);
                    l.put("interp_r", a);
                }
                Matcher m = Pattern.compile("key=\"BRIGHTNESS_VALUE_MAX\"\\s+value=\"([^\"]*)\"").matcher(xml);
                if (m.find()) {
                    String[] p = m.group(1).split(",");
                    JSONArray a = new JSONArray();
                    for (int i = 0; i < Math.min(3, p.length); i++) a.put(Integer.parseInt(p[i].trim()));
                    l.put("value_max", a);
                }
                l.put("from_firmware", true);
                if (configName != null) l.put("config", configName);
            }
        } catch (Exception ignored) {}
        limits = l;
        return l;
    }

    private String configName;

    /** Конфиг собираем как сам hippo: общий файл + папка модели + уточнения по Build.DEVICE / ro.boot.dlpModel / ревизии железа. */
    private String readHippoConfig() {
        try {
            ApplicationInfo ai = ctx.getPackageManager().getApplicationInfo("com.jmgo.hippo", 0);
            HippoConfig.Result r = HippoConfig.load(ai.sourceDir,
                    new String[]{prop("ro.jmgo.product.device"), Build.DEVICE, model(), Build.MODEL},
                    Build.DEVICE, prop("ro.boot.dlpModel"), prop("ro.boot.hardware_version"));
            if (r == null) return null;
            StringBuilder n = new StringBuilder(r.dir);
            for (String f : r.files) {
                int i = f.indexOf("hippo_configs-");
                if (f.startsWith(r.dir + "/") && i >= 0) n.append(" + ").append(f.substring(i + 14, f.length() - 4));
            }
            configName = n.toString();
            return r.xml;
        } catch (Exception e) { return null; }
    }

    private double[] dmdMux = new double[0], dmdDlpCfg = new double[0];
    /** Индексы датчиков лазера из конфига модели (10, 11, 12, 14 = модули 1–4); пусто — показываем все, что отдал сервис. */
    private final java.util.Set<Integer> laserIdx = new java.util.TreeSet<>();
    private static final int[] LASER_SENSOR = {0, 10, 11, 12, 14};
    private final List<int[]> modeCurrents = new ArrayList<>();

    private static double[] list(String xml, String key) {
        Matcher m = Pattern.compile("key=\"" + key + "\"\\s+value=\"([^\"]*)\"").matcher(xml);
        if (!m.find()) return new double[0];
        Matcher n = Pattern.compile("-?\\d+(?:\\.\\d+)?").matcher(m.group(1));
        List<Double> v = new ArrayList<>();
        while (n.find()) v.add(Double.parseDouble(n.group()));
        double[] a = new double[v.size()];
        for (int i = 0; i < a.length; i++) a[i] = v.get(i);
        return a;
    }

    /** '6.0.0' -> 600, как в прошивке. */
    private static int dlpVersionInt(String v) {
        if (v == null) return 0;
        Matcher m = Pattern.compile("\\d+").matcher(v);
        int[] k = {100, 10, 1}; int r = 0, i = 0;
        while (m.find() && i < 3) r += Integer.parseInt(m.group()) * k[i++];
        return r;
    }

    /** Повторяет TemperaturePolicy.isDmdTemperatureByDlp() из hippo. */
    private boolean dmdByDlp(String dlpVersion) {
        double[] x = dmdMux;
        // sar −2 (Iris Ultra/Ultra Max, hippo 9.4.x): DMD всегда из DLP, без проверки версии
        if (x.length > 0 && x[0] == -2) return true;
        if (x.length == 0 || x[0] < 0) return false;
        if (x.length >= 7 && x[6] > 0) {
            int ver = dlpVersionInt(dlpVersion);
            if (ver >= x[6]) return true;
            if (ver > 0) return false;
        }
        return x.length >= 6 && x[5] == 1;
    }

    private static List<double[]> rows(String xml, String key) {
        List<double[]> out = new ArrayList<>();
        Matcher m = Pattern.compile("key=\"" + key + "\"[^>]*>(.*?)</setting-2-dimen-array>", Pattern.DOTALL).matcher(xml);
        if (!m.find()) return out;
        Matcher it = Pattern.compile("<item>(.*?)</item>", Pattern.DOTALL).matcher(m.group(1));
        Pattern numP = Pattern.compile("-?\\d+(?:\\.\\d+)?");
        while (it.find()) {
            Matcher n = numP.matcher(it.group(1));
            List<Double> v = new ArrayList<>();
            while (n.find()) v.add(Double.parseDouble(n.group()));
            double[] a = new double[v.size()];
            for (int i = 0; i < a.length; i++) a[i] = v.get(i);
            if (a.length > 0) out.add(a);
        }
        return out;
    }

    /** 0..100 из сервиса -> пункт меню 1..10 */
    static int menuLevel(int pct) { return Math.max(1, Math.min(10, Math.round(pct * 9 / 100f) + 1)); }

    /** Ступень 1..10 по току красного лазера (обратная к DISPLAY_BRIGHTNESS_INTERPOLATOR). */
    static Integer levelFromCurrent(int r, JSONArray interp) {
        if (interp == null || interp.length() < 2) return null;
        int n = interp.length();
        if (r <= interp.optInt(0)) return 1;
        if (r >= interp.optInt(n - 1)) return n;
        for (int i = 0; i < n - 1; i++) {
            int a = interp.optInt(i), b = interp.optInt(i + 1);
            if (r >= a && r <= b) return Math.round(i + 1 + (b == a ? 0 : (r - a) / (float) (b - a)));
        }
        return null;
    }

    /** Загрузка процессора по /proc/stat между двумя опросами; -1, если файл закрыт (SELinux). */
    private int cpuUsage() {
        String line = readFile("/proc/stat");
        if (line == null || !line.startsWith("cpu ")) return -1;
        String[] p = line.trim().split("\\s+");
        if (p.length < 8) return -1;
        long[] v = new long[p.length - 1];
        for (int i = 1; i < p.length; i++) { try { v[i - 1] = Long.parseLong(p[i]); } catch (Exception e) { return -1; } }
        long idle = v[3] + (v.length > 4 ? v[4] : 0), total = 0;
        for (int i = 0; i < Math.min(8, v.length); i++) total += v[i];
        long[] cur = {total, idle};
        long[] prev = prevCpu; prevCpu = cur;
        if (prev == null || cur[0] <= prev[0]) return -1;
        long dt = cur[0] - prev[0], di = cur[1] - prev[1];
        return (int) Math.max(0, Math.min(100, Math.round((dt - di) * 100.0 / dt)));
    }

    // ------------------------------------------------------------------ снимок

    private JSONObject snapshot() throws Exception {
        JSONObject s = new JSONObject();
        JSONObject lim = limits();
        String model = model();
        s.put("ts", now());
        s.put("model", model);
        s.put("name", modelName(model));
        s.put("firmware_version", Build.VERSION.INCREMENTAL);
        // версия DLP не меняется до перезагрузки: читаем один раз (это 3 запроса в DLP с паузами)
        if (dlpVersion == null) dlpVersion = readFile("/sys/class/jmgo_dlp/version");
        String dlpv = dlpVersion;
        if (dlpv != null) s.put("dlp_version", dlpv);
        s.put("dlp_model", prop("vendor.jmgo.dlp.model"));
        s.put("uptime", SystemClock.elapsedRealtime() / 1000);

        // температуры DMD и лазеров
        JSONObject t = new JSONObject(getString(IDX_TEMPS));
        TreeMap<Integer, Double> lasers = new TreeMap<>();
        Double dmd = null;
        Iterator<String> keys = t.keys();
        Pattern lp = Pattern.compile("laser(\\d+)_temperatures");
        while (keys.hasNext()) {
            String k = keys.next();
            Matcher m = lp.matcher(k);
            if (m.matches()) {
                int n = Integer.parseInt(m.group(1));
                // 3-лазерные модели: модуль, которого нет в конфиге, не показываем
                if (!laserIdx.isEmpty() && n >= 1 && n <= 4 && !laserIdx.contains(LASER_SENSOR[n])) continue;
                Double v = num(t.optString(k)); if (v != null) lasers.put(n, v);
            }
            else if (k.equals("dmd_temperatures")) dmd = num(t.optString(k));
        }
        JSONObject th = new JSONObject();
        JSONArray la = new JSONArray();
        Double lmax = null;
        for (Double v : lasers.values()) { la.put(v); lmax = lmax == null ? v : Math.max(lmax, v); }
        th.put("fresh", true); th.put("age", 0);
        th.put("dmd", dmd == null ? JSONObject.NULL : dmd);
        th.put("lasers", la);
        th.put("laser_max", lmax == null ? JSONObject.NULL : lmax);
        // объектив: датчик у оптического блока (hippo getLensTemperature), без порога и без влияния на защиту
        try { Double lens = num(getString(IDX_LENS)); if (lens != null) th.put("lens", lens); } catch (Exception ignored) {}
        int[] duty = getArray(IDX_FAN_DUTY);
        if (duty != null) { JSONArray d = new JSONArray(); for (int v : duty) d.put(v); th.put("fan_duty", d); }
        Integer fanMode = getInt(IDX_FAN_MODE);
        th.put("fan_mode", fanMode != null && fanMode == 0 ? "PID" : "manual");
        JSONObject fc = null;
        Integer fcNow = null;
        if (fanMode != null && fanMode == 0 && fanAuto > 0 && fanCtlModel(model)) {
            fcNow = getInt(IDX_FAN_TARGET);
            fc = new JSONObject();
            fc.put("auto", fanAuto);
            JSONArray o = new JSONArray();
            for (int v : fanOpts) o.put(v);
            fc.put("opts", o);
        }
        Integer ov = getInt(IDX_OVERLAP_LEVEL);
        th.put("overlap_level", ov == null ? 0 : ov);
        // статус вентиляторов (0x76) не запрашиваем: прошивка при этом еще раз читает тахометр
        s.put("thermal", th);
        JSONObject limOut = new JSONObject(lim.toString());
        boolean byDlp = dmdByDlp(dlpv);
        // KD11CN: свой порог DMD_TEMPERATURE_CHECK_CONFIG; KD11AX: его нет, порог уже в строке {7, 71, -1}
        limOut.put("dmd_source", byDlp ? "dlp" : "ntc");
        if (byDlp && dmdDlpCfg.length > 0 && dmdDlpCfg[0] > 0) limOut.put("dmd_shutdown", dmdDlpCfg[0]);
        s.put("limits", limOut);
        lim = limOut;

        // обороты вентиляторов (если прошивка дает приложению читать файл): раз в 5 с и только когда их видно
        String tach = null;
        long nowMs = SystemClock.elapsedRealtime();
        if (fastUsers > 0 && nowMs >= nextTachAt) {
            nextTachAt = nowMs + TACH_PERIOD_MS - 500 + rnd.nextInt(1001);
            tach = readFile("/sys/class/jmgo_fan/tachometer");
        }
        if (tach == null) {
            // между чтениями показываем последнее значение; в фоне (только предупреждения) оборотов нет
            if (fastUsers > 0 && lastFan1 != null) s.put("fan1", lastFan1);
            if (fastUsers > 0 && lastFan2 != null) s.put("fan2", lastFan2);
        } else {
            String[] p = tach.trim().split("\\s+");
            try {
                // одиночные «0 об/мин» при работающей системе — сбой чтения тахометра, а не остановка
                Integer f1 = p.length > 0 ? Integer.parseInt(p[0]) : null, f2 = p.length > 1 ? Integer.parseInt(p[1]) : null;
                // вместо сбойного нуля повторяем прошлое значение (на графике без разрыва); 3 нуля подряд — вентилятор правда стоит
                if (f1 != null && f1 == 0 && lastFan1 != null && lastFan1 > 300 && ++zeroFan1 < 3) f1 = lastFan1; else if (f1 != null) { lastFan1 = f1; if (f1 != 0) zeroFan1 = 0; }
                if (f2 != null && f2 == 0 && lastFan2 != null && lastFan2 > 300 && ++zeroFan2 < 3) f2 = lastFan2; else if (f2 != null) { lastFan2 = f2; if (f2 != 0) zeroFan2 = 0; }
                if (f1 != null) s.put("fan1", f1);
                if (f2 != null) s.put("fan2", f2);
            } catch (Exception ignored) {}
        }

        // яркость: уровень из меню и режим берем у сервиса JMGO, ток лазеров — из драйвера DLP
        Integer bPct = getInt(IDX_BRIGHTNESS);
        Integer bMode = getInt(IDX_BRIGHTNESS_MODE);
        if (bPct != null && bPct >= 0) s.put("brightness_pct", bPct);
        if (bMode != null) s.put("brightness_mode", bMode);
        try { s.put("bw_status", Integer.parseInt(prop("sys.jmgo.black_white_status").trim())); } catch (Exception ignored) {}
        String br = readFile("/sys/class/jmgo_dlp/brightness");
        Matcher bm = br == null ? null : Pattern.compile("R(\\d+)G(\\d+)B(\\d+)").matcher(br);
        if (bm != null && bm.find()) {
            int[] cur = {Integer.parseInt(bm.group(1)), Integer.parseInt(bm.group(2)), Integer.parseInt(bm.group(3))};
            JSONArray vmax = lim.optJSONArray("value_max");
            JSONArray pct = new JSONArray();
            for (int i = 0; i < 3; i++) {
                int mx = vmax != null && vmax.length() > i ? vmax.optInt(i, 1023) : 1023;
                pct.put(Math.round(cur[i] * 100f / (mx == 0 ? 1023 : mx)));
            }
            s.put("laser_current_pct", pct);
            // фиксированные режимы (Overlap, производительность, белый экран) — по точному току из таблицы прошивки
            if (bMode == null || bMode < 0) {
                for (int[] row : modeCurrents)
                    if (Math.abs(cur[0] - row[1]) <= 2 && Math.abs(cur[1] - row[2]) <= 2 && Math.abs(cur[2] - row[3]) <= 2) { s.put("brightness_mode", row[0]); break; }
            }
        }
        // Ступени меню 1..10 сервис хранит как (N-1)*100/9: 0, 11, 22 … 88, 100 (так же строит BrightnessInterpolator в hippo)
        if (bPct != null && bPct >= 0) { s.put("brightness_menu", menuLevel(bPct)); s.put("brightness_exact", true); }
        Integer ap = getInt(IDX_APERTURE);
        if (ap != null && ap > 0) s.put("aperture", ap / 100.0);
        int modeNow = s.optInt("brightness_mode", -1);
        boolean fixedMode = modeNow == 15 || modeNow == 16 || modeNow == 22 || modeNow == 23;
        Integer curLevel = null;
        if (bm != null && !fixedMode) {
            bm.reset();
            if (bm.find()) curLevel = levelFromCurrent(Integer.parseInt(bm.group(1)), lim.optJSONArray("interp_r"));
        }
        if (!s.has("brightness_menu") && curLevel != null) { s.put("brightness_menu", curLevel); s.put("brightness_exact", false); }
        if (fc != null) {
            // две цели: для яркости 1–6 и 7–10 (ступень меню; с AL 2.0 меню стоит на 10 — «яркая»)
            loadFanPrefs();
            Integer lo2 = nearestOpt(fanLo), hi2 = nearestOpt(fanHi);
            if (!java.util.Objects.equals(lo2, fanLo) || !java.util.Objects.equals(hi2, fanHi)) {
                fanLo = lo2; fanHi = hi2;
                prefs().edit().putInt("fan_lo", lo2 == null ? 0 : lo2).putInt("fan_hi", hi2 == null ? 0 : hi2).apply();
            }
            if (fanLo != null || fanHi != null || fanApplied != null) fcNow = applyFanTarget(s, fcNow, false);
            boolean hi = useHi(s);
            Integer want = hi ? fanHi : fanLo;
            fc.put("now", fcNow == null ? -1 : fcNow);
            fc.put("lo", fanLo == null ? 0 : fanLo);
            fc.put("hi", fanHi == null ? 0 : fanHi);
            fc.put("use", hi ? "hi" : "lo");
            fc.put("boot", fanBoot);
            fc.put("level", FAN_HI_LEVEL);
            if (want != null) fc.put("custom", want);
            s.put("fan_ctl", fc);
        }
        // AL 2.0 / Dynamic Black: флаг, который пишет сама прошивка; мощность лазеров меняется по сцене — показываем ступень по току
        int al = -1;
        try { al = android.provider.Settings.System.getInt(ctx.getContentResolver(), "dynamic_black_enabled", -1); } catch (Throwable ignored) {}
        if (al >= 0) s.put("al_enabled", al == 1);
        if (curLevel != null && (al == 1 || (al < 0 && curLevel != s.optInt("brightness_menu", curLevel)))) s.put("brightness_actual", curLevel);

        // процессор
        Integer cpu = readInt("/sys/class/thermal/thermal_zone0/temp");
        if (cpu != null) s.put("temp", cpu / 1000);
        Integer trip = readInt("/sys/class/thermal/thermal_zone0/trip_point_0_temp");
        s.put("trip_passive0", trip != null ? trip / 1000 : 123);
        String plat = prop("ro.jmgo.platform").trim();
        if (!plat.isEmpty()) s.put("chip", "MediaTek " + plat.toUpperCase(Locale.ROOT));
        Integer freq = readInt("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq");
        Integer fHw = readInt("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq");
        Integer fCap = readInt("/sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq");
        Integer cool = readInt("/sys/class/thermal/cooling_device0/cur_state");
        if (freq != null) s.put("freq", freq / 1000);
        if (fHw != null) s.put("freq_hw", fHw / 1000);
        if (fCap != null) s.put("freq_cap", fCap / 1000);
        Integer fMin = readInt("/sys/devices/system/cpu/cpu0/cpufreq/scaling_min_freq");
        if (fMin != null) s.put("freq_min", fMin / 1000);
        if (freq != null && fHw != null && fHw > 0) s.put("freq_pct", Math.round(freq * 100f / fHw));
        // Троттлинг — только когда процессор правда горячий (не дальше 15 °C до порога): на платформе N3 / IRIS
        // потолок частоты и cooling_device0 бывают «ниже нормы» и при 60–70 °C. Потолок сравниваем с наибольшим
        // за сеанс, охлаждение учитываем только процессорное; состояние меняется после 2 опросов подряд.
        if (coolType == null) { String ct = readFile("/sys/class/thermal/cooling_device0/type"); coolType = ct == null ? "" : ct.trim().toLowerCase(Locale.ROOT); }
        if (fCap != null && (capBase == null || fCap > capBase)) capBase = fCap;
        int tripC = trip != null ? trip / 1000 : 123;
        boolean hot = cpu != null && cpu / 1000 >= tripC - 15;
        boolean rawThr = hot && ((fCap != null && capBase != null && fCap < capBase) || (coolType.contains("cpu") && cool != null && cool > 0));
        if (rawThr != thrState) { if (++thrStreak >= 2) { thrState = rawThr; thrStreak = 0; } } else thrStreak = 0;
        boolean thr = thrState;
        long nowS = now();
        if (thr) { if (throttleSince == null) throttleSince = nowS; if (throttleFirst == null) throttleFirst = nowS; }
        else throttleSince = null;
        s.put("throttled", thr);
        if (throttleSince != null) s.put("throttle_since", throttleSince);
        if (throttleFirst != null) s.put("throttle_first", throttleFirst);
        s.put("cpu_usage", cpuUsage());

        // память, накопитель, вывод
        ActivityManager am = (ActivityManager) ctx.getSystemService(Context.ACTIVITY_SERVICE);
        ActivityManager.MemoryInfo mi = new ActivityManager.MemoryInfo();
        am.getMemoryInfo(mi);
        s.put("mem_total", mi.totalMem / 1024); s.put("mem_avail", mi.availMem / 1024);
        StatFs st = new StatFs(Environment.getDataDirectory().getPath());
        s.put("storage_free_kb", st.getAvailableBytes() / 1024);
        s.put("storage_total_kb", st.getTotalBytes() / 1024);
        try {
            Display disp = ((WindowManager) ctx.getSystemService(Context.WINDOW_SERVICE)).getDefaultDisplay();
            Display.Mode mode = disp.getMode();
            s.put("output_width", mode.getPhysicalWidth()); s.put("output_height", mode.getPhysicalHeight());
            s.put("output_hz", Math.round(mode.getRefreshRate()));
        } catch (Throwable ignored) {}

        // сигнал на HDMI (если сейчас выбран HDMI-вход и есть сигнал)
        Object locked = srcCall(15, false);
        if (locked instanceof Integer && (Integer) locked != 0) {
            Object rs = srcCall(1, true);
            Object fps = srcCall(7, false);
            Object vrr = srcCall(14, false);
            if (rs != null) s.put("input_rslt", rs.toString().trim());
            if (fps instanceof Integer && (Integer) fps > 0) s.put("input_fps", fps);
            if (vrr instanceof Integer) s.put("input_vrr", vrr);
        }
        if (dmd != null && (maxDmd == null || dmd > maxDmd)) maxDmd = dmd;
        if (lmax != null && (maxLaser == null || lmax > maxLaser)) maxLaser = lmax;
        if (cpu != null && (maxCpu == null || cpu / 1000 > maxCpu)) maxCpu = cpu / 1000;
        JSONObject mx = new JSONObject();
        if (maxDmd != null) mx.put("dmd", maxDmd);
        if (maxLaser != null) mx.put("laser", maxLaser);
        if (maxCpu != null) mx.put("cpu", maxCpu);
        s.put("session_max", mx);
        s.put("session_start", sessionStart);
        s.put("extra_zones", new JSONArray());
        try { s.put("app_version", ctx.getPackageManager().getPackageInfo(ctx.getPackageName(), 0).versionName); } catch (Exception ignored) {}
        return s;
    }
}
