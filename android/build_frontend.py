#!/usr/bin/env python3
"""Build the pinned upstream UI with source-level Mesh bootstrap integration.

The tree handed in with --source must be the pristine archive of REVISION (or
that archive after this prepare step). prepare() gates every file the Mesh
patch set touches on a SHA-256 pin: content must be byte-identical to the
pinned pristine file (then it is patched) or to the pinned post-prepare file
(a re-run is left untouched). Anything else is rejected instead of being
patched around, so a v2.0.15 tree that drifted from the pinned commit can no
longer be built silently.

package() additionally bundles the license texts of the bundled third-party
frontend artifacts. Dependency license paths are version-pinned and explicit
(app node_modules first, then the root bun layout); a drifted or missing
license fails the package step instead of shipping the wrong text.
"""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
REVISION = '6f3639d82ed0760091792189b78f8eeb44f699b1'
# SHA-256 of the codeload archive for REVISION
# (android-upstream-v2.0.15.tar.gz); recorded so a future downloader can
# re-verify the whole archive before extraction.
TAR_SHA256 = 'f3c554f05cd4aeeb44d480e5d8902ea75a2aa157d65e908db097717f09293375'
sys.path.insert(0, str(ROOT))
from src import __version__
from src.static_adapter import TRANSPORT_ADAPTER

# Files (relative to the upstream source root) the Mesh patch set touches.
# 'original' is the pristine content from the codeload archive of REVISION;
# 'patched' is the exact content prepare() must produce; both are verified.
PINNED_FILE_HASHES = {
    'packages/app/package.json': {
        'original': 'c5e4d6386ad043edae8e25f27f525313e8e614c87890e668df91048959d8781c',
        'patched': 'c5e4d6386ad043edae8e25f27f525313e8e614c87890e668df91048959d8781c',
    },
    'packages/app/src/runtime/platform/web.ts': {
        'original': '2eee848beb56c6c45c784289f96ed6c4e8a276c5eaab10b01d83e9f31e31afaa',
        'patched': 'cb6baa5717af7d7a79578d791cd049874ce35a4f819e2f3f57b0b4380f728330',
    },
    'packages/app/src/entry.tsx': {
        'original': '406ecd46932cd5de5ec4b6c9e807b772b6fd494e1d897efb6487efbdae525dc2',
        'patched': '1f0a3eda4d789f3fd9d4b92bc20691f261ca40e75d9b09139cc3af46a56b797b',
    },
    'packages/app/vite.config.ts': {
        'original': 'c39a1edcbb7e6c53b86ad5554f2858bc553f9e2b0a5f417d92bafa6402f3ec51',
        'patched': 'd2badf03c3b943cf1f7d41f54766db074a31d8ab24b4e13ffdca4fbf4afc50b9',
    },
    'packages/app/index.html': {
        'original': '07e8fb49f13f409dc7576af68674d26c40a185c1d06b2b401904263a6b70f9ec',
        'patched': '12baf8b32efc9f4f07902ccf7f82992bd70d515ff949eaafabd05fd680721a6c',
    },
}

# The exact replacement pairs per entry file; applied only when the file's
# digest matched the pinned 'original' value, in the order the pinned
# 'patched' hashes were captured.
PATCHES = {
    'packages/app/src/runtime/platform/web.ts': (
        ('  return location.origin\n', '  return (window as any).__ocmBootstrap.serverUrl\n'),
    ),
    'packages/app/src/entry.tsx': (
        ('const web = createWebPlatform(pkg.version)',
         'await (window as any).__ocmBootstrap.ready\nconst web = createWebPlatform(pkg.version)'),
        ('if (import.meta.env.PROD && "serviceWorker" in navigator) {',
         'if (false) { // The APK owns its immutable UI resources; no service worker. '),
    ),
    'packages/app/vite.config.ts': (
        ('    serviceWorker(fileURLToPath(new URL("./dist", import.meta.url))),',
         '    false, // Packaged builds do not emit a service worker.'),
        ('    sourcemap: true,', '    sourcemap: false,'),
    ),
    'packages/app/index.html': (
        ('    <title>OpenCode</title>',
         '    <title>OpenCode Mesh</title>\n'
         '    <script src="/mesh-transport.js"></script>\n'
         '    <script src="/mesh-ui.js"></script>'),
    ),
}

# Third-party license texts bundled next to the packaged UI. Each entry maps
# the output file name to explicit, version-pinned candidates, app-scoped node
# modules first and the root bun layout second. No name or version globbing: a
# drifted dependency must fail the package step, not ship the wrong license.
PINNED_LICENSE_FILES = {
    'LICENSE-KaTeX.txt': (
        'packages/app/node_modules/.bun/katex@0.16.47/node_modules/katex/LICENSE',
        'node_modules/.bun/katex@0.16.47/node_modules/katex/LICENSE',
    ),
    'LICENSE-IBMPlex.txt': (
        'packages/app/node_modules/.bun/@ibm+plex@6.4.1/node_modules/@ibm/plex/LICENSE.txt',
        'node_modules/.bun/@ibm+plex@6.4.1/node_modules/@ibm/plex/LICENSE.txt',
    ),
}

