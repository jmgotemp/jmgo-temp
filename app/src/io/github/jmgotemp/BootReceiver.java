package io.github.jmgotemp;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/**
 * После включения проектора: если включено «Применять после включения проектора» и выбрана своя цель
 * вентиляторов, запускает фоновую службу — она ставит выбранные цели. Иначе ничего не делает.
 */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context c, Intent i) {
        if (!Monitor.bootTargetSaved(c)) return;
        try { c.startForegroundService(new Intent(c, WatchService.class)); } catch (Throwable ignored) {}
    }
}
