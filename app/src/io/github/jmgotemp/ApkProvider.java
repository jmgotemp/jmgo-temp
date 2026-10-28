package io.github.jmgotemp;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.database.Cursor;
import android.database.MatrixCursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import android.provider.OpenableColumns;

import java.io.File;
import java.io.FileNotFoundException;

/** Отдает только скачанный APK обновления (cache/update.apk) установщику прошивки на чтение.
 *  Нужен, потому что установщик JMGO не понимает установку через PackageInstaller-сессию. */
public class ApkProvider extends ContentProvider {
    static final String AUTH = "com.jmgo.jmgotemp.apk";
    static Uri uri() { return Uri.parse("content://" + AUTH + "/update.apk"); }

    private File file() { return new File(getContext().getCacheDir(), "update.apk"); }

    @Override public boolean onCreate() { return true; }

    @Override public ParcelFileDescriptor openFile(Uri u, String mode) throws FileNotFoundException {
        if (!"update.apk".equals(u.getLastPathSegment())) throw new FileNotFoundException();
        return ParcelFileDescriptor.open(file(), ParcelFileDescriptor.MODE_READ_ONLY);
    }

    @Override public String getType(Uri u) { return "application/vnd.android.package-archive"; }

    @Override public Cursor query(Uri u, String[] p, String s, String[] a, String o) {
        MatrixCursor c = new MatrixCursor(new String[]{OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE});
        c.addRow(new Object[]{"JMGO-Temp-update.apk", file().length()});
        return c;
    }

    @Override public Uri insert(Uri u, ContentValues v) { return null; }
    @Override public int delete(Uri u, String s, String[] a) { return 0; }
    @Override public int update(Uri u, ContentValues v, String s, String[] a) { return 0; }
}
