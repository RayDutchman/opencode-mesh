package dev.opencodemesh.app;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.ColorDrawable;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.text.InputType;
import android.view.View;
import android.view.Window;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.webkit.HttpAuthHandler;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;
import java.io.BufferedReader;
import java.io.ByteArrayInputStream;
import java.io.InputStreamReader;
import java.net.URI;
import java.util.Collections;
import java.util.HashSet;
import java.util.Set;
import org.json.JSONObject;

/**
 * WebView container for the bundled OpenCode UI.
 *
 * The page communicates with the container exclusively through limited
 * {@code ocm-app://} main-frame navigations parsed by {@link AppScheme}; no
 * JavascriptInterface is ever exposed to any frame. The container keeps its
 * own visible connection status (enter Gateway / connecting / failure with
 * Retry and Gateway settings) because during priming JavaScript is disabled
 * and the bundled page cannot yet offer menu actions.
 *
 * The window, system bars and the native dialog follow the page theme: ui.js
 * reports the browser-computed background color (r/g/b/a) and light/dark
 * flag; the bars are transparent on Android 15 edge-to-edge and the window
 * background carries the page color behind them. The gesture navigation pill
 * color is a system decision and is not controlled here.
 *
 * Auth priming, credential policy, discovery deadline, view-identity guards
 * and file picking semantics are unchanged.
 */
public final class MainActivity extends Activity {
    private static final int FILE_REQUEST = 10;
    private static final int DISCOVERY_DEADLINE_MS = 15000;

    private volatile WebView web;
    private LinearLayout layout;
    private volatile AssetPolicy policy;
    private final Set<String> assets = new HashSet<>();
    private String origin = "", username = "", password = "";
    private volatile boolean priming;
    private boolean offeredCredentials, primeFailed, clearDiscoveryHistory;
    private ValueCallback<Uri[]> chooser;
    private Runnable discoveryTimeout;

    // Theme mirrored from the page via the ocm-app://theme bridge.
    private int themeBackground = Color.rgb(250, 250, 250);
    private boolean themeDark = false;

    // Native connection status shown while the page cannot provide UI.
    private LinearLayout status;
    private TextView statusLine;
    private Button retryButton;