# Repository-local third-party notices (e.g. the Inter and JetBrainsNerd font
# licenses collected by the release owner); every top-level file is copied
# verbatim into output/notices when the directory exists.
NOTICES_DIR = Path(__file__).resolve().parent / 'notices'


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tar_digest(path):
    """SHA-256 of a codeload archive; compare with TAR_SHA256 for full-tree pinning."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def replace_once(path, old, new):
    text = path.read_text()
    if text.count(new) == 1:
        return
    if text.count(old) != 1:
        raise ValueError(f'Unrecognized upstream source contract: {path}')
    path.write_text(text.replace(old, new, 1))


def prepare(source, pinned=None):
    """Pin-check the upstream entry files, apply the Mesh patch set, return the app dir."""
    pinned = PINNED_FILE_HASHES if pinned is None else pinned
    source = Path(source)
    app = source / 'packages/app'
    pkg = app / 'package.json'
    if json.loads(pkg.read_text())['version'] != '2.0.15':
        raise ValueError('The bundled frontend must be OpenCode 2.0.15')
    # Accept every pinned file as pristine (to patch) or already patched (re-run).
    for rel, spec in pinned.items():
        digest = file_digest(source / rel)
        if digest not in (spec['original'], spec['patched']):
            raise ValueError(f'Unrecognized upstream source contract: {source / rel}')
    # Patch only the pristine files.
    for rel, replacements in PATCHES.items():
        if file_digest(source / rel) == pinned[rel]['original']:
            for old, new in replacements:
                replace_once(source / rel, old, new)
    # Whole-file gate so patching can never drift from the pins.
    for rel, spec in pinned.items():
        if file_digest(source / rel) != spec['patched']:
            raise ValueError(f'Patch did not reproduce pinned upstream content: {source / rel}')
    adapter = TRANSPORT_ADAPTER.split('<script id="ocm-transport-adapter">', 1)[1].split('</script>', 1)[0]
    adapter = adapter.replace('__OCM_VERSION_JSON__', json.dumps(__version__))
    (app / 'public/mesh-transport.js').write_text(adapter)
    # APK-only UI overlay (dots menu + theme bridge); bundled verbatim so the
    # audited android/ui.js source and the packaged asset can never diverge.
    (app / 'public/mesh-ui.js').write_bytes((Path(__file__).resolve().parent / 'ui.js').read_bytes())
    return app


def _resolve_licenses(source):
    """Read every pinned license from its first existing candidate; fail loudly otherwise."""
    resolved = {}
    for name, candidates in PINNED_LICENSE_FILES.items():
        for rel in candidates:
            path = source / rel
            if path.is_file():
                resolved[name] = path.read_bytes()
                break
        if name not in resolved:
            raise ValueError(
                f'Missing pinned license source for {name}; expected '
                f'{", ".join(str(source / r) for r in candidates)}')
    return resolved


def package(app, output, source, notices=None):
    """Bundle a built dist/ tree and its licenses into a fresh output directory."""
    if output.exists():
        raise ValueError(f'Use a fresh assets output directory: {output}')
    licenses = _resolve_licenses(source)
    web = output / 'web'
    shutil.copytree(app / 'dist', web)
    (output / 'LICENSE-OpenCode.txt').write_bytes((source / 'LICENSE').read_bytes())
    for name, data in licenses.items():
        (output / name).write_bytes(data)
    notices = Path(notices) if notices is not None else NOTICES_DIR
    if notices.is_dir():
        target = output / 'notices'
        target.mkdir()
        for entry in sorted(notices.iterdir()):
            if entry.is_file():
                (target / entry.name).write_bytes(entry.read_bytes())
    files = sorted(path.relative_to(web).as_posix() for path in web.rglob('*') if path.is_file())
    if 'index.html' not in files or 'mesh-transport.js' not in files or 'mesh-ui.js' not in files:
        raise ValueError('Build omitted required local bootstrap assets')
    (output / 'web-assets.txt').write_text('\n'.join(files) + '\n')
    manifest = {
        'opencodeVersion': '2.0.15', 'opencodeRevision': REVISION,
        'sourceTarSha256': TAR_SHA256, 'meshVersion': __version__,
        'files': {name: hashlib.sha256((web / name).read_bytes()).hexdigest() for name in files},
    }
    (output / 'bundle-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(f'Packaged {len(files)} local frontend resources in {output}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--source-tar', type=Path,
                        help='codeload archive backing --source; its SHA-256 must equal TAR_SHA256')
    args = parser.parse_args()
    if args.source_tar is not None and tar_digest(args.source_tar) != TAR_SHA256:
        raise ValueError(f'Source archive does not match the pinned tar: {args.source_tar}')
    app = prepare(args.source.resolve())
    if args.prepare_only:
        return
    subprocess.run(['bun', 'run', 'build'], cwd=app, check=True)
    package(app, args.output.resolve(), args.source.resolve())


if __name__ == '__main__':
    main()
