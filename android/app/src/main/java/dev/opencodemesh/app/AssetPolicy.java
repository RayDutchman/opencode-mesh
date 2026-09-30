package dev.opencodemesh.app;

import java.net.URI;
import java.util.HashSet;
import java.util.Set;

/** Restricts bundled responses to the configured origin and known UI routes. */
public final class AssetPolicy {
    private final URI origin;
    private final Set<String> assets;

    public AssetPolicy(String gateway, Set<String> assets) {
        origin = URI.create(gateway);
        if (!"https".equals(origin.getScheme()) || origin.getHost() == null
                || origin.getRawUserInfo() != null || origin.getQuery() != null
                || origin.getFragment() != null
                || !(origin.getPath().isEmpty() || "/".equals(origin.getPath()))) {
            throw new IllegalArgumentException("Enter an HTTPS Gateway origin without a path or credentials");
        }
        this.assets = new HashSet<>(assets);
    }

    private static int port(URI uri) {
        return uri.getPort() == -1 ? 443 : uri.getPort();
    }

    public boolean sameOrigin(String value) {
        try {
            URI uri = URI.create(value);
            return "https".equals(uri.getScheme()) && uri.getRawUserInfo() == null
                    && origin.getHost().equalsIgnoreCase(uri.getHost()) && port(origin) == port(uri);
        } catch (IllegalArgumentException e) {
            return false;
        }
    }

    public boolean isDiscoveryRequest(String value, String method) {
        if (!"GET".equals(method) || !sameOrigin(value)) return false;
        URI uri = URI.create(value);
        return "/_mesh/devices".equals(uri.getRawPath()) && uri.getRawQuery() == null && uri.getRawFragment() == null;
    }

    /** null means network; empty means a missing bundled asset, never network fallback. */
    public String localPath(String value, boolean navigation) {
        if (!sameOrigin(value)) return null;
        URI uri = URI.create(value);
        String path = uri.getPath();
        if (path == null || path.contains("\\") || path.indexOf('\0') >= 0) return null;
        for (String segment : path.split("/")) {
            if (segment.equals("..") || segment.equals(".")) return null;
        }
        if (path.startsWith("/_mesh/ui/")) return "";
        if (path.startsWith("/api/") || path.equals("/api") || path.startsWith("/_mesh/")) return null;
        String relative = path.startsWith("/") ? path.substring(1) : path;
        if (assets.contains(relative)) return relative;
        if (path.startsWith("/_assets/") || path.startsWith("/fonts/")
                || path.matches(".*\\.(js|mjs|css|woff2?|ttf|wasm|svg|png|ico|webmanifest)$")) return "";
        if (navigation && (path.isEmpty() || path.equals("/") || path.startsWith("/server/"))) {
            return "index.html";
        }
        return null;
    }
}
