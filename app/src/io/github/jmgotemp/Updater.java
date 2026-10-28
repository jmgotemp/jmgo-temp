package io.github.jmgotemp;

import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageInstaller;
import android.net.Uri;
import android.provider.Settings;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;

/** Обновления с GitHub (с 1.6). Сеть — только здесь: номер последнего релиза и, по кнопке, сам APK.
 *  Автопроверка — не чаще раза в сутки при открытии экрана приложения; в фоне ничего не делается.
 *  Никаких данных о проекторе не отправляется. Установку подтверждает пользователь (окно Android). */
final class Updater {
    static final String API = "https://api.github.com/repos/jmgotemp/jmgo-temp/releases/latest";
    static final String PAGE = "https://github.com/jmgotemp/jmgo-temp/releases/latest";
    static final String ACTION_STATUS = "io.github.jmgotemp.INSTALL_STATUS";
    private static final long DAY = 24L * 3600 * 1000;

    private static Updater instance;
    static synchronized Updater get(Context c) {
        if (instance == null) instance = new Updater(c.getApplicationContext());
        return instance;
    }

    private final Context ctx;
    // phase: idle | checking | download | install | error
    private volatile String phase = "idle", err = "";
    private volatile int progress = -1;

    private Updater(Context c) { ctx = c; }

    private SharedPreferences prefs() { return ctx.getSharedPreferences("overlay", Context.MODE_PRIVATE); }

    String current() {
        try { return ctx.getPackageManager().getPackageInfo(ctx.getPackageName(), 0).versionName; }
        catch (Exception e) { return ""; }
    }

    /** "1.6" > "1.5.2"; "v" в начале тега отбрасывается */
    static int cmp(String a, String b) {
        String[] x = a.replaceFirst("^[vV]", "").split("[^0-9]+"), y = b.replaceFirst("^[vV]", "").split("[^0-9]+");
        for (int i = 0; i < Math.max(x.length, y.length); i++) {
            int p = i < x.length && !x[i].isEmpty() ? Integer.parseInt(x[i]) : 0;
            int q = i < y.length && !y[i].isEmpty() ? Integer.parseInt(y[i]) : 0;
            if (p != q) return p < q ? -1 : 1;
        }
        return 0;
    }

    synchronized String stateJson() {
        try {
            SharedPreferences p = prefs();
            String latest = p.getString("upd_latest", ""), cur = current();
            JSONObject o = new JSONObject();
            o.put("cur", cur);
            o.put("latest", latest);
            o.put("newer", !latest.isEmpty() && cmp(latest, cur) > 0);
            o.put("checked", p.getLong("upd_checked", 0) / 1000);
            o.put("phase", phase);
            o.put("err", err);
            o.put("progress", progress);
            o.put("page", PAGE);
            o.put("can_install", true);
            // «Что нового» берем из описания релиза: английская часть, пустая строка, русская (как в наших релизах)
            o.put("notes", p.getString("upd_notes", ""));
            return o.toString();
        } catch (Exception e) { return "{}"; }
    }

    /** При открытии экрана: проверить, если с прошлой проверки прошли сутки */
    void autoCheck() {
        if (!"install".equals(phase) && !"download".equals(phase)) new File(ctx.getCacheDir(), "update.apk").delete();
        if ("install".equals(phase)) phase = "idle";  // вернулись из установщика (отмена) — снова показываем кнопку
        if (System.currentTimeMillis() - prefs().getLong("upd_checked", 0) >= DAY) check();
    }

    synchronized void check() {
        if ("checking".equals(phase) || "download".equals(phase)) return;
        phase = "checking"; err = "";
        new Thread(() -> {
            try {
                JSONObject r = new JSONObject(get(API));
                String tag = r.getString("tag_name").replaceFirst("^[vV]", "");
                String apk = "";
                JSONArray a = r.optJSONArray("assets");
                for (int i = 0; a != null && i < a.length(); i++) {
                    String n = a.getJSONObject(i).optString("name");
                    if (n.endsWith(".apk")) { apk = a.getJSONObject(i).optString("browser_download_url"); break; }
                }
                String body = r.optString("body", "");
                prefs().edit().putString("upd_latest", tag).putString("upd_apk", apk).putString("upd_notes", body)
                        .putLong("upd_checked", System.currentTimeMillis()).apply();
                Log.i("JmgoTemp", "update check: latest " + tag + ", installed " + current());
                phase = "idle";
            } catch (Throwable t) {
                Log.w("JmgoTemp", "update check failed: " + t);
                err = "net"; phase = "error";
            }
        }, "jt-update").start();
    }

