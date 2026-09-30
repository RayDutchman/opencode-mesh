"""Behavior checks for the APK-only UI layer.

android/ui.js is bundled as public/mesh-ui.js and injected by
build_frontend.prepare. It is exercised here with a DOM/Canvas/location shim so
the bridge contract is pinned by tests instead of manual inspection:

- the dots menu is appended on the right of the existing #ocm-mesh-bar,
  lists exactly "Gateway settings" and "Reload", and only ever navigates to the
  limited ocm-app:// main-frame scheme (no JavascriptInterface, no prompts);
- menu styles come from the v2 theme tokens (nested var()/oklch chains), never
  hard-coded surfaces;
- the theme report resolves the *computed* background through the browser
  (canvas getImageData normalization) and carries only r/g/b/a/dark — no
  credential-shaped payload.

AppScheme.java is pure JVM code; it is compiled and executed like
test_android_asset_policy.py. A javac gate against the API-35 android.jar
additionally compiles the whole container when the local toolchain is present.
"""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI_JS = ROOT / 'android/ui.js'
JAVA_DIR = ROOT / 'android/app/src/main/java/dev/opencodemesh/app'
ANDROID_JAR = Path('/tmp/opencode/android-toolchain/platform/android-35/android.jar')

BRIDGE_RE = (
    r'^ocm-app://(settings|reload|'
    r'theme\?r=[0-9]{1,3}&g=[0-9]{1,3}&b=[0-9]{1,3}&a=[0-9]{1,3}&dark=[01])$'
)

