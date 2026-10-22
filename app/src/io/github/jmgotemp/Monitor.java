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
import java.util.Locale;
import java.util.TreeMap;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.zip.ZipEntry;
import java.util.zip.ZipFile;

/**
 * Только чтение. Данные берутся у системного сервиса JMGO (jmgomiddle-01 -> DLP manager),
 * того же, к которому обращаются штатные настройки и «Клон настроек».
 */
public final class Monitor {
    private static final String SYS = "com.jmgo.middleware.service.IJmGOSystemService";
    private static final String DLP = "com.jmgo.middleware.dlp.IJmGODlpManagerService";
    private static final int T_GET = 2, T_GET_STRING = 4, T_GET_ARRAY = 6;
    private static final int IDX_TEMPS = 0x105, IDX_FAN_DUTY = 0xa7, IDX_FAN_STATUS = 0x76,
            IDX_FAN_MODE = 0x70, IDX_BRIGHTNESS = 0x200, IDX_OVERLAP_LEVEL = 0x334,
            IDX_BRIGHTNESS_MODE = 0x64;

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
    private int users;
    private long lastHistory;
    private final long sessionStart = System.currentTimeMillis() / 1000;
    private Double maxDmd, maxLaser; private Integer maxCpu;
    private long[] prevCpu;
    private Long throttleSince, throttleFirst;
    private Integer lastFan1, lastFan2;

    private Monitor(Context c) { ctx = c; }

    public synchronized void addListener(Listener l) { listeners.add(l); }
    public synchronized void removeListener(Listener l) { listeners.remove(l); }

    public synchronized void acquire() {
        users++;
        if (thread == null) {
            thread = new Thread(this::loop, "jmgo-monitor");
            thread.setDaemon(true);
            thread.start();
        }
    }

    public synchronized void release() {
        users = Math.max(0, users - 1);
        if (users == 0 && thread != null) { thread.interrupt(); thread = null; }
    }

