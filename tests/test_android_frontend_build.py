"""Hermetic pin tests for the Android frontend prepare()/package() steps.

The pristine packages/app files below are byte-for-byte copies of the codeload
archive pinned by android.build_frontend.REVISION (upstream opencode v2.0.15).
Their digests must equal the 'original' entries in
android.build_frontend.PINNED_FILE_HASHES; test_fixture_matches_the_pinned_
originals enforces that on every run. When the pinned commit moves, regenerate
this fixture and the pins together from the new archive: extract it, re-embed
packages/app/{package.json,src/runtime/platform/web.ts,src/entry.tsx,
vite.config.ts,index.html} and recompute the SHA-256 values.
"""

import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from android import build_frontend as bf

PRISTINE_PACKAGE_JSON = b'{\n  "name": "@opencode/app",\n  "version": "2.0.15",\n  "description": "",\n  "type": "module",\n  "exports": {\n    ".": "./src/index.ts",\n    "./desktop": "./src/desktop.ts",\n    "./desktop-menu": "./src/shell/commands/desktop-menu.ts",\n    "./i18n/desktop-native": "./src/runtime/i18n/desktop-native.ts",\n    "./updater": "./src/shell/updates/types.ts",\n    "./wsl/types": "./src/servers/wsl/types.ts",\n    "./ssh": "./src/servers/ssh/types.ts",\n    "./vite": "./vite.js",\n    "./index.css": "./src/index.css"\n  },\n  "scripts": {\n    "typecheck": "tsgo -b",\n    "typecheck:e2e": "tsgo -p e2e/tsconfig.json",\n    "start": "vite",\n    "dev": "vite",\n    "build": "vite build",\n    "serve": "vite preview",\n    "test": "bun run test:unit && bun run test:browser",\n    "test:unit": "bun test --conditions=solid --only-failures --preload ./happydom.ts ./src",\n    "test:browser": "bun test --conditions=browser --preload ./happydom.ts ./test-browser",\n    "test:unit:watch": "bun test --conditions=solid --watch --preload ./happydom.ts ./src",\n    "test:e2e": "playwright test",\n    "test:e2e:built": "PLAYWRIGHT_BUILD=1 playwright test",\n    "test:e2e:local": "playwright test",\n    "test:components": "playwright test --config playwright.components.config.ts",\n    "test:components:ui": "playwright test --config playwright.components.config.ts --ui",\n    "test:e2e:ui": "playwright test --ui",\n    "test:e2e:report": "playwright show-report e2e/playwright-report",\n    "test:service-worker": "bun run build && playwright test --config e2e/service-worker/playwright.config.ts",\n    "test:stability": "bun test ./e2e/performance/unit/visual-stability.test.ts && playwright test --config e2e/performance/timeline-stability/playwright.config.ts",\n    "test:bench": "bun test ./e2e/performance/unit && playwright test --config e2e/performance/playwright.config.ts",\n    "bench:tabs": "PLAYWRIGHT_BUILD=1 playwright test --config e2e/performance/playwright.config.ts timeline/session-tab-switch-benchmark.spec.ts --repeat-each=20 --workers=1 --retries=0 --reporter=line,./e2e/performance/tab-switch-reporter.ts",\n    "bench:entry": "PLAYWRIGHT_BUILD=1 playwright test --config e2e/performance/playwright.config.ts timeline/session-entry-benchmark.spec.ts --repeat-each=20 --workers=1 --retries=0 --reporter=line,./e2e/performance/tab-switch-reporter.ts",\n    "test:bench:devex": "bun test ./e2e/performance/unit/desktop-startup.test.ts && playwright test --config e2e/performance/devex/playwright.config.ts"\n  },\n  "license": "MIT",\n  "devDependencies": {\n    "@happy-dom/global-registrator": "20.0.11",\n    "@playwright/test": "catalog:",\n    "@sentry/vite-plugin": "catalog:",\n    "@tailwindcss/vite": "4.3.3",\n    "@types/bun": "catalog:",\n    "@types/node": "catalog:",\n    "@typescript/native-preview": "catalog:",\n    "diff": "catalog:",\n    "happy-dom": "20.11.1",\n    "tw-animate-css": "1.4.0",\n    "vite": "8.2.2",\n    "vite-plugin-pwa": "1.3.0",\n    "vite-plugin-solid": "2.11.14"\n  },\n  "dependencies": {\n    "@corvu/drawer": "catalog:",\n    "@dnd-kit/abstract": "0.5.0",\n    "@dnd-kit/dom": "0.5.0",\n    "@dnd-kit/helpers": "0.5.0",\n    "@dnd-kit/solid": "0.5.0",\n    "@ibm/plex": "6.4.1",\n    "@kobalte/core": "catalog:",\n    "@opencode/client": "workspace:*",\n    "@opencode/schema": "workspace:*",\n    "@opencode/plugin-browser": "workspace:*",\n    "@opencode/session-ui": "workspace:*",\n    "@opencode/ui": "workspace:*",\n    "@opencode/util": "workspace:*",\n    "@pierre/trees": "1.0.0-beta.4",\n    "@sentry/solid": "catalog:",\n    "@solid-primitives/event-bus": "1.1.2",\n    "@solid-primitives/event-listener": "catalog:",\n    "@solid-primitives/i18n": "2.2.1",\n    "@solid-primitives/keyed": "1.5.3",\n    "@solid-primitives/media": "catalog:",\n    "@solid-primitives/resize-observer": "catalog:",\n    "@solid-primitives/scheduled": "1.5.3",\n    "@solid-primitives/storage": "catalog:",\n    "@solidjs/meta": "catalog:",\n    "@solidjs/router": "catalog:",\n    "@tanstack/solid-query": "5.91.4",\n    "@tanstack/solid-virtual": "catalog:",\n    "core-js": "3.50.0",\n    "effect": "catalog:",\n    "fuzzysort": "catalog:",\n    "ghostty-web": "github:anomalyco/ghostty-web#83c0a07b8628b748aed073b232cb4b52a6ca11c1",\n    "qr-scanner": "1.4.2",\n    "remeda": "catalog:",\n    "solid-js": "catalog:",\n    "solid-presence": "0.2.0",\n    "tailwindcss": "4.3.3",\n    "uqr": "0.1.3"\n  }\n}\n'
PRISTINE_WEB_TS = b'import { createBrowserDraftStore } from "@/runtime/persistence/drafts"\nimport { ServerConnection } from "@/runtime/server/registry"\nimport type { Platform } from "./platform"\n\nconst DEFAULT_SERVER_URL_KEY = "opencode.settings.dat:defaultServerUrl"\n\nexport function createWebPlatform(version: string) {\n  const currentServerUrl = getCurrentServerUrl()\n  const storedServerUrl = readDefaultServerUrl()\n  const platform: Platform = {\n    platform: "web",\n    draftStore: createBrowserDraftStore(),\n    version,\n    openExternal(value) {\n      if (!URL.canParse(value)) return\n      const url = new URL(value)\n      if (url.protocol !== "http:" && url.protocol !== "https:" && url.protocol !== "mailto:") return\n      window.open(url.href, "_blank", "noopener,noreferrer")\n    },\n    restart: async () => window.location.reload(),\n    async notify(title, description, onClick) {\n      if (!("Notification" in window)) return\n\n      const permission =\n        Notification.permission === "default"\n          ? await Notification.requestPermission().catch(() => "denied")\n          : Notification.permission\n      if (permission !== "granted") return\n      if (document.visibilityState === "visible" && document.hasFocus()) return\n\n      const notification = new Notification(title, {\n        body: description ?? "",\n        icon: "https://opencode.ai/favicon-96x96-v3.png",\n      })\n      notification.onclick = () => {\n        window.focus()\n        onClick?.()\n        notification.close()\n      }\n    },\n    getDefaultServer: async () => {\n      const stored = readDefaultServerUrl()\n      return stored ? ServerConnection.Key.make(stored) : null\n    },\n    setDefaultServer: writeDefaultServerUrl,\n  }\n\n  return {\n    platform,\n    currentServerUrl,\n    defaultServerUrl: storedServerUrl ?? currentServerUrl,\n  }\n}\n\nfunction getCurrentServerUrl() {\n  if (import.meta.env.VITE_OPENCODE_SERVER_MODE === "none") return undefined\n  if (import.meta.env.DEV) {\n    const loopback =\n      location.hostname === "localhost" || location.hostname === "[::1]" || location.hostname.startsWith("127.")\n    const host = import.meta.env.VITE_OPENCODE_SERVER_HOST ?? (loopback ? location.hostname : "localhost")\n    return `http://${host}:${import.meta.env.VITE_OPENCODE_SERVER_PORT ?? "4096"}`\n  }\n  return location.origin\n}\n\nfunction readDefaultServerUrl() {\n  if (typeof localStorage === "undefined") return null\n  try {\n    return localStorage.getItem(DEFAULT_SERVER_URL_KEY)\n  } catch {\n    return null\n  }\n}\n\nfunction writeDefaultServerUrl(value: string | null) {\n  if (typeof localStorage === "undefined") return\n  try {\n    if (value !== null) {\n      localStorage.setItem(DEFAULT_SERVER_URL_KEY, value)\n      return\n    }\n    localStorage.removeItem(DEFAULT_SERVER_URL_KEY)\n  } catch {\n    return\n  }\n}\n'
PRISTINE_ENTRY_TSX = b'// @refresh reload\n\nimport "@/runtime/polyfills"\nimport { init } from "@sentry/solid"\nimport { render } from "solid-js/web"\nimport { AppBaseProviders, AppInterface } from "@/app"\nimport { loadInitialLocale } from "@/runtime/i18n/language"\nimport { PlatformProvider } from "@/runtime/platform/platform"\nimport { createWebPlatform } from "@/runtime/platform/web"\nimport { isStandalone, PwaRoutePersistence, restorePwaRoute } from "@/runtime/platform/pwa"\nimport { KeyboardInsets } from "@/runtime/platform/keyboard"\nimport en from "@/runtime/i18n/en"\nimport zh from "@/runtime/i18n/zh"\nimport { authFromToken } from "@/runtime/server/api"\nimport pkg from "../package.json"\nimport { ServerConnection } from "@/runtime/server/registry"\n\nconst getLocale = () => {\n  if (typeof navigator !== "object") return "en" as const\n  const languages = navigator.languages?.length ? navigator.languages : [navigator.language]\n  for (const language of languages) {\n    if (!language) continue\n    if (language.toLowerCase().startsWith("zh")) return "zh" as const\n  }\n  return "en" as const\n}\n\nconst getRootNotFoundError = () => {\n  const key = "error.dev.rootNotFound" as const\n  const locale = getLocale()\n  return locale === "zh" ? (zh[key] ?? en[key]) : en[key]\n}\n\nconst root = document.getElementById("root")\nif (!(root instanceof HTMLElement) && import.meta.env.DEV) {\n  throw new Error(getRootNotFoundError())\n}\n\nconst clearAuthToken = () => {\n  const params = new URLSearchParams(location.search)\n  if (!params.has("auth_token")) return\n  params.delete("auth_token")\n  history.replaceState(null, "", location.pathname + (params.size ? `?${params}` : "") + location.hash)\n}\n\nconst web = createWebPlatform(pkg.version)\n\nif (import.meta.env.PROD && "serviceWorker" in navigator) {\n  window.addEventListener("load", () => void navigator.serviceWorker.register("/sw.js"), { once: true })\n}\n\nif (import.meta.env.VITE_SENTRY_DSN) {\n  init({\n    dsn: import.meta.env.VITE_SENTRY_DSN,\n    environment: import.meta.env.VITE_SENTRY_ENVIRONMENT ?? import.meta.env.MODE,\n    release: import.meta.env.VITE_SENTRY_RELEASE ?? `web@${pkg.version}`,\n    initialScope: {\n      tags: {\n        platform: "web",\n      },\n    },\n    integrations: (integrations) => {\n      return integrations.filter(\n        (i) =>\n          i.name !== "Breadcrumbs" && !(import.meta.env.OPENCODE_CHANNEL === "prod" && i.name === "GlobalHandlers"),\n      )\n    },\n  })\n}\n\nif (root instanceof HTMLElement && root.dataset.opencodeMounted === undefined) {\n  // Lazy chunks can import the entry chunk back under a distinct URL, so claim the root before async startup.\n  root.dataset.opencodeMounted = ""\n  void loadInitialLocale().then((locale) => {\n    const auth = authFromToken(new URLSearchParams(location.search).get("auth_token"))\n    clearAuthToken()\n    const standalone = isStandalone()\n    root.dataset.standalone = String(standalone)\n    if (standalone) restorePwaRoute()\n    const server: ServerConnection.Http | undefined = web.currentServerUrl\n      ? {\n          type: "http",\n          authToken: !!auth,\n          http: {\n            url: web.currentServerUrl,\n            ...auth,\n          },\n        }\n      : undefined\n    render(\n      () => (\n        <PlatformProvider value={web.platform}>\n          <AppBaseProviders locale={locale}>\n            <AppInterface\n              defaultServer={web.defaultServerUrl ? ServerConnection.Key.make(web.defaultServerUrl) : undefined}\n              canonicalLocalServer={server ? ServerConnection.key(server) : undefined}\n              servers={server ? [server] : []}\n            >\n              <KeyboardInsets />\n              {standalone && <PwaRoutePersistence />}\n            </AppInterface>\n          </AppBaseProviders>\n        </PlatformProvider>\n      ),\n      root,\n    )\n  })\n}\n'
PRISTINE_VITE_CONFIG = b'import { sentryVitePlugin } from "@sentry/vite-plugin"\nimport { fileURLToPath } from "node:url"\nimport { defineConfig } from "vite"\nimport desktopPlugin, { channel } from "./vite.js"\nimport { icons } from "./vite.icons"\nimport { serviceWorker } from "./vite.pwa"\n\nconst sentry =\n  process.env.SENTRY_AUTH_TOKEN && process.env.SENTRY_ORG && process.env.SENTRY_PROJECT\n    ? sentryVitePlugin({\n        authToken: process.env.SENTRY_AUTH_TOKEN,\n        org: process.env.SENTRY_ORG,\n        project: process.env.SENTRY_PROJECT,\n        telemetry: false,\n        release: {\n          name: process.env.SENTRY_RELEASE ?? process.env.VITE_SENTRY_RELEASE,\n        },\n        sourcemaps: {\n          assets: "./dist/**",\n          filesToDeleteAfterUpload: "./dist/**/*.map",\n        },\n      })\n    : false\n\nexport default defineConfig({\n  plugins: [\n    desktopPlugin,\n    icons(channel),\n    serviceWorker(fileURLToPath(new URL("./dist", import.meta.url))),\n    sentry,\n  ] as any,\n  server: {\n    host: "0.0.0.0",\n    allowedHosts: true,\n    port: 3000,\n  },\n  build: {\n    ...(process.env.VITE_OPENCODE_TEST_FIXTURES === "1"\n      ? { rolldownOptions: { input: ["index.html", "e2e/utils/settings-wsl.html", "e2e/utils/app-direction.html"] } }\n      : {}),\n    assetsDir: "_assets",\n    target: "esnext",\n    sourcemap: true,\n  },\n})\n'
PRISTINE_INDEX_HTML = b'<!doctype html>\n<html lang="en" style="background-color: var(--v2-background-bg-deep, #fafafa)">\n  <head>\n    <meta charset="utf-8" />\n    <meta\n      name="viewport"\n      content="width=device-width, initial-scale=1, interactive-widget=resizes-content, viewport-fit=cover"\n    />\n    <title>OpenCode</title>\n    <link rel="icon" type="image/x-icon" href="%OPENCODE_FAVICON%" />\n    <link rel="apple-touch-icon" sizes="180x180" href="%OPENCODE_APPLE_TOUCH_ICON%" />\n    <link rel="manifest" href="/site.webmanifest" />\n    <meta name="theme-color" content="#fafafa" />\n    <meta name="mobile-web-app-capable" content="yes" />\n    <meta name="apple-mobile-web-app-capable" content="yes" />\n    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />\n    <meta property="og:image" content="/social-share.png" />\n    <meta property="twitter:image" content="/social-share.png" />\n    <script id="oc-theme-preload-script" src="/oc-theme-preload.js"></script>\n  </head>\n  <body\n    class="antialiased overscroll-none font-(family-name:--font-family-text) text-[13px] font-[440] overflow-hidden bg-v2-background-bg-deep"\n  >\n    <noscript>You need to enable JavaScript to run this app.</noscript>\n    <div id="root" class="flex flex-col h-dvh bg-v2-background-bg-deep p-px"></div>\n    <script src="/src/entry.tsx" type="module"></script>\n  </body>\n</html>\n'