    private static HttpURLConnection open(String url) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setConnectTimeout(10000);
        c.setReadTimeout(20000);
        c.setInstanceFollowRedirects(true);
        c.setRequestProperty("User-Agent", "JMGO-Temp");
        c.setRequestProperty("Accept", "application/vnd.github+json");
        return c;
    }

    private static String get(String url) throws Exception {
        HttpURLConnection c = open(url);
        try (InputStream in = c.getInputStream()) {
            if (c.getResponseCode() != 200) throw new IllegalStateException("HTTP " + c.getResponseCode());
            java.io.ByteArrayOutputStream b = new java.io.ByteArrayOutputStream();
            byte[] buf = new byte[8192]; int n;
            while ((n = in.read(buf)) > 0) b.write(buf, 0, n);
            return b.toString("UTF-8");
        } finally { c.disconnect(); }
    }

    /** Разрешено ли приложению ставить APK («Неизвестные источники» для JMGO Temp) */
    boolean canInstall() {
        try { return ctx.getPackageManager().canRequestPackageInstalls(); } catch (Throwable t) { return true; }
    }

    /** Открыть экран разрешения установки. false — на прошивке нет такого экрана. */
    boolean askPermission() {
        try {
            Intent i = new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:" + ctx.getPackageName()))
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            ctx.startActivity(i);
            return true;
        } catch (Throwable t) { Log.w("JmgoTemp", "no unknown-sources screen: " + t); return false; }
    }

    /** Скачать APK последнего релиза и передать установщику Android. */
    synchronized String install() {
        if ("download".equals(phase) || "install".equals(phase)) return "busy";
        String url = prefs().getString("upd_apk", "");
        if (url.isEmpty()) return "no_apk";
        // Экран «Неизвестные источники» есть не во всех прошивках (на N5 Pro его нет, установщик свой —
        // com.jmgo.PackageInstaller). Если экрана нет, все равно отдаем APK установщику: решит он.
        if (!canInstall() && askPermission()) { err = "perm"; phase = "error"; return "perm"; }
        phase = "download"; err = ""; progress = 0;
        new Thread(() -> {
            File f = new File(ctx.getCacheDir(), "update.apk");
            f.delete();
            try {
                HttpURLConnection c = open(url);
                c.setRequestProperty("Accept", "application/octet-stream");
                long total = c.getContentLengthLong(), done = 0;
                try (InputStream in = c.getInputStream(); OutputStream out = new FileOutputStream(f)) {
                    byte[] buf = new byte[16384]; int n;
                    while ((n = in.read(buf)) > 0) {
                        out.write(buf, 0, n); done += n;
                        if (total > 0) progress = (int) (done * 100 / total);
                    }
                } finally { c.disconnect(); }
                if (done < 10000) throw new IllegalStateException("short file " + done);
                phase = "install"; progress = 100;
                // Установщик прошивки JMGO (com.jmgo.PackageInstaller) не поддерживает сессии PackageInstaller
                // (нет CONFIRM_INSTALL), но принимает обычный INSTALL_PACKAGE с content://-файлом — как из файлового менеджера.
                Intent in = new Intent(Intent.ACTION_INSTALL_PACKAGE)
                        .setDataAndType(ApkProvider.uri(), "application/vnd.android.package-archive")
                        .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_ACTIVITY_NEW_TASK)
                        .putExtra(Intent.EXTRA_NOT_UNKNOWN_SOURCE, true)
                        .putExtra(Intent.EXTRA_INSTALLER_PACKAGE_NAME, ctx.getPackageName());
                ctx.startActivity(in);
                Log.i("JmgoTemp", "update: downloaded " + done + " bytes, installer started");
            } catch (Throwable t) {
                Log.w("JmgoTemp", "update install failed: " + t);
                err = "dl"; phase = "error";
            }
        }, "jt-update-dl").start();
        return "ok";
    }

    /** Ответ установщика (приходит в MainActivity). Возвращает true, если интент наш. */
    boolean onStatus(Context activity, Intent i) {
        if (i == null || !ACTION_STATUS.equals(i.getAction())) return false;
        int st = i.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE);
        Log.i("JmgoTemp", "update status " + st + " " + i.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE));
        if (st == PackageInstaller.STATUS_PENDING_USER_ACTION) {
            Intent confirm = i.getParcelableExtra(Intent.EXTRA_INTENT);
            if (confirm != null) { try { activity.startActivity(confirm); } catch (Throwable t) { err = "inst"; phase = "error"; } }
        } else if (st == PackageInstaller.STATUS_SUCCESS) {
            phase = "idle";
        } else {
            // отменили в окне Android или установка не прошла
            err = st == PackageInstaller.STATUS_FAILURE_ABORTED ? "cancel" : "inst"; phase = "error";
        }
        return true;
    }
}