    @Override public void onCreate(Bundle saved) {
        super.onCreate(saved);
        layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        buildStatus();
        setContentView(layout);
        applyInsets();
        applyUiColors();
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(getAssets().open("web-assets.txt")))) {
            String line;
            while ((line = reader.readLine()) != null) assets.add(line);
            JSONObject stored = CredentialStore.load(this);
            if (stored != null) {
                origin = stored.getString("origin");
                username = stored.getString("username");
                password = stored.getString("password");
                connect();
            } else {
                showStatus("Enter your Gateway to get started.", false);
                configure();
            }
        } catch (Exception e) {
            showStatus("Settings could not be loaded. Enter your Gateway again.", false);
            configure();
        }
    }

    private int dp(int value) {
        return Math.round(getResources().getDisplayMetrics().density * value);
    }

    private void message(String text) {
        Toast.makeText(this, text, Toast.LENGTH_LONG).show();
    }

    private void buildStatus() {
        status = new LinearLayout(this);
        status.setOrientation(LinearLayout.VERTICAL);
        status.setPadding(dp(20), dp(20), dp(20), dp(16));
        TextView title = new TextView(this);
        title.setText("OpenCode Mesh");
        title.setTextSize(18);
        title.setTypeface(title.getTypeface(), Typeface.BOLD);
        statusLine = new TextView(this);
        statusLine.setTextSize(14);
        statusLine.setPadding(0, dp(6), 0, dp(12));
        LinearLayout buttons = new LinearLayout(this);
        buttons.setOrientation(LinearLayout.HORIZONTAL);
        Button settings = new Button(this);
        settings.setText("Gateway settings");
        settings.setOnClickListener(v -> configure());
        retryButton = new Button(this);
        retryButton.setText("Retry");
        retryButton.setOnClickListener(v -> retry());
        buttons.addView(settings);
        buttons.addView(retryButton);
        status.addView(title);
        status.addView(statusLine);
        status.addView(buttons);
        status.setVisibility(View.GONE);
        layout.addView(status);
    }

    private void showStatus(String text, boolean retry) {
        if (statusLine != null) statusLine.setText(text);
        if (retryButton != null) retryButton.setVisibility(retry ? View.VISIBLE : View.GONE);
        if (status != null) status.setVisibility(View.VISIBLE);
    }

    private void hideStatus() {
        if (status != null) status.setVisibility(View.GONE);
    }

    private void retry() {
        if (priming || primeFailed || web == null) connect();
        else if (web != null) web.reload();
    }

    private void configure() {
        LinearLayout box = new LinearLayout(this);
        box.setPadding(dp(24), dp(8), dp(24), dp(8));
        box.setOrientation(LinearLayout.VERTICAL);
        EditText address = input(box, "https://mesh.example.com", origin,
                InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        EditText user = input(box, "Username", username, InputType.TYPE_CLASS_TEXT);
        EditText secret = input(box, "Password", password,
                InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        AlertDialog dialog = new AlertDialog.Builder(this, themeDark
                ? AlertDialog.THEME_DEVICE_DEFAULT_DARK : AlertDialog.THEME_DEVICE_DEFAULT_LIGHT)
                .setTitle("Mesh Gateway").setView(box)
                .setPositiveButton("Connect", null).setNegativeButton("Cancel", null).create();
        dialog.setOnShowListener(unused -> {
            styleDialog(dialog, address, user, secret);
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(v -> {
                try {
                    String next = address.getText().toString().trim();
                    new AssetPolicy(next, assets);
                    URI uri = URI.create(next);
                    next = "https://" + uri.getRawAuthority();
                    CredentialStore.save(this, next, user.getText().toString(), secret.getText().toString());
                    origin = next;
                    username = user.getText().toString();
                    password = secret.getText().toString();
                    dialog.dismiss();
                    connect();
                } catch (Exception e) {
                    address.setError("Use an HTTPS origin without a path; settings must be saved securely.");
                }
            });
        });
        dialog.show();
    }

    private EditText input(LinearLayout box, String hint, String value, int type) {
        EditText field = new EditText(this);
        field.setHint(hint);
        field.setInputType(type);
        field.setText(value);
        box.addView(field);
        return field;
    }

    /** Mirrors the page theme onto the native dialog. */
    private void styleDialog(AlertDialog dialog, EditText address, EditText user, EditText secret) {
        Window window = dialog.getWindow();
        if (window != null) {
            window.setBackgroundDrawable(new ColorDrawable(themeBackground));
        }
        int text = themeDark ? 0xFFE6E6E6 : 0xFF202020;
        int faint = themeDark ? 0xFF9A9A9A : 0xFF6B6B6B;
        TextView title = dialog.findViewById(android.R.id.title);
        if (title != null) title.setTextColor(text);
        for (EditText field : new EditText[]{address, user, secret}) {
            field.setTextColor(text);
            field.setHintTextColor(faint);
            field.setBackgroundTintList(android.content.res.ColorStateList.valueOf(faint));
        }
        dialog.getButton(AlertDialog.BUTTON_POSITIVE).setTextColor(themeDark ? 0xFF7AA2F7 : 0xFF1A56C2);
        dialog.getButton(AlertDialog.BUTTON_NEGATIVE).setTextColor(faint);
    }

    // ---- theme mirroring ---------------------------------------------------

    private void handleTheme(AppScheme.Theme theme) {
        if (theme == null) return;
        themeBackground = AppScheme.composite(theme.dark ? 0xFF000000 : 0xFFFFFFFF, theme);
        themeDark = theme.dark;
        applyUiColors();
    }

    private void applyUiColors() {
        ColorDrawable background = new ColorDrawable(themeBackground);
        Window window = getWindow();
        if (window != null) {
            window.setBackgroundDrawable(background);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                window.setNavigationBarContrastEnforced(false);
            }
            // Android 15+ edge-to-edge ignores bar colors; the window
            // background shows through the transparent bars instead.
            // Earlier releases get the page color directly on the bars.
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.VANILLA_ICE_CREAM) {
                try {
                    window.setStatusBarColor(themeBackground);
                    window.setNavigationBarColor(themeBackground);
                } catch (Throwable ignored) {
                    // Some OEM builds reject translucent bar colors; keep the window color.
                }
            }
        }
        layout.setBackground(background);
        paintStatus();
        boolean lightBars = !themeDark;
        View decor = getWindow().getDecorView();
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            WindowInsetsController controller = decor.getWindowInsetsController();
            if (controller != null) {
                int mask = WindowInsetsController.APPEARANCE_LIGHT_STATUS_BARS
                        | WindowInsetsController.APPEARANCE_LIGHT_NAVIGATION_BARS;
                controller.setSystemBarsAppearance(
                        lightBars ? mask : 0, mask);
            }
        } else {
            decor.setSystemUiVisibility(lightBars
                    ? View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR | View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR
                    : 0);
        }
    }

    private void paintStatus() {
        if (status == null) return;
        int text = themeDark ? 0xFFE6E6E6 : 0xFF202020;
        int faint = themeDark ? 0xFF9A9A9A : 0xFF555555;
        status.setBackgroundColor(themeBackground);
        statusLine.setTextColor(faint);
        if (status.getChildCount() > 0) {
            TextView title = (TextView) status.getChildAt(0);
            title.setTextColor(text);
        }
    }

    // ---- safe area & keyboard ----------------------------------------------

    /**
     * The content is padded by status bar / display cutout / navigation bar /
     * IME insets instead of relying on fitsSystemWindows, so the page color
     * (window background) visibly fills the transparent system bar regions on
     * edge-to-edge builds and the keyboard never covers the input fields.
     */
    private void applyInsets() {
        // Legacy decor already fits system bars and resizes for the IME.
        // Explicit edge-to-edge and the CONSUMED object require API 30.
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) {
            layout.setFitsSystemWindows(true);
            return;
        }
        getWindow().setDecorFitsSystemWindows(false);
        layout.setOnApplyWindowInsetsListener((view, insets) -> {
            int top = 0, left = 0, right = 0, bottom = 0;
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                android.graphics.Insets bars = insets.getInsets(
                        WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout());
                android.graphics.Insets ime = insets.getInsets(WindowInsets.Type.ime());
                top = Math.max(bars.top, ime.top);
                left = Math.max(bars.left, 0);
                right = Math.max(bars.right, 0);
                bottom = Math.max(bars.bottom, ime.bottom);
            } else {
                top = Math.max(insets.getSystemWindowInsetTop(), 0);
                left = Math.max(insets.getSystemWindowInsetLeft(), 0);
                right = Math.max(insets.getSystemWindowInsetRight(), 0);
                bottom = Math.max(insets.getSystemWindowInsetBottom(), 0);
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P && insets.getDisplayCutout() != null) {
                    top = Math.max(top, insets.getDisplayCutout().getSafeInsetTop());
                    left = Math.max(left, insets.getDisplayCutout().getSafeInsetLeft());
                    right = Math.max(right, insets.getDisplayCutout().getSafeInsetRight());
                }
            }
            view.setPadding(left, top, right, bottom);
            return WindowInsets.CONSUMED;
        });
    }

    // ---- gateway connection -------------------------------------------------

    private void connect() {
        clearDiscoveryTimeout();
        if (chooser != null) {
            chooser.onReceiveValue(null);
            chooser = null;
        }
        if (web != null) {
            layout.removeView(web);
            web.destroy();
        }
        policy = new AssetPolicy(origin, assets);
        priming = true;
        offeredCredentials = false;
        primeFailed = false;
        clearDiscoveryHistory = false;
        showStatus("Connecting to " + origin + " \u2026", !origin.isEmpty());
        web = new WebView(this);
        web.setVisibility(View.INVISIBLE);
        WebSettings settings = web.getSettings();
        settings.setJavaScriptEnabled(false);
        settings.setDomStorageEnabled(true);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        web.setWebViewClient(new WebViewClient() {
            @Override public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                if (view != web) return missing();
                if (priming) {
                    return policy.isDiscoveryRequest(request.getUrl().toString(), request.getMethod()) ? null : missing();
                }
                if (!request.getMethod().equals("GET")) return null;
                String path = policy.localPath(request.getUrl().toString(), request.isForMainFrame());
                if (path == null) return null;
                try {
                    if (path.isEmpty()) return missing();
                    return new WebResourceResponse(mime(path), null, 200, "OK",
                            Collections.singletonMap("Cache-Control", "no-store"), getAssets().open("web/" + path));
                } catch (Exception e) {
                    return missing();
                }
            }

            @Override public void onReceivedHttpAuthRequest(WebView view, HttpAuthHandler handler, String host, String realm) {
                // Seed Chromium's HTTP auth cache only during a known main-frame discovery request.
                // Later challenges never receive stored credentials automatically.
                if (view == web && priming && !offeredCredentials && host.equalsIgnoreCase(URI.create(origin).getHost())) {
                    offeredCredentials = true;
                    handler.proceed(username, password);
                } else {
                    handler.cancel();
                    if (view == web && priming) {
                        primeFailed = true;
                        showStatus("Gateway authentication failed. Check your settings.", true);
                    }
                }
            }

            @Override public void onReceivedHttpError(WebView view, WebResourceRequest request, WebResourceResponse response) {
                if (view == web && priming && request.isForMainFrame()) {
                    primeFailed = true;
                    showStatus("Gateway returned HTTP " + response.getStatusCode() + ". Check your settings.", true);
                }
            }

            @Override public void onReceivedError(WebView view, WebResourceRequest request, android.webkit.WebResourceError error) {
                if (view == web && priming && request.isForMainFrame()) {
                    primeFailed = true;
                    showStatus("Gateway connection failed. Check your network and address.", true);
                }
            }

            @Override public void onPageFinished(WebView view, String url) {
                if (view != web) return;
                if (priming && url.equals(origin + "/_mesh/devices")) {
                    clearDiscoveryTimeout();
                    priming = false;
                    if (!primeFailed) {
                        view.getSettings().setJavaScriptEnabled(true);
                        clearDiscoveryHistory = true;
                        view.loadUrl(origin + "/");
                    }
                } else if (!priming && !primeFailed) {
                    if (clearDiscoveryHistory) {
                        view.clearHistory();
                        clearDiscoveryHistory = false;
                    }
                    view.setVisibility(View.VISIBLE);
                    hideStatus();
                }
            }

            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                if (view != web) return true;
                String url = request.getUrl().toString();
                if (AppScheme.isAppScheme(url)) {
                    // Custom-scheme navigation is always blocked here; the
                    // gating below decides whether the requested action runs.
                    handleBridge(view, request, url);
                    return true;
                }
                if (priming) return !policy.isDiscoveryRequest(url, request.getMethod());
                if (policy.sameOrigin(url)) return false;
                if (request.isForMainFrame() && (url.startsWith("https://") || url.startsWith("http://")
                        || url.startsWith("mailto:"))) {
                    try {
                        startActivity(new Intent(Intent.ACTION_VIEW, request.getUrl()));
                    } catch (Exception e) {
                        message("No application can open this link.");
                    }
                }
                return true;
            }
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
                if (view != web) {
                    callback.onReceiveValue(null);
                    return true;
                }
                if (chooser != null) chooser.onReceiveValue(null);
                chooser = callback;
                try {
                    startActivityForResult(params.createIntent(), FILE_REQUEST);
                } catch (Exception e) {
                    chooser.onReceiveValue(null);
                    chooser = null;
                    message("No file picker is available.");
                }
                return true;
            }
        });
        layout.addView(web, new LinearLayout.LayoutParams(-1, 0, 1));
        final WebView attempt = web;
        discoveryTimeout = () -> {
            if (web != attempt || !priming) return;
            primeFailed = true;
            priming = false;
            discoveryTimeout = null;
            attempt.stopLoading();
            showStatus("Gateway connection timed out. Check your address or network, then retry.", true);
        };
        web.postDelayed(discoveryTimeout, DISCOVERY_DEADLINE_MS);
        // This fetch authenticates business discovery; the UI itself is always packaged.
        web.loadUrl(origin + "/_mesh/devices");
    }

    /**
     * Runs a bridge action only when the source is trustworthy: the current
     * WebView instance, a main-frame navigation, priming already finished
     * (the local UI is live) and the page still on the configured Gateway
     * origin.
     */
    private void handleBridge(WebView view, WebResourceRequest request, String url) {
        AppScheme.Request bridge = AppScheme.parse(url);
        boolean allowed = AppScheme.gate(bridge.action, view == web, request.isForMainFrame(),
                priming, policy.sameOrigin(view.getUrl()));
        if (!allowed) return;
        switch (bridge.action) {
            case SETTINGS:
                configure();
                break;
            case RELOAD:
                retry();
                break;
            case THEME:
                handleTheme(bridge.theme);
                break;
            default:
                break;
        }
    }

    private void clearDiscoveryTimeout() {
        if (web != null && discoveryTimeout != null) web.removeCallbacks(discoveryTimeout);
        discoveryTimeout = null;
    }

    private static WebResourceResponse missing() {
        return new WebResourceResponse("text/plain", "UTF-8", 404, "Not Found",
                Collections.singletonMap("Cache-Control", "no-store"),
                new ByteArrayInputStream("Bundled asset not found".getBytes(java.nio.charset.StandardCharsets.UTF_8)));
    }

    private static String mime(String path) {
        if (path.endsWith(".html")) return "text/html";
        if (path.endsWith(".js") || path.endsWith(".mjs")) return "text/javascript";
        if (path.endsWith(".css")) return "text/css";
        if (path.endsWith(".wasm")) return "application/wasm";
        if (path.endsWith(".svg")) return "image/svg+xml";
        if (path.endsWith(".woff2")) return "font/woff2";
        if (path.endsWith(".json") || path.endsWith(".webmanifest")) return "application/json";
        String type = java.net.URLConnection.guessContentTypeFromName(path);
        return type == null ? "application/octet-stream" : type;
    }

    @Override protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (request == FILE_REQUEST && chooser != null) {
            chooser.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(result, data));
            chooser = null;
        }
    }

    @Override public void onBackPressed() {
        if (web != null && web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }

    @Override protected void onDestroy() {
        clearDiscoveryTimeout();
        if (chooser != null) chooser.onReceiveValue(null);
        if (web != null) web.destroy();
        super.onDestroy();
    }
}