    private void loop() {
        while (!Thread.currentThread().isInterrupted()) {
            JSONObject snap;
            try { snap = snapshot(); } catch (Throwable t) {
                dlp = null;
                snap = new JSONObject();
                try { snap.put("error", "Нет связи с сервисом JMGO: " + t); snap.put("ts", now()); } catch (Exception ignored) {}
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
            try { Thread.sleep(1000); } catch (InterruptedException e) { return; }
        }
    }

    public JSONObject latest() { return latest; }

    public synchronized String apiJson() {
        try {
            JSONObject o = new JSONObject();
            o.put("latest", latest);
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
        if (sys == null) throw new IllegalStateException("сервис jmgomiddle-01 не найден (это не JMGO?)");
        Parcel d = Parcel.obtain(), r = Parcel.obtain();
        try {
            d.writeInterfaceToken(SYS);
            sys.transact(5, d, r, 0);
            r.readException();
            dlp = r.readStrongBinder();
        } finally { d.recycle(); r.recycle(); }
        if (dlp == null) throw new IllegalStateException("DLP-сервис недоступен");
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
                for (double[] r : check) {
                    if (r.length < 2) continue;
                    int idx = (int) r[0];
                    if (idx == 7) l.put("dmd_shutdown", r[1]);
                    else if (idx == 10 || idx == 11 || idx == 12 || idx == 14) {
                        ls = ls == null ? r[1] : Math.min(ls, r[1]);
                        if (r.length > 2 && r[2] > 0) ld = ld == null ? r[2] : Math.min(ld, r[2]);
                    }
                }
                if (ls != null) l.put("laser_shutdown", ls);
                if (ld != null) l.put("laser_derate", ld);
                List<double[]> pid = rows(xml, "FAN_PID_CONTROL_CONFIG");
                if (!pid.isEmpty()) {
                    JSONArray p = new JSONArray(), fx = new JSONArray();
                    for (double[] r : pid) if (r.length > 0) { p.put(r[0]); fx.put(r.length > 2 && r[1] >= r[2]); }
                    l.put("pid_targets", p);
                    l.put("pid_fixed", fx);
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
            }
        } catch (Exception ignored) {}
        limits = l;
        return l;
    }

    private String readHippoConfig() {
        try {
            ApplicationInfo ai = ctx.getPackageManager().getApplicationInfo("com.jmgo.hippo", 0);
            try (ZipFile z = new ZipFile(ai.sourceDir)) {
                ZipEntry e = z.getEntry("assets/" + model() + "/hippo_configs.xml");
                if (e == null) return null;
                try (InputStream in = z.getInputStream(e)) {
                    ByteArrayOutputStream bo = new ByteArrayOutputStream();
                    byte[] buf = new byte[8192]; int n;
                    while ((n = in.read(buf)) > 0) bo.write(buf, 0, n);
                    return bo.toString("UTF-8");
                }
            }
        } catch (Exception e) { return null; }
    }

    private double[] dmdMux = new double[0], dmdDlpCfg = new double[0];
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
        s.put("name", "K981CN".equals(model) ? "JMGO N5 Pro" : "KD11CN".equals(model) ? "JMGO N5S Ultra Max" : "JMGO " + model);
        s.put("firmware_version", Build.VERSION.INCREMENTAL);
        String dlpv = readFile("/sys/class/jmgo_dlp/version");
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
            if (m.matches()) { Double v = num(t.optString(k)); if (v != null) lasers.put(Integer.parseInt(m.group(1)), v); }
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
        int[] duty = getArray(IDX_FAN_DUTY);
        if (duty != null) { JSONArray d = new JSONArray(); for (int v : duty) d.put(v); th.put("fan_duty", d); }
        Integer fanMode = getInt(IDX_FAN_MODE);
        th.put("fan_mode", fanMode != null && fanMode == 0 ? "PID" : "manual");
        Integer ov = getInt(IDX_OVERLAP_LEVEL);
        th.put("overlap_level", ov == null ? 0 : ov);
        Integer fs = getInt(IDX_FAN_STATUS);
        if (fs != null) th.put("fan_status", fs);
        s.put("thermal", th);
        JSONObject limOut = new JSONObject(lim.toString());
        limOut.put("dmd_source", "ntc");
        if (dmdDlpCfg.length > 0 && dmdDlpCfg[0] > 0 && dmdByDlp(dlpv)) {
            limOut.put("dmd_shutdown", dmdDlpCfg[0]);
            limOut.put("dmd_source", "dlp");
        }
        s.put("limits", limOut);
        lim = limOut;

        // обороты вентиляторов (если прошивка даёт приложению читать файл)
        String tach = readFile("/sys/class/jmgo_fan/tachometer");
        if (tach != null) {
            String[] p = tach.trim().split("\\s+");
            try {
                // одиночные «0 об/мин» при работающей системе — сбой чтения тахометра, а не остановка
                Integer f1 = p.length > 0 ? Integer.parseInt(p[0]) : null, f2 = p.length > 1 ? Integer.parseInt(p[1]) : null;
                if (f1 != null && f1 == 0 && lastFan1 != null && lastFan1 > 300) f1 = null; else if (f1 != null) lastFan1 = f1;
                if (f2 != null && f2 == 0 && lastFan2 != null && lastFan2 > 300) f2 = null; else if (f2 != null) lastFan2 = f2;
                if (f1 != null) s.put("fan1", f1);
                if (f2 != null) s.put("fan2", f2);
            } catch (Exception ignored) {}
        }

        // яркость: уровень из меню и режим берём у сервиса JMGO, ток лазеров — из драйвера DLP
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
        if (bPct != null && bPct >= 0) s.put("brightness_menu", Math.max(1, Math.min(10, Math.round(bPct / 10f))));

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
        boolean thr = (fCap != null && fHw != null && fCap < fHw) || (cool != null && cool > 0);
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
