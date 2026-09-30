package dev.opencodemesh.app;

import java.net.URI;
import java.net.URLDecoder;
import java.util.HashMap;
import java.util.Map;

/**
 * The contract of the APK-only UI bridge (bundled from android/ui.js).
 *
 * The page never gets a JavascriptInterface; it communicates exclusively by
 * attempting main-frame navigations against the fixed {@code ocm-app://}
 * scheme. This class strictly parses those navigations and decides, together
 * with container context supplied by MainActivity, whether an action may run.
 * The theme payload only ever carries r/g/b/a/dark integers, so no
 * credential-shaped value can enter or leave through this bridge.
 */
public final class AppScheme {
    public static final String SCHEME = "ocm-app";

    public enum Action { NONE, SETTINGS, RELOAD, THEME }

    public static final class Theme {
        public final int red;
        public final int green;
        public final int blue;
        public final int alpha;
        public final boolean dark;

        Theme(int red, int green, int blue, int alpha, boolean dark) {
            this.red = red;
            this.green = green;
            this.blue = blue;
            this.alpha = alpha;
            this.dark = dark;
        }
    }

    public static final class Request {
        public final Action action;
        public final Theme theme;

        Request(Action action, Theme theme) {
            this.action = action;
            this.theme = theme;
        }
    }

    private AppScheme() {
    }

    public static boolean isAppScheme(String url) {
        if (url == null) return false;
        try {
            return SCHEME.equalsIgnoreCase(URI.create(url).getScheme());
        } catch (IllegalArgumentException e) {
            return false;
        }
    }

    /**
     * Parses a candidate bridge navigation. Any deviation from the documented
     * shape (unknown host, unexpected query/fragment, userinfo, malformed or
     * extra theme parameters) yields NONE; nothing ambiguous ever runs.
     */
    public static Request parse(String url) {
        if (url == null) return new Request(Action.NONE, null);
        URI uri;
        try {
            uri = URI.create(url);
        } catch (IllegalArgumentException e) {
            return new Request(Action.NONE, null);
        }
        if (!SCHEME.equalsIgnoreCase(uri.getScheme())
                || uri.getRawUserInfo() != null
                || !(uri.getPath() == null || uri.getPath().isEmpty() || "/".equals(uri.getPath()))) {
            return new Request(Action.NONE, null);
        }
        String host = uri.getHost();
        if ("settings".equalsIgnoreCase(host)) {
            return clean(uri) ? new Request(Action.SETTINGS, null) : new Request(Action.NONE, null);
        }
        if ("reload".equalsIgnoreCase(host)) {
            return clean(uri) ? new Request(Action.RELOAD, null) : new Request(Action.NONE, null);
        }
        if ("theme".equalsIgnoreCase(host)) {
            Map<String, String> params = query(uri.getRawQuery());
            if (params == null || params.isEmpty()) return new Request(Action.NONE, null);
            Integer red = intValue(params.get("r"));
            Integer green = intValue(params.get("g"));
            Integer blue = intValue(params.get("b"));
            Integer alpha = params.containsKey("a") ? intValue(params.get("a")) : Integer.valueOf(255);
            Boolean dark = params.containsKey("dark") ? boolValue(params.get("dark")) : Boolean.FALSE;
            int expected = 3 + (params.containsKey("a") ? 1 : 0) + (params.containsKey("dark") ? 1 : 0);
            if (red == null || green == null || blue == null || alpha == null || dark == null
                    || params.size() != expected) {
                return new Request(Action.NONE, null);
            }
            return new Request(Action.THEME, new Theme(red, green, blue, alpha, dark));
        }
        return new Request(Action.NONE, null);
    }

    /**
     * Context gate applied on the container side: the navigation must come
     * from the current WebView, be a main-frame navigation, arrive after
     * priming finished (local UI is live), and originate from the configured
     * Gateway origin.
     */
    public static boolean gate(Action action, boolean currentView, boolean mainFrame, boolean priming,
            boolean sameOrigin) {
        if (action == null || action == Action.NONE) return false;
        return currentView && mainFrame && !priming && sameOrigin;
    }

    /**
     * Composites a (possibly translucent) theme color over an opaque base.
     * Base is either white or black depending on the reported dark flag; the
     * result is always opaque so it can back native windows and system bars.
     */
    public static int composite(int base, Theme theme) {
        if (theme == null) return base;
        int alpha = Math.max(0, Math.min(255, theme.alpha));
        if (alpha == 0) return base;
        int red = (theme.red * alpha + ((base >>> 16) & 0xFF) * (255 - alpha) + 127) / 255;
        int green = (theme.green * alpha + ((base >>> 8) & 0xFF) * (255 - alpha) + 127) / 255;
        int blue = (theme.blue * alpha + (base & 0xFF) * (255 - alpha) + 127) / 255;
        return 0xFF000000 | (red << 16) | (green << 8) | blue;
    }

    private static boolean clean(URI uri) {
        return uri.getRawQuery() == null && uri.getRawFragment() == null;
    }

    private static Map<String, String> query(String raw) {
        if (raw == null || raw.isEmpty()) return new HashMap<>();
        Map<String, String> map = new HashMap<>();
        for (String part : raw.split("&")) {
            if (part.isEmpty()) return null;
            int eq = part.indexOf('=');
            if (eq <= 0) return null;
            String key = decode(part.substring(0, eq));
            String value = decode(part.substring(eq + 1));
            if (key == null || value == null || map.containsKey(key)) return null;
            map.put(key, value);
        }
        return map;
    }

    private static String decode(String value) {
        try {
            return URLDecoder.decode(value, "UTF-8");
        } catch (Exception e) {
            return null;
        }
    }

    private static Integer intValue(String value) {
        if (value == null || value.isEmpty() || value.length() > 3) return null;
        for (int i = 0; i < value.length(); i++) {
            if (!Character.isDigit(value.charAt(i))) return null;
        }
        int parsed = Integer.parseInt(value);
        return parsed <= 255 ? Integer.valueOf(parsed) : null;
    }

    private static Boolean boolValue(String value) {
        if ("1".equals(value)) return Boolean.TRUE;
        if ("0".equals(value)) return Boolean.FALSE;
        return null;
    }
}