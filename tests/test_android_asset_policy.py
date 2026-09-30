"""Exercise the Android container's URL boundary with the host JVM."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_local_assets_never_replace_api_or_other_origins(tmp_path):
    if not shutil.which('javac') or not shutil.which('java'):
        pytest.skip('JDK required for Android URL policy checks')
    source = Path(__file__).parents[1] / 'android/app/src/main/java/dev/opencodemesh/app/AssetPolicy.java'
    harness = tmp_path / 'PolicyCheck.java'
    harness.write_text('''
import dev.opencodemesh.app.AssetPolicy;
import java.util.Set;
public class PolicyCheck {
    static void eq(Object a, Object b) {
        if (!java.util.Objects.equals(a, b)) throw new AssertionError(a + " != " + b);
    }
    public static void main(String[] args) {
        AssetPolicy p = new AssetPolicy("https://mesh.example.com:8443",
            Set.of("index.html", "_assets/index.js", "fonts/test.woff2"));
        eq(p.localPath("https://mesh.example.com:8443/", true), "index.html");
        eq(p.localPath("https://mesh.example.com:8443/server/device/session/one", true), "index.html");
        eq(p.localPath("https://mesh.example.com:8443/_assets/index.js?v=1", false), "_assets/index.js");
        eq(p.localPath("https://mesh.example.com:8443/fonts/test.woff2", false), "fonts/test.woff2");
        eq(p.localPath("https://mesh.example.com:8443/api/session", true), null);
        eq(p.localPath("https://mesh.example.com:8443/_mesh/devices", false), null);
        eq(p.localPath("https://mesh.example.com:8443/_mesh/device/a/api/session", true), null);
        eq(p.localPath("https://mesh.example.com/_assets/index.js", false), null);
        eq(p.localPath("https://evil.example/_assets/index.js", false), null);
        eq(p.localPath("http://mesh.example.com:8443/_assets/index.js", false), null);
        eq(p.localPath("https://mesh.example.com:8443/_assets/../index.html", false), null);
        eq(p.localPath("https://mesh.example.com:8443/_assets/%2e%2e/index.html", false), null);
        eq(p.localPath("https://mesh.example.com:8443/_assets/missing.js", false), "");
        eq(p.localPath("https://mesh.example.com:8443/fonts/missing.woff2", false), "");
        eq(p.localPath("https://mesh.example.com:8443/oc-theme-missing.js", false), "");
        eq(p.localPath("https://mesh.example.com:8443/_mesh/ui/2/device/_assets/index.js", false), "");
        eq(p.localPath("https://mesh.example.com:8443/server/a", false), null);
        eq(p.isDiscoveryRequest("https://mesh.example.com:8443/_mesh/devices", "GET"), true);
        eq(p.isDiscoveryRequest("https://mesh.example.com:8443/_mesh/devices?redirect=1", "GET"), false);
        eq(p.isDiscoveryRequest("https://mesh.example.com/_mesh/devices", "GET"), false);
        eq(p.isDiscoveryRequest("https://mesh.example.com:8443/_mesh/devices", "POST"), false);
        eq(p.isDiscoveryRequest("https://mesh.example.com:8443/", "GET"), false);
        try { new AssetPolicy("http://mesh.example.com", Set.of()); throw new AssertionError(); }
        catch (IllegalArgumentException expected) { }
        try { new AssetPolicy("https://user:password@mesh.example.com", Set.of()); throw new AssertionError(); }
        catch (IllegalArgumentException expected) { }
    }
}
''')
    subprocess.run(['javac', '-d', str(tmp_path), str(source), str(harness)], check=True, capture_output=True, text=True)
    subprocess.run(['java', '-cp', str(tmp_path), 'PolicyCheck'], check=True, capture_output=True, text=True)
