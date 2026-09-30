#!/usr/bin/env python3
"""Build and sign a dependency-free Android WebView preview using Android SDK tools."""

import argparse
import hashlib
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / 'app/src/main/AndroidManifest.xml'
RES = ROOT / 'app/src/main/res'
NS = '{http://schemas.android.com/apk/res/android}'


def run(*args):
    subprocess.run([str(arg) for arg in args], check=True)


def manifest_version():
    """Read versionCode/versionName from the APK manifest (single source of truth)."""
    root = ET.parse(MANIFEST).getroot()
    return root.attrib[NS + 'versionCode'], root.attrib[NS + 'versionName']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tools', type=Path, required=True)
    parser.add_argument('--platform', type=Path, required=True)
    parser.add_argument('--assets', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--keystore', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        raise ValueError('Use a fresh output directory to avoid stale build artifacts')
    classes = output / 'classes'
    classes.mkdir(parents=True)
    android = args.platform.resolve() / 'android.jar'
    sources = sorted((ROOT / 'app/src/main/java').rglob('*.java'))
    run('javac', '--release', '8', '-classpath', android, '-d', classes, *sources)
    dex = output / 'dex'
    dex.mkdir()
    run(args.tools / 'd8', '--lib', android, '--min-api', '26', '--output', dex, *sorted(classes.rglob('*.class')))
    compiled_res = output / 'res.zip'
    run(args.tools / 'aapt2', 'compile', '--dir', RES, '-o', compiled_res)
    unsigned = output / 'unsigned.apk'
    run(args.tools / 'aapt2', 'link', '-o', unsigned, '-I', android, '--manifest',
        MANIFEST, '-A', args.assets.resolve(), compiled_res)
    with zipfile.ZipFile(unsigned, 'a', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(dex / 'classes.dex', 'classes.dex')
    aligned = output / 'aligned.apk'
    run(args.tools / 'zipalign', '-f', '4', unsigned, aligned)
    key = args.keystore.resolve()
    if not key.exists():
        key.parent.mkdir(parents=True, exist_ok=True)
        # This is a local preview key, not a production release signing identity.
        run('keytool', '-genkeypair', '-keystore', key, '-storepass', 'android', '-keypass', 'android',
            '-alias', 'mesh-preview', '-keyalg', 'RSA', '-keysize', '2048', '-validity', '3650',
            '-dname', 'CN=OpenCode Mesh Preview')
        key.chmod(0o600)
    code, name = manifest_version()
    apk = output / f'opencode-mesh-{name}.apk'
    run(args.tools / 'apksigner', 'sign', '--ks', key, '--ks-key-alias', 'mesh-preview',
        '--ks-pass', 'pass:android', '--key-pass', 'pass:android', '--out', apk, aligned)
    run(args.tools / 'apksigner', 'verify', '--verbose', apk)
    run(args.tools / 'aapt2', 'dump', 'badging', apk)
    digest = hashlib.sha256(apk.read_bytes()).hexdigest()
    (output / (apk.name + '.sha256')).write_text(f'{digest}  {apk.name}\n')
    print(f'APK: {apk}\nSHA-256: {digest}')


if __name__ == '__main__':
    main()
