"""V2 frontend adapter: isolate cached assets and adapt verified bootstrap and preload contracts."""

import re
import base64
import json
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote


ASSET_ROOT = '/_mesh/ui/2/'

# The Gateway, not any device, describes the installed application. Manifest and
# icons are credentialed Basic Auth resources, so a document without a usable
# link is completed here instead of being served with a manifest the browser
# would fetch without credentials.
PWA_ROOT = '/_mesh/pwa/'
PWA_SW_PATH = '/sw.js'
PWA_MANIFEST_PATH = PWA_ROOT + 'manifest.webmanifest'
PWA_ICON_SIZES = (192, 512)
PWA_ICON_DIR = Path(__file__).resolve().parent / 'assets' / 'pwa'
PWA_MANIFEST_LINK = '<link rel="manifest" crossorigin="use-credentials" href="' + PWA_MANIFEST_PATH + '" />'
PWA_ICON_LINK = '<link rel="icon" href="' + PWA_ROOT + 'icon-192.png" />'
PWA_APPLE_TOUCH_LINK = '<link rel="apple-touch-icon" href="' + PWA_ROOT + 'icon-192.png" />'
# A single line keeps normalization exactly idempotent: no separator is left
# behind when a later pass removes the tags again.
PWA_LINK_BLOCK = PWA_MANIFEST_LINK + PWA_ICON_LINK + PWA_APPLE_TOUCH_LINK
PWA_REGISTRATION_SCRIPT = ('<script id="ocm-pwa-registration">\n'
                           '// The Gateway owns the root-scope worker; a failed registration must not affect the page.\n'
                           "if ('serviceWorker' in navigator) { navigator.serviceWorker.register('" + PWA_SW_PATH + "', { scope: '/' }).catch(function () {}); }\n"
                           '</script>')
# Upstream link relations to replace. A rel value is a token list, so only a
# real rel attribute with one of these tokens makes a link a PWA link.
PWA_REL_TOKENS = frozenset({'manifest', 'icon', 'apple-touch-icon'})


def legacy_server_redirect(path: str, origin: str, device_id: str | None) -> str | None:
    """Migrate only old page bookmarks for the current Gateway origin; external Server identity is left untouched."""
    match = re.fullmatch(r'/server/([^/]+)(/.*)?', path)
    if not match or not device_id:
        return None
    try:
        value = match[1].replace('-', '+').replace('_', '/')
        old = base64.b64decode(value + '=' * (-len(value) % 4), validate=True).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    if old.rstrip('/') != origin:
        return None
    server = origin + '/_mesh/device/' + quote(device_id, safe='')
    key = base64.urlsafe_b64encode(server.encode()).decode().rstrip('=')
    return '/server/' + key + (match[2] or '/')


def asset_prefix(device_id: str) -> str:
    return ASSET_ROOT + quote(device_id, safe='')


def parse_asset_route(path: str) -> tuple[str, str]:
    match = re.fullmatch(re.escape(ASSET_ROOT) + r'([^/]+)(/_assets/.+)', path)
    if not match or '..' in match[2].split('/'):
        raise ValueError('Invalid frontend asset route')
    return match[1], match[2]


def pwa_icon_path(size: int) -> str:
    return PWA_ROOT + f'icon-{size}.png'


def pwa_manifest_document() -> dict:
    """One Gateway-scoped application, independent of which device serves a page."""
    return {
        'name': 'OpenCode Mesh',
        'short_name': 'Mesh',
        # A per-launch handoff parameter would break last-route restore and turn
        # the same installation into several identities.
        'id': '/',
        'start_url': '/',
        # Keep the root scope explicit even if start_url changes in the future.
        'scope': '/',
        'icons': [{'src': pwa_icon_path(size), 'sizes': f'{size}x{size}',
                   'type': 'image/png', 'purpose': 'any maskable'} for size in PWA_ICON_SIZES],
        # Matches the native page background so the standalone window does not flash another color.
        'theme_color': '#fafafa',
        'background_color': '#fafafa',
        'display': 'standalone',
    }


def pwa_service_worker_source(version: str) -> bytes:
    """A worker that only retires the upstream one.

    It registers no fetch handler and never touches CacheStorage: Mesh keeps
    the existing HTTP cache behaviour, and whatever the previous worker left
    behind stays untouched and unused. The embedded version keeps the bytes
    changing on every release so browsers run the update check.
    """
    return ('// OpenCode Mesh gateway service worker, version ' + version + '\n'
            '// There is no fetch handler on purpose: this worker only replaces the\n'
            '// upstream worker registered at the same URL and scope. Mesh serves no\n'
            '// cached content, so requests keep their existing HTTP cache behaviour.\n'
            'self.addEventListener("install", () => { self.skipWaiting(); });\n'
            'self.addEventListener("activate", (event) => { event.waitUntil(self.clients.claim()); });\n').encode()


