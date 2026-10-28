package io.github.jmgotemp;

import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.provider.Settings;
import android.view.KeyEvent;
import android.webkit.JavascriptInterface;
import android.webkit.WebSettings;
import android.webkit.WebView;

public class MainActivity extends Activity {
    private WebView web;
    private Monitor monitor;

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        monitor = Monitor.get(this);
        web = new WebView(this);
        WebSettings ws = web.getSettings();
        ws.setJavaScriptEnabled(true);
        ws.setDomStorageEnabled(true);
        web.setBackgroundColor(0xFF0F1115);
        web.addJavascriptInterface(new Bridge(), "Android");
        web.setFocusable(true);
        web.setFocusableInTouchMode(true);
        setContentView(web);
        web.loadUrl("file:///android_asset/dashboard.html");
        web.requestFocus();
        // фоновые предупреждения о перегреве: работают, пока приложение в памяти после выхода с экрана
        // своя цель вентиляторов тоже держит службу (ставит цель по яркости и после белого экрана)
        if (prefs().getBoolean("watch", true) || monitor.hasFanTarget()) startForegroundService(new Intent(this, WatchService.class));
        if (!Updater.get(this).onStatus(this, getIntent())) handleIntent(getIntent());
    }

    @Override
    protected void onNewIntent(Intent i) { super.onNewIntent(i); if (!Updater.get(this).onStatus(this, i)) handleIntent(i); }

    /** am start -n <пакет>/io.github.jmgotemp.MainActivity --es overlay on|off  — включить/выключить компактный режим без пульта.
     *  Пакет с 1.3: com.jmgo.jmgotemp (hippo не закрывает приложения com.jmgo.*), до 1.3: io.github.jmgotemp. */
    private void handleIntent(Intent i) {
        // --es watch test — показать пробное предупреждение (проверка вида), --es watch off|on — выключить/включить
        if (i != null && i.hasExtra("watch")) {
            String w = i.getStringExtra("watch");
            Intent s = new Intent(this, WatchService.class);
            // служба нужна и без предупреждений, если выбрана своя температура вентиляторов
            if ("off".equals(w)) { prefs().edit().putBoolean("watch", false).apply(); if (!monitor.hasFanTarget()) stopService(s); }
            else { prefs().edit().putBoolean("watch", true).apply(); startForegroundService("test".equals(w) ? s.setAction("test") : s); }
            if (i.getBooleanExtra("finish", false)) finish();
            return;
        }
        if (i == null || !i.hasExtra("overlay")) return;
        boolean on = "on".equals(i.getStringExtra("overlay"));
        Intent s = new Intent(this, OverlayService.class);
        prefs().edit().putBoolean("enabled", on).apply();
        if (on) startForegroundService(s); else stopService(s);
        if (i.getBooleanExtra("finish", false)) finish();
    }

    @Override protected void onStart() { super.onStart(); monitor.acquire(); }
    @Override protected void onStop() { monitor.release(); super.onStop(); }
    // пока открыт полный экран, компактный режим не нужен — прячем его, чтобы не закрывал заголовок
    // проверка обновлений: не чаще раза в сутки и только когда открыт экран приложения
    @Override protected void onResume() { super.onResume(); OverlayService.setHidden(true); WatchService.appVisible = true; Updater.get(this).autoCheck(); }
    @Override protected void onPause() { WatchService.appVisible = false; OverlayService.setHidden(false); super.onPause(); }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK) {
            // «Назад» сначала закрывает окно «Поддержать» (QR), и только потом выходит из приложения
            web.evaluateJavascript("typeof onBack==='function'&&onBack()", v -> { if (!"true".equals(v)) finish(); });
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    private SharedPreferences prefs() { return getSharedPreferences("overlay", MODE_PRIVATE); }

    final class Bridge {
        @JavascriptInterface public String api() { return monitor.apiJson(); }

        @JavascriptInterface public String appVersion() {
            try { return getPackageManager().getPackageInfo(getPackageName(), 0).versionName; }
            catch (Exception e) { return ""; }
        }

        @JavascriptInterface public String pkg() { return getPackageName(); }

        /** Обновления: состояние {cur, latest, newer, checked, phase, err, progress}, проверка и установка */
        @JavascriptInterface public String updState() { return Updater.get(MainActivity.this).stateJson(); }
        @JavascriptInterface public void updCheck() { Updater.get(MainActivity.this).check(); }
        @JavascriptInterface public String updInstall() { return Updater.get(MainActivity.this).install(); }

        @JavascriptInterface public String getLang() { return prefs().getString("lang", ""); }

        @JavascriptInterface public void setLang(String l) {
            prefs().edit().putString("lang", l).apply();
            if (OverlayService.running) startForegroundService(new Intent(MainActivity.this, OverlayService.class).setAction("reposition"));
        }

        @JavascriptInterface public boolean canOverlay() { return Settings.canDrawOverlays(MainActivity.this); }

        @JavascriptInterface public boolean overlayOn() { return OverlayService.running; }

        @JavascriptInterface public String corner() { return prefs().getString("corner", "tr"); }

        /** Настройки фоновых предупреждений: {"watch":true,"w_cpu":true,…} */
        @JavascriptInterface public String warnPrefs() {
            StringBuilder sb = new StringBuilder("{\"watch\":").append(prefs().getBoolean("watch", true));
            for (String k : WatchService.KEYS) sb.append(",\"").append(k).append("\":").append(prefs().getBoolean(k, true));
            return sb.append('}').toString();
        }

        /** key = "all" — включить/выключить все предупреждения сразу. Если не включено ни одно,
         *  фоновая служба останавливается и после выхода приложение ничего не делает. */
        @JavascriptInterface public void setWarn(String key, boolean on) {
            SharedPreferences.Editor e = prefs().edit();
            if ("all".equals(key)) for (String k : WatchService.KEYS) e.putBoolean(k, on);
            else e.putBoolean(key, on);
            e.apply();
            boolean any = false;
            for (String k : WatchService.KEYS) any |= prefs().getBoolean(k, true);
            prefs().edit().putBoolean("watch", any).apply();
            Intent s = new Intent(MainActivity.this, WatchService.class);
            // своя цель вентиляторов тоже держит службу: она ставит цель снова после белого экрана
            if (any || monitor.hasFanTarget()) startForegroundService(s); else stopService(s);
        }

        /** Цели температуры для вентиляторов: lo — яркость 1–3, mid — 4–6, hi — 7–10; t > 0 — своя, 0 — как в прошивке. */
        @JavascriptInterface public String setFanTargets(int lo, int mid, int hi) {
            String r = monitor.setFanTargets(lo, mid, hi);
            Intent s = new Intent(MainActivity.this, WatchService.class);
            if (monitor.hasFanTarget()) startForegroundService(s);
            else if (!prefs().getBoolean("watch", true)) stopService(s);
            return r;
        }

        /** «Применять после включения проектора»: после загрузки приложение само запускается в фоне и ставит выбранные цели. */
        @JavascriptInterface public void setFanBoot(boolean on) { monitor.setFanBoot(on); }

        @JavascriptInterface public void testWarn() {
            startForegroundService(new Intent(MainActivity.this, WatchService.class).setAction("test"));
        }

        @JavascriptInterface public void toggleOverlay() {
            Intent i = new Intent(MainActivity.this, OverlayService.class);
            if (OverlayService.running) {
                prefs().edit().putBoolean("enabled", false).apply();
                stopService(i);
            } else {
                prefs().edit().putBoolean("enabled", true).apply();
                startForegroundService(i);
            }
        }

        @JavascriptInterface public void cycleCorner() {
            String[] order = {"tr", "br", "bl", "tl"};
            String c = corner(); int k = 0;
            for (int j = 0; j < order.length; j++) if (order[j].equals(c)) k = j;
            prefs().edit().putString("corner", order[(k + 1) % order.length]).apply();
            if (OverlayService.running) startForegroundService(new Intent(MainActivity.this, OverlayService.class).setAction("reposition"));
        }
    }
}
