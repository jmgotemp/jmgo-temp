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
        handleIntent(getIntent());
    }

    @Override
    protected void onNewIntent(Intent i) { super.onNewIntent(i); handleIntent(i); }

    /** am start -n <пакет>/io.github.jmgotemp.MainActivity --es overlay on|off  — включить/выключить окно без пульта.
     *  Пакет с 1.3: com.jmgo.jmgotemp (hippo не закрывает приложения com.jmgo.*), до 1.3: io.github.jmgotemp. */
    private void handleIntent(Intent i) {
        if (i == null || !i.hasExtra("overlay")) return;
        boolean on = "on".equals(i.getStringExtra("overlay"));
        Intent s = new Intent(this, OverlayService.class);
        prefs().edit().putBoolean("enabled", on).apply();
        if (on) startForegroundService(s); else stopService(s);
        if (i.getBooleanExtra("finish", false)) finish();
    }

    @Override protected void onStart() { super.onStart(); monitor.acquire(); }
    @Override protected void onStop() { monitor.release(); super.onStop(); }
    // пока открыт полный экран, окно поверх не нужно — прячем его, чтобы не закрывало заголовок
    @Override protected void onResume() { super.onResume(); OverlayService.setHidden(true); }
    @Override protected void onPause() { OverlayService.setHidden(false); super.onPause(); }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK) { finish(); return true; }
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

        @JavascriptInterface public String getLang() { return prefs().getString("lang", ""); }

        @JavascriptInterface public void setLang(String l) {
            prefs().edit().putString("lang", l).apply();
            if (OverlayService.running) startForegroundService(new Intent(MainActivity.this, OverlayService.class).setAction("reposition"));
        }

        @JavascriptInterface public boolean canOverlay() { return Settings.canDrawOverlays(MainActivity.this); }

        @JavascriptInterface public boolean overlayOn() { return OverlayService.running; }

        @JavascriptInterface public String corner() { return prefs().getString("corner", "tr"); }

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