def load_pwa_icons() -> dict[int, bytes]:
    """Read the packaged icons once at startup so a missing asset is not a runtime surprise."""
    icons = {}
    for size in PWA_ICON_SIZES:
        path = PWA_ICON_DIR / f'icon-{size}.png'
        if not path.is_file():
            raise FileNotFoundError(f'Missing PWA icon asset: {path}')
        icons[size] = path.read_bytes()
    return icons


class _PwaLinkScanner(HTMLParser):
    """Report the byte spans of real PWA link tags and the head boundary.

    A regex cannot decide this: a '>' inside a quoted attribute ends no tag,
    and the same text inside a script, a style or a comment is content rather
    than markup. The tokenizer knows those differences, and get_starttag_text
    returns the exact bytes of a start tag, so the span ends are read from the
    document instead of being guessed.
    """

    def __init__(self, text: str) -> None:
        super().__init__(convert_charrefs=False)
        self._text = text
        # getpos reports a line and a column, so the line starts are needed to
        # turn that into an offset into the text.
        self._line_starts = [0] + [index + 1 for index, character in enumerate(text) if character == '\n']
        self.removed: list[tuple[int, int]] = []
        # Offset of the </head> tag itself: the block goes in front of it.
        self.head_start: int | None = None

    def _offset(self) -> int:
        line, column = self.getpos()
        return self._line_starts[line - 1] + column

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # Self-closing tags arrive through the default handle_startendtag,
        # which delegates here, so '<link ... />' is covered as well.
        if tag != 'link' or not self._is_pwa_link(attrs):
            return
        start = self._offset()
        self.removed.append((start, start + len(self.get_starttag_text() or '')))

    def handle_endtag(self, tag: str) -> None:
        # Only the first head end delimits the block; a second one in a
        # fragment must not turn into a second insertion point.
        if tag == 'head' and self.head_start is None:
            self.head_start = self._offset()

    @staticmethod
    def _is_pwa_link(attrs: list[tuple[str, str | None]]) -> bool:
        # Only a real rel attribute counts: data-rel or aria-rel on a
        # stylesheet must not make the whole tag look like a PWA link.
        return any(name == 'rel' and value and PWA_REL_TOKENS & set(value.lower().split())
                   for name, value in attrs)


def normalize_pwa_links(body: bytes) -> bytes:
    """Point every document at the Gateway manifest and icons.

    Upstream links are root-relative and describe the device that served the
    page. Rewriting them is best effort on purpose: a document that lacks the
    tags, or a fragment without a head, must still render.
    """
    # latin-1 maps every byte to exactly one character, so the spans the
    # scanner reports are byte offsets and everything outside them is returned
    # unchanged; re-serializing the document would rewrite unrelated bytes.
    text = body.decode('latin-1')
    scanner = _PwaLinkScanner(text)
    scanner.feed(text)
    scanner.close()

    # Removing every upstream tag first is what makes repeated rewrites
    # converge: duplicates collapse and no device-scoped link survives.
    # Splicing from the end keeps the remaining offsets valid.
    for start, end in reversed(scanner.removed):
        text = text[:start] + text[end:]

    if scanner.head_start is None:
        return (text + PWA_LINK_BLOCK).encode('latin-1')
    # The boundary moved by whatever was removed in front of it.
    at = scanner.head_start - sum(min(end, scanner.head_start) - start
                                  for start, end in scanner.removed if start < scanner.head_start)
    return (text[:at] + PWA_LINK_BLOCK + text[at:]).encode('latin-1')


def adapt_entry(path: str, body: bytes, device_id: str | None = None) -> bytes:
    if re.fullmatch(r'/_assets/preload-helper-[\w-]+\.js', path):
        # Verified Vite helpers prefix dependency-table entries with the origin root.
        # Bind that prefix to the source device, independently of the active Server.
        prefix = rb'function\(([\w$]+)\)\{return([\x22\x27`])/\2\+\1\}'
        if not device_id or len(re.findall(prefix, body)) != 1:
            raise ValueError('Unsupported OpenCode preload contract')
        root = json.dumps(asset_prefix(device_id) + '/').encode()
        return re.sub(prefix, lambda m: b'function(' + m[1] + b'){return ' + root + b'+' + m[1] + b'}', body)
    if not re.fullmatch(r'/_assets/index-[\w-]+\.js', path):
        return body
    # The getter must be unique in a real V2.0.6 bundle; if a newer build changes
    # the structure, fail explicitly instead of guessing a replacement.
    getter = rb'(function [\w$]+\(\)\{return )location\.origin(\})'
    if b'currentServerUrl' not in body or b'defaultServerUrl' not in body or len(re.findall(getter, body)) != 1:
        raise ValueError('Unsupported OpenCode bootstrap contract')
    patched = re.sub(getter, rb'\1window.__ocmBootstrap.serverUrl\2', body)
    return b'await window.__ocmBootstrap.ready;\n' + patched
