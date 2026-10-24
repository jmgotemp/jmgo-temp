package io.github.jmgotemp;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Enumeration;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.zip.ZipEntry;
import java.util.zip.ZipFile;

/**
 * Собирает настройки модели из APK com.jmgo.hippo так же, как сам hippo (XmlParser.parseAssetsConfigXml):
 * общий assets/hippo_configs.xml, поверх него assets/&lt;модель&gt;/hippo_configs.xml, затем уточнения
 * hippo_configs-device-*.xml (Build.DEVICE), -model-*.xml (ro.boot.dlpModel) и -hw-*.xml
 * (ro.boot.hardware_version). Более поздний файл целиком заменяет одноименный ключ.
 * Только java.* — проверяется на компьютере обычным java.
 */
final class HippoConfig {
    static final class Result {
        String dir;
        String xml;
        final List<String> files = new ArrayList<>();
    }

    private static final Pattern ELEM = Pattern.compile(
            "<(setting(?:-array|-2-dimen-array)?)\\s[^>]*?key=\"([^\"]+)\"[^>]*?(?:/>|>.*?</\\1\\s*>)", Pattern.DOTALL);

    private HippoConfig() {}

    /** candidates — возможные имена папки модели по порядку (ro.jmgo.product.device, Build.DEVICE, ro.boot.jmgo.model…). */
    static Result load(String apk, String[] candidates, String device, String dlpModel, String hw) throws IOException {
        try (ZipFile z = new ZipFile(apk)) {
            String dir = null;
            for (String c : candidates)
                if (c != null && !c.trim().isEmpty() && z.getEntry("assets/" + c.trim() + "/hippo_configs.xml") != null) { dir = c.trim(); break; }
            if (dir == null) return null;
            Result r = new Result();
            r.dir = dir;
            Map<String, String> m = new LinkedHashMap<>();
            merge(z, "assets/hippo_configs.xml", m, r);
            String base = "assets/" + dir + "/hippo_configs.xml";
            merge(z, base, m, r);
            List<String> extra = new ArrayList<>();
            String pre = "assets/" + dir + "/hippo_configs";
            for (Enumeration<? extends ZipEntry> en = z.entries(); en.hasMoreElements(); ) {
                String n = en.nextElement().getName();
                if (n.startsWith(pre) && n.endsWith(".xml") && !n.equals(base) && n.indexOf('/', pre.length()) < 0) extra.add(n);
            }
            Collections.sort(extra);
            String[][] kinds = {{"-device-", device}, {"-model-", dlpModel}, {"-hw-", hw}};
            for (String[] k : kinds) {
                String want = k[1] == null ? "" : k[1].trim();
                if (want.isEmpty()) continue;
                for (String n : extra) {
                    String f = n.substring(n.lastIndexOf('/') + 1);
                    if (!f.contains(k[0])) continue;
                    for (String t : f.split("[-.]")) if (t.equals(want)) { merge(z, n, m, r); break; }
                }
            }
            StringBuilder sb = new StringBuilder();
            for (String v : m.values()) sb.append(v).append('\n');
            r.xml = sb.toString();
            return r;
        }
    }

    private static void merge(ZipFile z, String name, Map<String, String> m, Result r) throws IOException {
        ZipEntry e = z.getEntry(name);
        if (e == null) return;
        String xml;
        try (InputStream in = z.getInputStream(e)) {
            ByteArrayOutputStream bo = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) > 0) bo.write(buf, 0, n);
            xml = bo.toString("UTF-8");
        }
        xml = xml.replaceAll("(?s)<!--.*?-->", "");  // в комментариях лежат старые/примерные значения
        Matcher el = ELEM.matcher(xml);
        while (el.find()) m.put(el.group(2), el.group(0));
        r.files.add(name.substring("assets/".length()));
    }
}