PRISTINE = {
    'packages/app/package.json': PRISTINE_PACKAGE_JSON,
    'packages/app/src/runtime/platform/web.ts': PRISTINE_WEB_TS,
    'packages/app/src/entry.tsx': PRISTINE_ENTRY_TSX,
    'packages/app/vite.config.ts': PRISTINE_VITE_CONFIG,
    'packages/app/index.html': PRISTINE_INDEX_HTML,
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_source(root: Path, files=None) -> Path:
    files = PRISTINE if files is None else files
    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (root / 'packages/app/public').mkdir(parents=True, exist_ok=True)
    return root


def test_fixture_matches_the_pinned_originals():
    """The embedded fixture equals the upstream bytes the pins were derived from."""
    for rel, data in PRISTINE.items():
        assert sha256(data) == bf.PINNED_FILE_HASHES[rel]['original'], rel


def test_prepare_accepts_the_pinned_pristine_tree(tmp_path):
    source = build_source(tmp_path)
    app = bf.prepare(source)
    assert app == source / 'packages/app'
    for rel, spec in bf.PINNED_FILE_HASHES.items():
        assert sha256((source / rel).read_bytes()) == spec['patched'], rel
    assert (app / 'public/mesh-transport.js').is_file()
    assert (app / 'public/mesh-ui.js').is_file()


def test_repeated_prepare_is_idempotent(tmp_path):
    source = build_source(tmp_path)
    bf.prepare(source)
    transport = source / 'packages/app/public/mesh-transport.js'
    before = {rel: (source / rel).read_bytes() for rel in PRISTINE}
    before['packages/app/public/mesh-transport.js'] = transport.read_bytes()
    app = bf.prepare(source)
    assert app == source / 'packages/app'
    for rel, data in before.items():
        assert (source / rel).read_bytes() == data, rel


# Each tamper keeps package.json on version 2.0.15 and leaves every patch
# anchor intact, so the old version-only check would have patched the drift
# silently; the digest gate must reject it.
TAMPERS = {
    'package.json': lambda b: b.replace(
        b'"name": "@opencode/app"',
        b'"name": "@opencode/app-drift"'),
    'src/entry.tsx': lambda b: b.replace(
        b'const web = createWebPlatform(pkg.version)',
        b'const web = createWebPlatform(pkg.version) // drift'),
    'src/runtime/platform/web.ts': lambda b: b.replace(
        b'const DEFAULT_SERVER_URL_KEY = "opencode.settings.dat:defaultServerUrl"',
        b'const DEFAULT_SERVER_URL_KEY = "opencode.settings.dat:drift"'),
}


@pytest.mark.parametrize('rel', sorted(TAMPERS))
def test_same_version_modified_entry_is_rejected(tmp_path, rel):
    files = dict(PRISTINE)
    files['packages/app/' + rel] = TAMPERS[rel](files['packages/app/' + rel])
    source = build_source(tmp_path, files)
    with pytest.raises(ValueError, match='Unrecognized upstream source contract'):
        bf.prepare(source)


def test_bootstrap_awaited_before_render_and_service_worker_disabled(tmp_path):
    source = build_source(tmp_path)
    bf.prepare(source)
    entry = (source / 'packages/app/src/entry.tsx').read_text()
    assert entry.index('await (window as any).__ocmBootstrap.ready') < entry.index(
        'const web = createWebPlatform(pkg.version)')
    assert 'if (false) { // The APK owns its immutable UI resources; no service worker. ' in entry
    assert 'if (import.meta.env.PROD && "serviceWorker" in navigator) {' not in entry
    web = (source / 'packages/app/src/runtime/platform/web.ts').read_text()
    assert 'return (window as any).__ocmBootstrap.serverUrl' in web
    assert '  return location.origin\n' not in web
    vite = (source / 'packages/app/vite.config.ts').read_text()
    assert '    false, // Packaged builds do not emit a service worker.' in vite
    assert '    sourcemap: false,' in vite
    assert 'serviceWorker(fileURLToPath' not in vite
    assert '    sourcemap: true,' not in vite
    index = (source / 'packages/app/index.html').read_text()
    assert '<title>OpenCode Mesh</title>' in index
    assert '<script src="/mesh-transport.js"></script>' in index
    assert '<script src="/mesh-ui.js"></script>' in index
    assert index.index('<script src="/mesh-transport.js"></script>') < index.index('<script src="/mesh-ui.js"></script>')
    assert (source / 'packages/app/public/mesh-ui.js').read_bytes() == (
        Path(__file__).resolve().parents[1] / 'android/ui.js').read_bytes()


def _prepared_with_dist(tmp_path):
    source = build_source(tmp_path)
    app = bf.prepare(source)
    dist = app / 'dist'
    dist.mkdir()
    (dist / 'index.html').write_bytes((app / 'index.html').read_bytes())
    (dist / 'mesh-transport.js').write_bytes((app / 'public/mesh-transport.js').read_bytes())
    (dist / 'mesh-ui.js').write_bytes((app / 'public/mesh-ui.js').read_bytes())
    (source / 'LICENSE').write_text('MIT\n')
    katex = source / 'node_modules/.bun/katex@0.16.47/node_modules/katex'
    katex.mkdir(parents=True)
    (katex / 'LICENSE').write_text('KaTeX MIT license\n')
    plex = source / 'node_modules/.bun/@ibm+plex@6.4.1/node_modules/@ibm/plex'
    plex.mkdir(parents=True)
    (plex / 'LICENSE.txt').write_text('IBM Plex OFL license\n')
    return source, app


def _no_notices(tmp_path):
    return tmp_path / 'no-notices'


def test_package_bundles_the_pinned_dependency_licenses(tmp_path):
    source, app = _prepared_with_dist(tmp_path)
    output = tmp_path / 'out'
    bf.package(app, output, source, notices=_no_notices(tmp_path))
    assert (output / 'LICENSE-OpenCode.txt').read_text() == 'MIT\n'
    assert (output / 'LICENSE-KaTeX.txt').read_text() == 'KaTeX MIT license\n'
    assert (output / 'LICENSE-IBMPlex.txt').read_text() == 'IBM Plex OFL license\n'


def test_package_prefers_the_app_scoped_pinned_license_candidate(tmp_path):
    source, app = _prepared_with_dist(tmp_path)
    app_scoped = source / 'packages/app/node_modules/.bun/katex@0.16.47/node_modules/katex'
    app_scoped.mkdir(parents=True)
    (app_scoped / 'LICENSE').write_text('app-scoped katex license\n')
    output = tmp_path / 'out'
    bf.package(app, output, source, notices=_no_notices(tmp_path))
    assert (output / 'LICENSE-KaTeX.txt').read_text() == 'app-scoped katex license\n'


def test_package_rejects_a_wrong_dependency_version_license(tmp_path):
    source, app = _prepared_with_dist(tmp_path)
    for rel in ('node_modules/.bun/katex@0.16.47/node_modules/katex/LICENSE',
                'node_modules/.bun/@ibm+plex@6.4.1/node_modules/@ibm/plex/LICENSE.txt'):
        (source / rel).unlink()
    drifted = source / 'node_modules/.bun/katex@0.16.46/node_modules/katex'
    drifted.mkdir(parents=True)
    (drifted / 'LICENSE').write_text('drifted katex license\n')
    drifted_plex = source / 'node_modules/.bun/@ibm+plex@6.4.0/node_modules/@ibm/plex'
    drifted_plex.mkdir(parents=True)
    (drifted_plex / 'LICENSE.txt').write_text('drifted plex license\n')
    output = tmp_path / 'out'
    with pytest.raises(ValueError, match='Missing pinned license source'):
        bf.package(app, output, source, notices=_no_notices(tmp_path))
    assert not output.exists()


def test_package_copies_repo_notices_dir_verbatim(tmp_path):
    source, app = _prepared_with_dist(tmp_path)
    notices = tmp_path / 'notices'
    notices.mkdir()
    (notices / 'FONT-LICENSES.txt').write_text('Inter and JetBrains Nerd Font licenses\n')
    output = tmp_path / 'out'
    bf.package(app, output, source, notices=notices)
    assert (output / 'notices' / 'FONT-LICENSES.txt').read_text() == (
        'Inter and JetBrains Nerd Font licenses\n')


def test_package_skips_notices_when_directory_is_absent(tmp_path):
    source, app = _prepared_with_dist(tmp_path)
    output = tmp_path / 'out'
    bf.package(app, output, source, notices=_no_notices(tmp_path))
    assert not (output / 'notices').exists()


def test_package_refuses_to_overwrite_an_existing_output(tmp_path):
    source, app = _prepared_with_dist(tmp_path)
    output = tmp_path / 'out'
    output.mkdir()
    (output / 'web').mkdir()
    with pytest.raises(ValueError, match='fresh assets output'):
        bf.package(app, output, source, notices=_no_notices(tmp_path))
    assert not (output / 'bundle-manifest.json').exists()


def test_package_records_revision_tar_pin_and_file_digests(tmp_path):
    source, app = _prepared_with_dist(tmp_path)
    output = tmp_path / 'out'
    bf.package(app, output, source, notices=_no_notices(tmp_path))
    manifest = json.loads((output / 'bundle-manifest.json').read_text())
    assert manifest['opencodeVersion'] == '2.0.15'
    assert manifest['opencodeRevision'] == bf.REVISION
    assert manifest['sourceTarSha256'] == bf.TAR_SHA256
    assert manifest['meshVersion']
    assert set(manifest['files']) == {'index.html', 'mesh-transport.js', 'mesh-ui.js'}
    for name, digest in manifest['files'].items():
        assert sha256((output / 'web' / name).read_bytes()) == digest
    assert (output / 'LICENSE-OpenCode.txt').is_file()
    assert sorted((output / 'web-assets.txt').read_text().splitlines()) == [
        'index.html', 'mesh-transport.js', 'mesh-ui.js']


def test_pin_constants_are_wellformed():
    assert len(bf.REVISION) == 40
    assert set(bf.REVISION).issubset('0123456789abcdef')
    assert len(bf.TAR_SHA256) == 64
    assert set(bf.TAR_SHA256).issubset('0123456789abcdef')
    pinned = set(bf.PINNED_FILE_HASHES)
    assert pinned == set(bf.PATCHES) | {'packages/app/package.json'}
    for rel, spec in bf.PINNED_FILE_HASHES.items():
        assert set(spec) == {'original', 'patched'}
        assert len(spec['original']) == 64 and len(spec['patched']) == 64
    for rel in bf.PATCHES:
        assert bf.PINNED_FILE_HASHES[rel]['original'] != bf.PINNED_FILE_HASHES[rel]['patched'], rel
    pkg = bf.PINNED_FILE_HASHES['packages/app/package.json']
    assert pkg['original'] == pkg['patched']
    for out_name, candidates in bf.PINNED_LICENSE_FILES.items():
        assert out_name.endswith('.txt') and candidates
        for rel in candidates:
            assert rel.startswith(('packages/app/node_modules/.bun/', 'node_modules/.bun/'))


def test_source_tar_mismatch_is_rejected_before_prepare(tmp_path, monkeypatch):
    source = build_source(tmp_path)
    tar = tmp_path / 'upstream.tar.gz'
    with tarfile.open(tar, 'w:gz') as tf:
        payload = b'not the pinned archive'
        info = tarfile.TarInfo('dummy')
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
    monkeypatch.setattr(
        sys, 'argv',
        ['build_frontend.py', '--source', str(source),
         '--output', str(tmp_path / 'assets'), '--prepare-only', '--source-tar', str(tar)])
    with pytest.raises(ValueError, match='does not match the pinned tar'):
        bf.main()


def test_cli_builds_fresh_assets_instead_of_packaging_stale_dist(tmp_path, monkeypatch):
    import os
    source, app = _prepared_with_dist(tmp_path)
    (app / 'dist/index.html').write_text('stale build')
    binary = tmp_path / 'bin'
    binary.mkdir()
    bun = binary / 'bun'
    bun.write_text('''#!/usr/bin/env python3
import sys
from pathlib import Path
assert sys.argv[1:] == ['run', 'build']
Path('dist/index.html').write_text('freshly compiled UI')
''')
    bun.chmod(0o755)
    monkeypatch.setenv('PATH', str(binary) + os.pathsep + os.environ['PATH'])
    output = tmp_path / 'assets'
    monkeypatch.setattr(sys, 'argv', ['build_frontend.py', '--source', str(source), '--output', str(output)])
    bf.main()
    assert (output / 'web/index.html').read_text() == 'freshly compiled UI'
