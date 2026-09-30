"""Hermetic call-shape tests for the gradle-free APK assembly script.

Fake SDK tools record their argv instead of running; they only materialize the
files later assembly steps read (zipalign output, keystore, signed APK). This
keeps the test free of the full Android toolchain while it still exercises the
real javac/d8/aapt2 invocation order and the version-derived output name.
"""

import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from android import build_apk as ba

FAKE_TOOLS = '''#!/usr/bin/env python3
import os
import sys
from pathlib import Path

log = Path(os.environ['OCM_FAKE_LOG'])
name = os.path.basename(sys.argv[0])
with log.open('a') as f:
    f.write(' '.join([name] + sys.argv[1:]) + '\\n')
if name == 'd8':
    Path(sys.argv[sys.argv.index('--output') + 1] + '/classes.dex').write_bytes(b'dex')
elif name == 'zipalign':
    Path(sys.argv[-1]).write_bytes(b'aligned')
elif name == 'keytool':
    Path(sys.argv[sys.argv.index('-keystore') + 1]).write_bytes(b'key')
elif name == 'apksigner' and sys.argv[1] == 'sign':
    Path(sys.argv[sys.argv.index('--out') + 1]).write_bytes(b'APK')
'''


def _tools_dir(tmp_path):
    tools = tmp_path / 'tools'
    tools.mkdir()
    for name in ('javac', 'd8', 'aapt2', 'zipalign', 'apksigner', 'keytool'):
        script = tools / name
        script.write_text(FAKE_TOOLS)
        script.chmod(0o755)
    return tools


def _manifest_attrs():
    root = ET.parse(ba.MANIFEST).getroot()
    ns = '{http://schemas.android.com/apk/res/android}'
    return root.attrib[ns + 'versionCode'], root.attrib[ns + 'versionName']


def test_manifest_version_matches_the_shipped_manifest():
    code, name = ba.manifest_version()
    assert (code, name) == _manifest_attrs()


def test_apk_build_compiles_res_and_names_output_from_manifest(tmp_path, monkeypatch):
    tools = _tools_dir(tmp_path)
    log = tmp_path / 'calls.log'
    monkeypatch.setenv('OCM_FAKE_LOG', str(log))
    assets = tmp_path / 'assets'
    assets.mkdir()
    platform = tmp_path / 'android-35'
    out = tmp_path / 'out'
    key = tmp_path / 'signing' / 'preview.jks'
    monkeypatch.setenv('PATH', str(tools) + os.pathsep + os.environ['PATH'])
    monkeypatch.setattr(sys, 'argv', [
        'build_apk.py', '--tools', str(tools), '--platform', str(platform),
        '--assets', str(assets), '--output', str(out), '--keystore', str(key),
    ])
    ba.main()
    calls = log.read_text()
    assert f'aapt2 compile --dir {ba.RES} -o {out}/res.zip' in calls
    assert (
        f'aapt2 link -o {out}/unsigned.apk -I {platform}/android.jar '
        f'--manifest {ba.MANIFEST} -A {assets} {out}/res.zip'
    ) in calls
    code, name = ba.manifest_version()
    assert (out / f'opencode-mesh-{name}.apk').is_file()
    assert 'apksigner verify' in calls
    assert (out / f'opencode-mesh-{name}.apk.sha256').is_file()