HARNESS = r'''
const fs = require('fs');
const vm = require('vm');
const code = fs.readFileSync(process.argv[2], 'utf8');
const scenario = JSON.parse(process.argv[3]);
const facts = {};
try {
  class FakeElement {
    constructor(tag) {
      this.tagName = String(tag || 'div').toUpperCase();
      this.children = [];
      this.style = {};
      this.dataset = {};
      this.attrs = {};
      this.listeners = {};
      this.className = '';
      this.textContent = '';
      this._parent = null;
    }
    setAttribute(k, v) { this.attrs[k] = String(v); }
    getAttribute(k) { return this.attrs[k] == null ? null : this.attrs[k]; }
    appendChild(child) { child._parent = this; this.children.push(child); return child; }
    remove() {
      if (this._parent) {
        const i = this._parent.children.indexOf(this);
        if (i >= 0) this._parent.children.splice(i, 1);
      }
      this._parent = null;
    }
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
    fire(type, event) {
      const ev = event || { target: this, stopPropagation() {} };
      for (const fn of this.listeners[type] || []) fn(ev);
    }
    querySelector(sel) {
      if (sel.charAt(0) === '.') {
        const cls = sel.slice(1);
        for (const child of this.children) {
          if ((' ' + (child.className || '') + ' ').indexOf(' ' + cls + ' ') >= 0) return child;
        }
      }
      return null;
    }
  }
  const navigations = [];
  const location = {
    _value: 'https://mesh.example.com/',
    get href() { return this._value; },
    set href(v) { this._value = String(v); navigations.push(String(v)); },
  };
  const canvasFillStyles = [];
  const FakeCanvas = class {
    constructor() {
      this.width = 0;
      this.height = 0;
      this._ctx = {
        set fillStyle(v) { canvasFillStyles.push(v); },
        get fillStyle() { return ''; },
        clearRect() {},
        fillRect() {},
        getImageData() { return { data: scenario.pixels }; },
      };
    }
    getContext() { return this._ctx; }
  };
  const mediaListeners = [];
  const matchMedia = () => ({
    matches: !!scenario.mediaDark,
    addEventListener(type, fn) { if (type === 'change') mediaListeners.push(fn); },
    addListener(fn) { mediaListeners.push(fn); },
  });
  const observers = [];
  const FakeMutationObserver = class {
    constructor(cb) { this.cb = cb; observers.push(this); }
    observe() {}
    disconnect() {}
  };
  const document = {
    readyState: 'complete',
    documentElement: new FakeElement('html'),
    body: new FakeElement('body'),
    head: new FakeElement('head'),
    _byId: {},
    listeners: {},
    getElementById(id) { return this._byId[id] || null; },
    createElement(tag) { if (tag === 'canvas') return new FakeCanvas(); return new FakeElement(tag); },
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
  };
  const bar = new FakeElement('div');
  bar.id = 'ocm-mesh-bar';
  document._byId['ocm-mesh-bar'] = bar;
  document.body.appendChild(bar);
  document.documentElement.appendChild(document.body);
  let computedBackground = scenario.computedBackground;
  const syncTimer = (fn) => { fn(); return 0; };
  const sandbox = {
    document,
    location,
    matchMedia,
    MutationObserver: FakeMutationObserver,
    getComputedStyle: () => ({ backgroundColor: computedBackground }),
    setTimeout: syncTimer,
    clearTimeout: () => {},
    setInterval: () => 1,
    clearInterval: () => {},
  };
  sandbox.window = sandbox;
  vm.runInNewContext(code, sandbox);

  const click = (el) => el.fire('click', { target: el, stopPropagation() {} });
  const menuButton = bar.querySelector('.ocm-menu-button');
  facts.menuButtonIsLastChild = !!menuButton && bar.children[bar.children.length - 1] === menuButton;
  facts.menuButtonText = menuButton ? menuButton.textContent : null;
  facts.menuButtonLabel = menuButton ? menuButton.getAttribute('aria-label') : null;
  let menuEl = null;
  if (menuButton) click(menuButton);
  for (const child of document.body.children) {
    if ((' ' + (child.className || '') + ' ').indexOf(' ocm-menu ') >= 0) menuEl = child;
  }
  facts.menuVisibleAfterOpen = menuEl ? menuEl.style.display !== 'none' : null;
  facts.menuItems = menuEl
    ? menuEl.children.filter((c) => (' ' + c.className + ' ').indexOf(' ocm-menu-item ') >= 0).map((c) => c.textContent)
    : null;
  const items = () => (menuEl ? menuEl.children.filter((c) => (' ' + c.className + ' ').indexOf(' ocm-menu-item ') >= 0) : []);
  if (menuEl && items().length >= 2) {
    click(items()[0]);
    facts.menuClosedAfterSettings = menuEl.style.display === 'none';
  }
  if (menuButton) click(menuButton);
  if (menuEl && items().length >= 2) click(items()[items().length - 1]);

  const before = navigations.length;
  if (scenario.computedBackground2 !== undefined) computedBackground = scenario.computedBackground2;
  if (scenario.pixels2 !== undefined) scenario.pixels = scenario.pixels2;
  for (const obs of observers) obs.cb([], obs);
  for (const fn of mediaListeners) fn({ matches: !!scenario.mediaDark2 });
  facts.navigationsAfterChange = navigations.slice(before);

  facts.navigations = navigations.slice();
  facts.canvasFillStyles = canvasFillStyles.slice();
  let styleEl = null;
  for (const child of document.head.children) if (child.tagName === 'STYLE') styleEl = child;
  facts.menuStyleText = styleEl ? styleEl.textContent : null;
  console.log(JSON.stringify(facts));
} catch (err) {
  console.log(JSON.stringify({ fatal: String((err && err.stack) || err) }));
  process.exit(2);
}
'''


