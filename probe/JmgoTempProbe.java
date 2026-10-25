import android.os.IBinder;
import android.os.Parcel;
import java.lang.reflect.Method;

/**
 * JMGO Temp — помощник браузерной версии. Запускается на проекторе через adb:
 *   CLASSPATH=/data/local/tmp/jmgotemp_probe.dex app_process /system/bin JmgoTempProbe [секунд]
 * Каждые N секунд (по умолчанию 2) печатает одну строку JSON с теми же данными, что берет приложение
 * у сервиса JMGO (jmgomiddle-01 → DLP manager). ТОЛЬКО ЧТЕНИЕ: вызываются лишь get / getString / getArray.
 * Тахометр (/sys/class/jmgo_fan/tachometer) не читается: на N5S Ultra Max / IRIS это сбивает проверку
 * вентиляторов прошивки (ошибка 4001). Завершается сам, когда adb закрывает соединение (stdout ломается).
 */
public class JmgoTempProbe {
    static final String SYS = "com.jmgo.middleware.service.IJmGOSystemService";
    static final String DLP = "com.jmgo.middleware.dlp.IJmGODlpManagerService";
    static final int T_GET = 2, T_GET_STRING = 4, T_GET_ARRAY = 6;
    static final int IDX_TEMPS = 0x105, IDX_FAN_DUTY = 0xa7, IDX_FAN_MODE = 0x70, IDX_OVERLAP = 0x334,
            IDX_LENS = 0x138, IDX_BRIGHTNESS = 0x200, IDX_BRIGHTNESS_MODE = 0x64, IDX_APERTURE = 0x213;
    static IBinder dlp;

    static IBinder connect() throws Exception {
        Method gs = Class.forName("android.os.ServiceManager").getDeclaredMethod("getService", String.class);
        IBinder sys = (IBinder) gs.invoke(null, "jmgomiddle-01");
        if (sys == null) throw new IllegalStateException("no_service");
        Parcel d = Parcel.obtain(), r = Parcel.obtain();
        try {
            d.writeInterfaceToken(SYS);
            sys.transact(5, d, r, 0);
            r.readException();
            IBinder b = r.readStrongBinder();
            if (b == null) throw new IllegalStateException("no_dlp");
            return b;
        } finally { d.recycle(); r.recycle(); }
    }

    static Parcel call(int code, int idx) throws Exception {
        Parcel d = Parcel.obtain(), r = Parcel.obtain();
        try {
            d.writeInterfaceToken(DLP);
            d.writeInt(idx);
            dlp.transact(code, d, r, 0);
            r.readException();
            return r;
        } catch (Exception e) { r.recycle(); throw e; } finally { d.recycle(); }
    }

    static String getInt(int idx) {
        try { Parcel r = call(T_GET, idx); int v = r.readInt(); r.recycle(); return String.valueOf(v); }
        catch (Exception e) { return "null"; }
    }

    static String getString(int idx) {
        try { Parcel r = call(T_GET_STRING, idx); String v = r.readString(); r.recycle(); return v == null ? "null" : quote(v); }
        catch (Exception e) { return "null"; }
    }

    static String getArray(int idx) {
        try {
            Parcel r = call(T_GET_ARRAY, idx); int[] a = r.createIntArray(); r.recycle();
            if (a == null) return "null";
            StringBuilder sb = new StringBuilder("[");
            for (int i = 0; i < a.length; i++) { if (i > 0) sb.append(','); sb.append(a[i]); }
            return sb.append(']').toString();
        } catch (Exception e) { return "null"; }
    }

    static String quote(String s) {
        StringBuilder sb = new StringBuilder("\"");
        for (char c : s.toCharArray()) {
            if (c == '"' || c == '\\') sb.append('\\').append(c);
            else if (c < 0x20) sb.append(String.format("\\u%04x", (int) c));
            else sb.append(c);
        }
        return sb.append('"').toString();
    }

    public static void main(String[] args) throws Exception {
        long period = 2000;
        if (args.length > 0) try { period = Math.max(1, Math.min(60, Integer.parseInt(args[0]))) * 1000L; } catch (NumberFormatException ignored) {}
        while (true) {
            String line;
            try {
                if (dlp == null || !dlp.isBinderAlive()) dlp = connect();
                line = "{\"ts\":" + System.currentTimeMillis()
                        + ",\"temps\":" + getString(IDX_TEMPS)
                        + ",\"lens\":" + getString(IDX_LENS)
                        + ",\"fan_duty\":" + getArray(IDX_FAN_DUTY)
                        + ",\"fan_mode\":" + getInt(IDX_FAN_MODE)
                        + ",\"overlap\":" + getInt(IDX_OVERLAP)
                        + ",\"brightness\":" + getInt(IDX_BRIGHTNESS)
                        + ",\"brightness_mode\":" + getInt(IDX_BRIGHTNESS_MODE)
                        + ",\"aperture\":" + getInt(IDX_APERTURE) + "}";
            } catch (Exception e) {
                dlp = null;
                line = "{\"ts\":" + System.currentTimeMillis() + ",\"error\":" + quote(String.valueOf(e)) + "}";
            }
            System.out.println(line);
            if (System.out.checkError()) return;  // adb закрыт — выходим
            Thread.sleep(period);
        }
    }
}
