// APK-only UI overlay for the bundled OpenCode frontend.
//
// Responsibilities:
//  - append a dots (U+22EE) menu on the right edge of the existing
//    #ocm-mesh-bar created by the mesh transport adapter, offering
//    "Gateway settings" and "Reload";
//  - report the real, browser-computed page background and light/dark
//    appearance to the native container so the window, system bars and the
//    native dialog can follow the page theme.
//
// The container bridge is deliberately narrow: only ocm-app:// main-frame
// navigations (settings/reload/theme) are accepted on the native side.
// No JavascriptInterface is exposed to any frame, no prompt()/evaluate
// bridge is used, and this script never handles or even mentions
// credentials. The theme payload carries only r/g/b/a/dark integers.
// The theme color is resolved by the browser itself (computed style through
// a canvas readback), never by treating a raw token string as a color.
(function () {
  'use strict';

  var BAR_ID = 'ocm-mesh-bar';
  var MENU_ID = 'ocm-ui-menu';
  var LAST_REPORT = null;
  var themeTimer = null;

  var MENU_CSS = '' +
    '#ocm-mesh-bar .ocm-menu-button{display:flex;align-items:center;justify-content:center;' +
    'height:24px;min-width:28px;margin-left:6px;padding:0 6px;border:1px solid transparent;' +
    'border-radius:6px;background:transparent;' +
    'color:var(--v2-text-text-faint,#8a8a8a);font-size:15px;line-height:22px;cursor:pointer}' +
    '#ocm-mesh-bar .ocm-menu-button:hover{background:var(--v2-overlay-simple-overlay-hover,rgba(0,0,0,0.04));' +
    'color:var(--v2-text-text-base,#1a1a1a)}' +
    '#ocm-mesh-bar .ocm-menu-button[aria-expanded="true"]{background:var(--v2-overlay-simple-overlay-pressed,rgba(0,0,0,0.08));' +
    'color:var(--v2-text-text-base,#1a1a1a)}' +
    '.ocm-menu{position:fixed;top:38px;right:10px;z-index:2147483000;min-width:180px;padding:4px;' +
    'border:1px solid var(--v2-border-border-base,#e5e5e5);border-radius:8px;' +
    'background:var(--v2-background-bg-layer-01,#ffffff);' +
    'box-shadow:0 8px 24px var(--v2-overlay-simple-overlay-scrim,rgba(0,0,0,0.2));' +
    'font-size:13px;line-height:20px;color:var(--v2-text-text-base,#1a1a1a);' +
    '-webkit-user-select:none;user-select:none}' +
    '.ocm-menu-item{display:block;width:100%;padding:8px 10px;border:0;border-radius:6px;' +
    'background:transparent;color:var(--v2-text-text-base,#1a1a1a);font:inherit;text-align:left;cursor:pointer}' +
    '.ocm-menu-item:hover{background:var(--v2-overlay-simple-overlay-hover,rgba(0,0,0,0.04))}' +
    '.ocm-menu-item:active{background:var(--v2-overlay-simple-overlay-pressed,rgba(0,0,0,0.08))}';

  function ensureStyle() {
    var style = document.getElementById('ocm-ui-style');
    if (style) return;
    style = document.createElement('style');
    style.id = 'ocm-ui-style';
    style.textContent = MENU_CSS;
    document.head.appendChild(style);
  }

  // Build a location navigation the container understands. Only these three
  // shapes are ever produced; parsing on the Java side is strict.
  function bridge(host, query) {
    return 'ocm-app://' + host + (query ? '?' + query : '');
  }

  function close(menu, button) {
    if (menu) menu.style.display = 'none';
    if (button) button.setAttribute('aria-expanded', 'false');
  }

  function menuItem(label, action) {
    var item = document.createElement('button');
    item.type = 'button';
    item.className = 'ocm-menu-item';
    item.setAttribute('role', 'menuitem');
    item.textContent = label;
    item.addEventListener('click', action);
    return item;
  }

  function installMenu(bar) {
    if (!bar || bar.querySelector('.ocm-menu-button')) return;
    ensureStyle();
    var button = document.createElement('button');
    button.type = 'button';
    button.className = 'ocm-menu-button';
    button.setAttribute('aria-label', 'Mesh options');
    button.setAttribute('aria-haspopup', 'menu');
    button.setAttribute('aria-expanded', 'false');
    button.textContent = '\u22ee'; // vertical ellipsis glyph, no font dependency
    var menu = document.createElement('div');
    menu.id = MENU_ID;
    menu.className = 'ocm-menu';
    menu.setAttribute('role', 'menu');
    menu.style.display = 'none';
    menu.appendChild(menuItem('Gateway settings', function () {
      close(menu, button);
      location.href = bridge('settings');
    }));
    menu.appendChild(menuItem('Reload', function () {
      close(menu, button);
      location.href = bridge('reload');
    }));
    button.addEventListener('click', function (event) {
      event.stopPropagation();
      var open = menu.style.display !== 'none';
      close(menu, button);
      if (!open) {
        menu.style.display = 'block';
        button.setAttribute('aria-expanded', 'true');
      }
    });
    document.addEventListener('click', function (event) {
      if (event.target === button) return;
      if (menu.contains && menu.contains(event.target)) return;
      close(menu, button);
    });
    bar.appendChild(button);
    document.body.appendChild(menu);
  }

  // ---- theme probe ---------------------------------------------------------

  // Let the browser normalize an already-computed color (oklch(), color(),
  // 8-digit hex, nested var() chains, ...) to RGBA bytes via a canvas
  // readback. We never parse color strings ourselves and never treat a raw
  // token or declaration as a hex value.
  function resolveToRgba(value) {
    if (!value || value === 'transparent' || value === 'rgba(0, 0, 0, 0)') return null;
    try {
      var canvas = document.createElement('canvas');
      canvas.width = 1;
      canvas.height = 1;
      var ctx = canvas.getContext('2d');
      ctx.clearRect(0, 0, 1, 1);
      ctx.fillStyle = value;
      ctx.fillRect(0, 0, 1, 1);
      var data = ctx.getImageData(0, 0, 1, 1).data;
      return { r: data[0], g: data[1], b: data[2], a: data[3] };
    } catch (_) {
      return null;
    }
  }

  function probeBackground() {
    var candidates = [document.documentElement, document.body];
    for (var i = 0; i < candidates.length; i++) {
      if (!candidates[i]) continue;
      var rgba = resolveToRgba(getComputedStyle(candidates[i]).backgroundColor);
      if (rgba && rgba.a > 0) return rgba;
    }
    // The --v2-background-bg-deep token is the page's designated canvas
    // background; resolve it through a hidden probe element so the browser
    // computes the final color (token chains may nest var()/oklch).
    try {
      var probe = document.createElement('div');
      probe.style.position = 'absolute';
      probe.style.width = '1px';
      probe.style.height = '1px';
      probe.style.visibility = 'hidden';
      probe.style.pointerEvents = 'none';
      probe.style.backgroundColor = 'var(--v2-background-bg-deep)';
      document.body.appendChild(probe);
      var resolved = resolveToRgba(getComputedStyle(probe).backgroundColor);
      probe.remove();
      if (resolved && resolved.a > 0) return resolved;
    } catch (_) {}
    return { r: 250, g: 250, b: 250, a: 255 };
  }

  function channel(value) {
    var c = value / 255;
    return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
  }

  function isDark(rgba) {
    var luminance = 0.2126 * channel(rgba.r) + 0.7152 * channel(rgba.g) + 0.0722 * channel(rgba.b);
    return luminance < 0.5;
  }

  function reportTheme(force) {
    var rgba = probeBackground();
    if (!rgba) return;
    var dark = isDark(rgba);
    var next = rgba.r + ',' + rgba.g + ',' + rgba.b + ',' + rgba.a + ',' + (dark ? 1 : 0);
    if (next === LAST_REPORT && !force) return;
    LAST_REPORT = next;
    location.href = bridge('theme', 'r=' + rgba.r + '&g=' + rgba.g + '&b=' + rgba.b + '&a=' + rgba.a + '&dark=' + (dark ? 1 : 0));
  }

  function scheduleReport() {
    if (themeTimer) return;
    themeTimer = setTimeout(function () {
      themeTimer = null;
      reportTheme(false);
    }, 150);
  }

  function watchTheme() {
    if (typeof MutationObserver !== 'undefined' && document.documentElement) {
      var observer = new MutationObserver(function () { scheduleReport(); });
      observer.observe(document.documentElement, {
        attributes: true,
        attributeFilter: ['data-color-scheme', 'data-theme', 'style'],
      });
    }
    if (typeof matchMedia === 'function') {
      var media = matchMedia('(prefers-color-scheme: dark)');
      if (typeof media.addEventListener === 'function') {
        media.addEventListener('change', scheduleReport);
      } else if (typeof media.addListener === 'function') {
        media.addListener(scheduleReport);
      }
    }
  }

  function boot() {
    var bar = document.getElementById(BAR_ID);
    if (bar) {
      installMenu(bar);
      watchTheme();
      reportTheme(false);
      return;
    }
    // The transport adapter creates the bar when the page is interactive;
    // wait for it without touching the production adapter.
    if (typeof MutationObserver !== 'undefined' && document.documentElement) {
      var watcher = new MutationObserver(function () {
        var found = document.getElementById(BAR_ID);
        if (found) {
          watcher.disconnect();
          boot();
        }
      });
      watcher.observe(document.documentElement, { childList: true, subtree: true });
    } else {
      var tries = 0;
      var timer = setInterval(function () {
        tries += 1;
        var found = document.getElementById(BAR_ID);
        if (found || tries > 100) {
          clearInterval(timer);
          if (found) boot();
        }
      }, 100);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();