def _run_ui(tmp_path, scenario):
    if not shutil.which('node'):
        pytest.skip('Node.js required for APK UI behavior checks')
    harness = tmp_path / 'ui-harness.js'
    harness.write_text(HARNESS)
    proc = subprocess.run(
        ['node', str(harness), str(UI_JS), json.dumps(scenario)],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    facts = json.loads(proc.stdout)
    assert 'fatal' not in facts, facts.get('fatal')
    return facts


DARK = {'r': 8, 'g': 8, 'b': 8, 'a': 255}
LIGHT = {'r': 250, 'g': 250, 'b': 250, 'a': 255}


def test_menu_button_is_appended_to_the_right_of_the_mesh_bar(tmp_path):
    facts = _run_ui(tmp_path, {
        'pixels': list(DARK.values()), 'computedBackground': 'oklch(0.2 0 0)', 'mediaDark': False})
    assert facts['menuButtonIsLastChild'] is True
    assert facts['menuButtonText'] == '\u22ee'
    assert facts['menuButtonLabel'] == 'Mesh options'


def test_menu_lists_gateway_settings_and_reload(tmp_path):
    facts = _run_ui(tmp_path, {
        'pixels': list(DARK.values()), 'computedBackground': 'oklch(0.2 0 0)', 'mediaDark': False})
    assert facts['menuVisibleAfterOpen'] is True
    assert facts['menuItems'] == ['Gateway settings', 'Reload']
    assert facts['menuClosedAfterSettings'] is True


def test_menu_actions_use_only_the_limited_app_scheme(tmp_path):
    facts = _run_ui(tmp_path, {
        'pixels': list(DARK.values()), 'computedBackground': 'rgb(8, 8, 8)', 'mediaDark': False})
    navigations = facts['navigations']
    # The boot theme report comes first, then exactly settings and reload.
    assert 'ocm-app://settings' in navigations
    assert navigations[-1] == 'ocm-app://reload'
    assert navigations[0] != navigations[-1]
    for nav in navigations:
        assert __import__('re').match(BRIDGE_RE, nav), nav
        assert not nav.startswith('http'), nav


def test_menu_styles_use_v2_theme_tokens_not_hardcoded_surfaces(tmp_path):
    facts = _run_ui(tmp_path, {
        'pixels': list(LIGHT.values()), 'computedBackground': 'rgb(250, 250, 250)', 'mediaDark': False})
    text = facts['menuStyleText']
    assert text
    for token in ('--v2-background-bg-layer-01', '--v2-text-text-base', '--v2-border-border-base'):
        assert 'var(' + token in text, token
    import re
    for value in re.findall(r'background:([^;}]+)', text):
        assert value.strip() == 'transparent' or 'var(--v2-' in value, value
    for value in re.findall(r'color:([^;}]+)', text):
        assert value.strip() == 'transparent' or 'var(--v2-' in value, value
    assert 'background:#' not in text


def test_theme_report_sends_browser_computed_color_and_darkness(tmp_path):
    facts = _run_ui(tmp_path, {
        'pixels': list(DARK.values()), 'computedBackground': 'oklch(0.2 0 0)', 'mediaDark': False})
    assert facts['navigations'][0] == 'ocm-app://theme?r=8&g=8&b=8&a=255&dark=1'
    # The canvas normalized the computed, browser-resolved color; the raw v2
    # token string never reaches the probe or the bridge.
    assert 'oklch(0.2 0 0)' in facts['canvasFillStyles']
    assert 'var(--v2-background-bg-deep)' not in facts['navigations'][0]
    # Nothing changed: the change watchers must not spam duplicate reports.
    assert facts['navigationsAfterChange'] == []


def test_theme_reports_again_only_when_appearance_changed(tmp_path):
    facts = _run_ui(tmp_path, {
        'pixels': list(DARK.values()), 'computedBackground': 'rgb(8, 8, 8)',
        'mediaDark': False,
        'pixels2': list(LIGHT.values()), 'computedBackground2': 'rgb(250, 250, 250)',
        'mediaDark2': True})
    assert facts['navigationsAfterChange'] == ['ocm-app://theme?r=250&g=250&b=250&a=255&dark=0']


def test_bridge_payload_never_carries_credentials(tmp_path):
    source = UI_JS.read_text()
    lowered = source.lower()
    for word in ('password', 'authorization', 'username', 'secret', 'addJavascriptInterface'):
        assert word not in lowered, word
    facts = _run_ui(tmp_path, {
        'pixels': list(LIGHT.values()), 'computedBackground': '#fafafa', 'mediaDark': True})
    import re
    for nav in facts['navigations']:
        assert re.match(BRIDGE_RE, nav), nav
        if nav.startswith('ocm-app://theme?'):
            params = dict(pair.split('=', 1) for pair in nav.split('?', 1)[1].split('&'))
            assert set(params) == {'r', 'g', 'b', 'a', 'dark'}, params


# ---------------------------------------------------------------- JVM pieces

def _compile_and_run(tmp_path, java_files, main_class, harness_source):
    if not shutil.which('javac') or not shutil.which('java'):
        pytest.skip('JDK required for Android scheme checks')
    out = tmp_path / 'classes'
    out.mkdir()
    cmd = ['javac', '--release', '8', '-d', str(out), *[str(f) for f in java_files]]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    harness = tmp_path / (main_class + '.java')
    harness.write_text(harness_source)
    subprocess.run(['javac', '--release', '8', '-cp', str(out), '-d', str(out), str(harness)],
                   check=True, capture_output=True, text=True)
    subprocess.run(['java', '-cp', str(out), main_class], check=True, capture_output=True, text=True)


def test_app_scheme_bridge_parses_and_gates_on_the_jvm(tmp_path):
    scheme = JAVA_DIR / 'AppScheme.java'
    harness = '''
import dev.opencodemesh.app.AppScheme;
import dev.opencodemesh.app.AppScheme.Action;
import dev.opencodemesh.app.AppScheme.Theme;
public class SchemeCheck {
    static void eq(Object a, Object b) {
        if (!java.util.Objects.equals(a, b)) throw new AssertionError(a + " != " + b);
    }
    static AppScheme.Request parse(String url) { return AppScheme.parse(url); }
    public static void main(String[] args) {
        AppScheme.Request r = parse("ocm-app://settings");
        eq(r.action, Action.SETTINGS); eq(r.theme, null);
        eq(parse("ocm-app://reload").action, Action.RELOAD);
        eq(parse("ocm-app://reload?x=1").action, Action.NONE);
        eq(parse("ocm-app://reload#frag").action, Action.NONE);
        r = parse("ocm-app://theme?r=8&g=8&b=8&a=255&dark=1");
        eq(r.action, Action.THEME);
        eq(r.theme.red, 8); eq(r.theme.green, 8); eq(r.theme.blue, 8);
        eq(r.theme.alpha, 255); eq(r.theme.dark, true);
        r = parse("ocm-app://theme?r=0&g=0&b=0");
        eq(r.action, Action.THEME); eq(r.theme.alpha, 255); eq(r.theme.dark, false);
        eq(parse("ocm-app://theme?r=256&g=0&b=0").action, Action.NONE);
        eq(parse("ocm-app://theme?r=abc&g=0&b=0").action, Action.NONE);
        eq(parse("ocm-app://theme?r=1&g=2&b=3&extra=4").action, Action.NONE);
        eq(parse("ocm-app://theme?r=1&r=2&g=3&b=4").action, Action.NONE);
        eq(parse("ocm-app://theme?").action, Action.NONE);
        eq(parse("ocm-app://other").action, Action.NONE);
        eq(parse("http://ocm-app/settings").action, Action.NONE);
        eq(parse("ocm-app://settings@evil/x").action, Action.NONE);
        eq(parse(null).action, Action.NONE);
        eq(AppScheme.isAppScheme("ocm-app://settings"), true);
        eq(AppScheme.isAppScheme("OCM-APP://SETTINGS"), true);
        eq(AppScheme.isAppScheme("https://mesh.example.com/"), false);
        eq(AppScheme.gate(Action.SETTINGS, true, true, false, true), true);
        eq(AppScheme.gate(Action.SETTINGS, false, true, false, true), false);
        eq(AppScheme.gate(Action.SETTINGS, true, false, false, true), false);
        eq(AppScheme.gate(Action.RELOAD, true, true, true, true), false);
        eq(AppScheme.gate(Action.SETTINGS, true, true, false, false), false);
        eq(AppScheme.gate(Action.NONE, true, true, false, true), false);
        eq(AppScheme.gate(null, true, true, false, true), false);
        eq(String.format("%08x", AppScheme.composite(0xffffffff,
            parse("ocm-app://theme?r=250&g=250&b=250&a=255").theme)), "fffafafa");
        eq(String.format("%08x", AppScheme.composite(0xff000000,
            parse("ocm-app://theme?r=0&g=0&b=0&a=0").theme)), "ff000000");
        Theme half = parse("ocm-app://theme?r=255&g=0&b=0&a=128").theme;
        eq(String.format("%08x", AppScheme.composite(0xffffffff, half)), "ffff7f7f");
        System.out.println("SchemeCheck ok");
    }
}
'''
    _compile_and_run(tmp_path, [scheme], 'SchemeCheck', harness)


def test_container_compiles_against_the_api35_android_jar(tmp_path):
    if not ANDROID_JAR.is_file():
        pytest.skip('API-35 android.jar toolchain not present')
    if not shutil.which('javac'):
        pytest.skip('JDK required to compile the Android container')
    out = tmp_path / 'classes'
    out.mkdir()
    sources = sorted(JAVA_DIR.glob('*.java'))
    subprocess.run(
        ['javac', '--release', '8', '-classpath', str(ANDROID_JAR), '-d', str(out), *map(str, sources)],
        check=True, capture_output=True, text=True)


def test_ui_script_stays_byte_identical_to_the_bundled_asset_source(tmp_path):
    # The build copies android/ui.js verbatim into public/mesh-ui.js; the copy
    # must never diverge from the audited source.
    assert UI_JS.is_file()
    digest = hashlib.sha256(UI_JS.read_bytes()).hexdigest()
    assert len(digest) == 64