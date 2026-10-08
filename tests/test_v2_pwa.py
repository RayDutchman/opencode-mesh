"""Gateway-owned PWA surface: manifest, icons, root-scope worker, and their HTML links.

The Gateway is the only origin that may describe the installed application.
The worker registered at ``/sw.js`` must therefore come from the Gateway and
must not answer any request: Mesh keeps its existing HTTP cache behaviour and
never adds a CacheStorage subsystem. These tests pin that contract through real
ASGI requests plus the real HTML rewriter, and they never touch a device.
"""
import asyncio
import base64
import hashlib
import json
import re
import struct
import subprocess
import tempfile
import time
import zlib
from pathlib import Path

from src.static_adapter import TRANSPORT_ADAPTER

import httpx
import pytest

from src import __version__
from src.frontend import (PWA_ICON_DIR, PWA_ICON_SIZES, PWA_LINK_BLOCK, PWA_MANIFEST_PATH,
                          asset_prefix, normalize_pwa_links, pwa_service_worker_source)
from src.main import Gateway, OFFLINE_PAGE, rewrite_device_html

# Provenance of the launcher icons, recorded in src/assets/pwa/README.md.
# Upstream source: packages/desktop/icons/prod/ in anomalyco/opencode v2.0.18
# (MIT, Copyright (c) 2025 opencode). Preserve the original PNG bytes, including
# alpha, as served by the upstream /site.webmanifest icon URLs.
ICON_SHA256 = {
    192: 'a2aedd1def885e3b7d7adc7668725c3772996f1699c4524252f752f55707101b',
    512: '324bd6ab9499f006519209eaa883519f37b9373a59d6eb01235189d4ac67ea27',
}

# Upstream v2.0.18 packages/app/index.html head, reduced to the link tags Mesh replaces.
UPSTREAM_LINK_TAGS = (
    '<link rel="icon" type="image/x-icon" href="/icons/prod/favicon.ico" />'
    '<link rel="apple-touch-icon" sizes="180x180" href="/icons/prod/apple-touch-icon.png" />'
    '<link rel="manifest" href="/site.webmanifest" />'
)


def _gateway(state_file: Path) -> Gateway:
    return Gateway({'state_file': str(state_file),
                    'auth': {'username': 'test', 'password': 'test-pass'}})


def _png_size(body: bytes) -> tuple[int, int]:
    """Read PNG dimensions from IHDR so the icon geometry is asserted without an image library."""
    assert body[:8] == b'\x89PNG\r\n\x1a\n', 'icons must be real PNG bytes'
    width, height = struct.unpack('>II', body[16:24])
    return width, height


def _client(gateway: Gateway, **kwargs):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app),
                             base_url='http://test', auth=('test', 'test-pass'), **kwargs)


def _anonymous(gateway: Gateway, **kwargs):
    """A browser that has not answered the Basic Auth challenge."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app),
                             base_url='http://test', **kwargs)


async def _fetch_icons(client: httpx.AsyncClient) -> dict[int, bytes]:
    icons = {}
    for size in PWA_ICON_SIZES:
        response = await client.get(f'/_mesh/pwa/icon-{size}.png')
        assert response.status_code == 200, size
        icons[size] = response.content
    return icons


def _decode_png(body: bytes) -> tuple[int, int, bytearray, int]:
    """Decode the 8-bit non-interlaced PNGs committed here, without an image dependency."""
    assert body[:8] == b'\x89PNG\r\n\x1a\n', 'icons must be real PNG bytes'
    width = height = channels = 0
    compressed = bytearray()
    position = 8
    while position < len(body):
        length, kind = struct.unpack('>I4s', body[position:position + 8])
        chunk = body[position + 8:position + 8 + length]
        if kind == b'IHDR':
            width, height, depth, color, _, _, interlace = struct.unpack('>IIBBBBB', chunk)
            assert depth == 8 and interlace == 0, 'icons must be 8-bit non-interlaced'
            assert color in (2, 6), 'icons must be RGB or RGBA'
            channels = 3 if color == 2 else 4
        elif kind == b'IDAT':
            compressed += chunk
        elif kind == b'IEND':
            break
        position += 12 + length
    raw = zlib.decompress(bytes(compressed))
    stride = width * channels
    pixels = bytearray(height * stride)
    previous = bytearray(stride)
    for row in range(height):
        start = row * (stride + 1)
        filter_type = raw[start]
        line = bytearray(raw[start + 1:start + 1 + stride])
        for index in range(stride):
            left = line[index - channels] if index >= channels else 0
            up = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_type == 1:
                line[index] = (line[index] + left) & 0xFF
            elif filter_type == 2:
                line[index] = (line[index] + up) & 0xFF
            elif filter_type == 3:
                line[index] = (line[index] + ((left + up) >> 1)) & 0xFF
            elif filter_type == 4:
                estimate = left + up - upper_left
                distances = (abs(estimate - left), abs(estimate - up), abs(estimate - upper_left))
                line[index] = (line[index] + (left, up, upper_left)[distances.index(min(distances))]) & 0xFF
        pixels[row * stride:(row + 1) * stride] = line
        previous = line
    return width, height, pixels, channels


# ---------- worker ----------

def test_root_scope_worker_takes_over_without_answering_requests(tmp_path):
    """The worker replaces the upstream one at the same URL and must not serve or store anything."""
    async def scenario():
        gateway = _gateway(tmp_path / 'registry.json')
        # A device that records every forwarded request: the PWA surface must not reach it.
        forwarded: list[str] = []

        async def send_control(ws, item, **kwargs):
            forwarded.append(item['path'])

        gateway.send_control = send_control
        gateway.registry.devices['device-a'] = {'device_id': 'device-a', 'name': 'Device A',
                                                 'last_seen': time.time(), 'ws': object()}
        async with _client(gateway) as client:
            response = await client.get('/sw.js')
            assert response.status_code == 200
            assert response.headers['content-type'].startswith('text/javascript')
            assert response.headers['cache-control'] == 'no-cache'
            assert response.headers['service-worker-allowed'] == '/'
            assert forwarded == [], 'the worker is Gateway-owned, never a device request'
            return response.text
    body = asyncio.run(scenario())

    assert f'version {__version__}' in body
    assert 'self.addEventListener("install"' in body
    assert 'self.skipWaiting()' in body
    assert 'self.addEventListener("activate"' in body
    assert 'self.clients.claim()' in body
    # No fetch handler, no CacheStorage access, no request replay: Mesh keeps HTTP cache semantics.
    assert 'addEventListener("fetch"' not in body
    assert 'respondWith' not in body
    assert 'caches' not in body
    assert 'importScripts' not in body
    with tempfile.TemporaryDirectory() as directory:
        script = Path(directory) / 'sw.js'
        script.write_text(body, encoding='utf-8')
        checked = subprocess.run(['node', '--check', str(script)], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr


def test_worker_version_comment_changes_with_every_release():
    """Byte changes are what make browsers run the update check, so the version must be embedded."""
    assert pwa_service_worker_source('0.0.1') != pwa_service_worker_source('0.0.2')
    assert b'version 0.0.1' in pwa_service_worker_source('0.0.1')


# ---------- manifest ----------

def test_manifest_describes_one_gateway_scoped_application(tmp_path):
    async def scenario():
        gateway = _gateway(tmp_path / 'registry.json')
        async with _client(gateway) as client:
            response = await client.get('/_mesh/pwa/manifest.webmanifest')
            assert response.status_code == 200
            assert response.headers['content-type'] == 'application/manifest+json'
            assert response.headers['cache-control'] == 'no-cache'
            return response.json()
    document = asyncio.run(scenario())

    assert document['start_url'] == '/' and document['scope'] == '/'
    assert document['display'] == 'standalone'
    assert document['name'] == 'OpenCode Mesh'
    # The installed label must not abbreviate to 'Mesh' on launchers that prefer short_name.
    assert document['short_name'] == 'OpenCode Mesh'
    # A per-launch handoff parameter would break last-route restore and single-app identity.
    assert '?' not in document['start_url'] and '#' not in document['start_url']
    sizes = {icon['sizes']: icon for icon in document['icons']}
    assert set(sizes) == {'192x192', '512x512'}
    for icon in sizes.values():
        assert icon['src'].startswith('/_mesh/pwa/')
        assert icon['type'] == 'image/png'
        assert set(icon['purpose'].split()) == {'any', 'maskable'}
    assert document['theme_color'] == document['background_color'] == '#fafafa'


def test_manifest_id_separates_the_gateway_app_from_the_native_app(tmp_path):
    """A browser keys an installed app by origin plus id, and the native app owns id '/'.

    Reusing '/' merges both installs on one origin: Chrome then shows one app
    whose launcher name and icon follow whichever install wrote last.
    """
    async def scenario():
        gateway = _gateway(tmp_path / 'registry.json')
        async with _client(gateway) as client:
            first = (await client.get('/_mesh/pwa/manifest.webmanifest')).json()
            second = (await client.get('/_mesh/pwa/manifest.webmanifest')).json()
            return first, second
    first, second = asyncio.run(scenario())

    assert first['id'] == '/_mesh/pwa', 'the Gateway app needs its own identity'
    assert first['id'] != '/', 'the native app on this origin already owns id /'
    # Stable across requests: a changing id reads as a new application.
    assert first['id'] == second['id']
    # The identity must not move with the manifest location or a device.
    assert PWA_MANIFEST_PATH not in first['id'] and '/device/' not in first['id']
    # The id is an identity, not an entry point: launching still goes to '/'.
    assert first['start_url'] == '/' and first['scope'] == '/'


# ---------- icons ----------

def test_icons_are_served_from_gateway_bytes_with_recorded_hashes(tmp_path):
    async def scenario():
        gateway = _gateway(tmp_path / 'registry.json')
        async with _client(gateway) as client:
            for size in PWA_ICON_SIZES:
                headers = (await client.get(f'/_mesh/pwa/icon-{size}.png')).headers
                assert headers['content-type'] == 'image/png'
                assert headers['cache-control'] == 'no-cache'
            return await _fetch_icons(client)
    icons = asyncio.run(scenario())

    for size, body in icons.items():
        assert _png_size(body) == (size, size), 'declared icon size must match the PNG geometry'
        assert hashlib.sha256(body).hexdigest() == ICON_SHA256[size]
    assert (PWA_ICON_DIR / 'README.md').exists(), 'icon provenance must be recorded'
    assert (PWA_ICON_DIR / 'LICENSE-OpenCode.txt').read_text().startswith('MIT License')
    # The upstream MIT notice is a redistribution condition, not a decoration.
    assert 'Copyright (c) 2025 opencode' in (PWA_ICON_DIR / 'LICENSE-OpenCode.txt').read_text()


def test_icons_preserve_upstream_transparency(tmp_path):
    """Filling the upstream corners changes the visible launcher artwork."""
    async def scenario():
        gateway = _gateway(tmp_path / 'registry.json')
        async with _client(gateway) as client:
            return await _fetch_icons(client)
    icons = asyncio.run(scenario())

    for size, body in icons.items():
        width, height, pixels, channels = _decode_png(body)
        assert channels == 4, 'preserve the upstream alpha channel'
        assert (width, height) == (size, size)
        for x, y in ((0, 0), (size - 1, 0), (0, size - 1), (size - 1, size - 1)):
            assert pixels[(y * width + x) * channels + 3] == 0
        assert pixels[((size // 2) * width + size // 2) * channels + 3] == 255


def test_missing_icon_files_fail_at_startup(tmp_path, monkeypatch):
    """Package assets are required; the Gateway refuses to start without them.

    The assertion goes through Gateway construction because that is the
    contract: a missing icon must not degrade into a 500 at request time.
    """
    import src.frontend as frontend

    monkeypatch.setattr(frontend, 'PWA_ICON_DIR', tmp_path / 'missing')
    with pytest.raises(FileNotFoundError):
        _gateway(tmp_path / 'registry.json')


def test_startup_reads_the_packaged_icons_into_the_gateway(tmp_path, monkeypatch):
    """Constructing the Gateway is what loads the bytes later served by the PWA routes."""
    icons = {size: b'\x89PNG\r\n\x1a\n' + bytes([size % 256]) * 4 for size in PWA_ICON_SIZES}
    monkeypatch.setattr('src.main.load_pwa_icons', lambda: icons)

    gateway = _gateway(tmp_path / 'registry.json')

    assert gateway.pwa_icons == icons


# ---------- HTML links ----------

def test_device_html_manifest_link_uses_the_gateway_with_credentials():
    body = f'<html><head><meta charset="utf-8">{UPSTREAM_LINK_TAGS}<title>OpenCode</title></head><body></body></html>'

    result = rewrite_device_html(body.encode(), 'device-a').decode()

    assert 'href="/_mesh/pwa/manifest.webmanifest"' in result
    assert 'crossorigin="use-credentials"' in result
    # The manifest stays credentialed because the Gateway protects it with Basic Auth.
    assert '/_mesh/device/device-a/_mesh' not in result
    assert '/site.webmanifest' not in result
    assert '/icons/prod/' not in result
    assert result.count('rel="manifest"') == 1
    assert result.count('rel="icon"') == 1 and result.count('rel="apple-touch-icon"') == 1


def test_html_without_pwa_links_gains_them_without_failing():
    body = b'<html><head><title>OpenCode</title></head><body><div id="root"></div></body></html>'

    result = rewrite_device_html(body, 'device-a')

    assert b'rel="manifest"' in result
    assert result.index(b'rel="manifest"') < result.index(b'</head>')
    assert b'crossorigin="use-credentials"' in result


def test_pwa_link_normalization_is_idempotent_and_deduplicates():
    body = ('<html><head><link rel="manifest" href="/site.webmanifest">'
            "<link rel='manifest' href='/site.webmanifest?x=1'></head><body></body></html>").encode()

    once = normalize_pwa_links(body)
    twice = normalize_pwa_links(once)

    assert once == twice, 'repeated rewrites must converge'
    assert once.count(b'rel="manifest"') == 1
    assert b'/site.webmanifest' not in once


def test_fragments_without_head_are_extended_not_rejected():
    assert normalize_pwa_links(b'<div id="root"></div>').count(b'rel="manifest"') == 1


def test_only_a_real_rel_attribute_makes_a_link_a_pwa_link():
    """Attribute name matching must not fire on data-rel, x-rel or aria-rel.

    Deleting such a tag would remove a stylesheet the page still needs.
    """
    body = ('<html><head>'
            '<link data-rel="manifest" rel="stylesheet" href="/app.css" />'
            '<link x-rel="icon" rel="stylesheet" href="/print.css" />'
            '<link aria-rel="apple-touch-icon" rel="preload" href="/font.woff2" as="font" />'
            '</head><body></body></html>').encode()

    result = normalize_pwa_links(body).decode()

    for tag in ('<link data-rel="manifest" rel="stylesheet" href="/app.css" />',
                '<link x-rel="icon" rel="stylesheet" href="/print.css" />',
                '<link aria-rel="apple-touch-icon" rel="preload" href="/font.woff2" as="font" />'):
        assert tag in result, f'{tag} is not a PWA link and must survive byte for byte'
    assert PWA_LINK_BLOCK in result, 'the canonical block is still added'


def test_pwa_link_with_quoted_angle_bracket_is_removed_whole():
    """A '>' inside a quoted value ends no tag; removing part of it would leave broken markup."""
    body = (b'<html><head><link rel="manifest" title="mesh > device" '
            b'href="/site.webmanifest"><title>t</title></head><body></body></html>')

    result = normalize_pwa_links(body)

    assert b'/site.webmanifest' not in result
    assert b'mesh > device' not in result, 'the whole tag is removed, not the part before the >'
    assert b'device"' not in result, 'no residue of the removed tag may survive'
    assert result.count(b'<link') == 3, 'only the canonical block keeps links'
    assert b'<title>t</title>' in result, 'the markup after the removed tag is untouched'
    assert result.endswith(b'</head><body></body></html>')


def test_pwa_link_text_inside_script_style_and_comment_is_untouched():
    """Those are text, not markup: rewriting them would corrupt the page."""
    script = b"var icon = '<link rel=\"icon\" href=\"/upstream.ico\">';"
    style = b'/* <link rel="manifest" href="/site.webmanifest"> */'
    comment = b'<!-- <link rel="apple-touch-icon" href="/touched.png"> -->'
    body = b'<html><head><script>' + script + b'</script><style>' + style + b'</style>' + comment + b'</head><body></body></html>'

    result = normalize_pwa_links(body)

    assert script in result
    assert style in result
    assert comment in result
    assert PWA_LINK_BLOCK.encode() in result, 'the canonical block is still added'


def test_rel_token_lists_and_attribute_case_are_honoured():
    body = (b'<HTML><HEAD>'
            b'<LINK REL="ALTERNATE MANIFEST" HREF="/site.webmanifest">'
            b'<link rel="ICON" href="/icons/prod/favicon.ico">'
            b'<link rel="shortcut icon" href="/legacy.ico" />'
            b'</HEAD><BODY></BODY></HTML>')

    result = normalize_pwa_links(body)

    for href in (b'/site.webmanifest', b'/icons/prod/favicon.ico', b'/legacy.ico'):
        assert href not in result
    assert result.count(b'rel="manifest"') == 1
    assert result.count(b'rel="icon"') == 1
    assert result.index(b'rel="manifest"') < result.index(b'</HEAD>'), 'uppercase head end is a real boundary'


def test_truncated_markup_still_gains_the_gateway_links():
    """Best effort by contract: an odd document must render, not fail the request."""
    for body in (b'<html><head><link rel="manifest" href="/site.webmanifest"',
                 b'<html><head',
                 b'<link rel="icon" href="/a.ico"',
                 b'<<link rel="icon" href="/a.ico">'):
        assert PWA_LINK_BLOCK.encode() in normalize_pwa_links(body), body


def test_full_rewrite_pipeline_is_idempotent_across_passes():
    body = ('<HTML><HEAD><META CHARSET="utf-8">' + UPSTREAM_LINK_TAGS +
            '<title>OpenCode</title></HEAD><BODY><div id="root"></div></BODY></HTML>').encode()

    once = rewrite_device_html(body, 'device-a')
    twice = rewrite_device_html(once, 'device-a')

    assert once == twice, 'repeated device rewrites must converge byte for byte'
    assert once.count(b'rel="manifest"') == 1
    assert b'/icons/prod/' not in once


def test_proxy_html_and_pwa_routes_both_answer_from_the_gateway(tmp_path):
    """End to end: device HTML is rewritten to the Gateway manifest and PWA paths stay local."""
    async def scenario():
        gateway = _gateway(tmp_path / 'registry.json')
        gateway.registry.devices['device-a'] = {'device_id': 'device-a', 'name': 'Device A',
                                                 'last_seen': time.time(), 'ws': object()}
        html = ('<html><head>' + UPSTREAM_LINK_TAGS + '<script src="/_assets/index.js"></script>'
                '</head><body></body></html>').encode()

        async def send_control(ws, item, **kwargs):
            forwarded.append(item['path'])
            gateway.pending[item['id']].set_result(
                {'status': 200, 'headers': {'content-type': 'text/html'},
                 'body': base64.b64encode(html).decode()})

        forwarded: list[str] = []
        gateway.send_control = send_control
        async with _client(gateway) as client:
            page = await client.get('/', headers={'accept': 'text/html'})
            assert page.status_code == 200
            assert page.headers['cache-control'] == 'no-store'
            assert 'href="/_mesh/pwa/manifest.webmanifest"' in page.text
            assert asset_prefix('device-a') + '/_assets/index.js' in page.text
            # Only the proxied page may reach the device.
            for path in ('/sw.js', '/_mesh/pwa/manifest.webmanifest', '/_mesh/pwa/icon-192.png',
                         '/_mesh/pwa/icon-512.png'):
                response = await client.get(path)
                assert response.status_code == 200, path
            assert (await client.post('/sw.js')).status_code == 405
            assert (await client.post('/_mesh/pwa/manifest.webmanifest')).status_code == 405
            # The browser's update check and manifest fetch both use HEAD-compatible GETs.
            for path in ('/sw.js', '/_mesh/pwa/manifest.webmanifest', '/_mesh/pwa/icon-192.png'):
                head = await client.head(path)
                assert head.status_code == 200, path
                assert head.headers['cache-control'] == 'no-cache'
                assert head.content == b''
            assert forwarded == ['/'], 'only the proxied page is a device request'
    asyncio.run(scenario())


# ---------- authentication ----------

def test_manifest_worker_and_pages_stay_behind_basic_auth(tmp_path):
    """Installability must not become a way around the Gateway credential.

    Only the two icon paths are anonymous, so the manifest, the worker, the
    proxied page and the device API keep answering 401 with a challenge.
    """
    async def scenario():
        gateway = _gateway(tmp_path / 'registry.json')
        gateway.registry.devices['device-a'] = {'device_id': 'device-a', 'name': 'Device A',
                                                 'last_seen': time.time(), 'ws': object()}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app),
                                     base_url='http://test') as client:
            for path in ('/sw.js', PWA_MANIFEST_PATH, '/', '/_mesh/devices'):
                denied = await client.get(path, headers={'accept': 'text/html'})
                assert denied.status_code == 401, path
                assert 'Basic' in denied.headers['www-authenticate'], path
    asyncio.run(scenario())


def test_icons_are_readable_without_credentials_and_nothing_else(tmp_path):
    """Chrome's WebAPK icon hasher omits credentials (CredentialsMode::kOmit).

    A 401 there costs the launcher icon, so an unauthenticated read of the two
    exact icon paths is answered. A write is still a credential question first:
    without one it is 401, and with one it is 405 because icons are read only.
    """
    async def scenario():
        gateway = _gateway(tmp_path / 'registry.json')
        async with _client(gateway) as client, _anonymous(gateway) as guest:
            for size in PWA_ICON_SIZES:
                path = f'/_mesh/pwa/icon-{size}.png'
                for method in ('GET', 'HEAD'):
                    read = await guest.request(method, path)
                    assert read.status_code == 200, (method, path)
                    assert read.headers['content-type'] == 'image/png'
                # The anonymous read must be the same committed bytes, not a
                # second or generated variant.
                assert (await guest.get(path)).content == (await client.get(path)).content, path
                # A query is a cache-buster, not a different resource.
                assert (await guest.get(path + '?v=2')).status_code == 200
                assert (await guest.post(path)).status_code == 401, path
                assert (await client.post(path)).status_code == 405, path
                assert (await client.put(path)).status_code == 405, path
            # The manifest and the worker are not part of that allowance.
            for path in ('/sw.js', PWA_MANIFEST_PATH):
                assert (await guest.get(path)).status_code == 401, path
                assert (await guest.head(path)).status_code == 401, path
    asyncio.run(scenario())


def test_icon_allowlist_matches_no_variant_path(tmp_path):
    """The allowance is two exact paths, not a prefix and not a pattern.

    Anything that merely looks like an icon path must still challenge, or the
    anonymous surface would grow with every new asset under /_mesh/pwa/.
    """
    variants = ('/_mesh/pwa/icon-192.png/',   # trailing slash
                '/_mesh/pwa/icon-64.png',     # size that is not served
                '/_mesh/pwa/ICON-192.PNG',    # case variant
                '/_mesh/pwa/sub/icon-192.png',  # nested path
                '/_mesh/pwa/icon-192.png.bak',  # suffixed file
                '/_mesh/pwa/icon-192.png%2F')  # encoded slash
    async def scenario():
        gateway = _gateway(tmp_path / 'registry.json')
        async with _anonymous(gateway) as guest:
            for path in variants:
                denied = await guest.get(path)
                assert denied.status_code == 401, path
                assert 'Basic' in denied.headers['www-authenticate'], path
    asyncio.run(scenario())


# ---------- recovery page ----------

def test_recovery_page_carries_manifest_link_and_registration():
    assert 'href="/_mesh/pwa/manifest.webmanifest"' in OFFLINE_PAGE
    assert 'crossorigin="use-credentials"' in OFFLINE_PAGE
    assert "serviceWorker.register('/sw.js'" in OFFLINE_PAGE
    assert OFFLINE_PAGE.index('rel="manifest"') < OFFLINE_PAGE.index('</head>')
    # The page script the Node tests extract must stay the first plain <script> block.
    assert OFFLINE_PAGE.index('<script>') < OFFLINE_PAGE.index("serviceWorker.register")
    assert re.search(r"id=\"ocm-pwa-registration\"", OFFLINE_PAGE)


# ---------- installed window layout ----------

BAR_HEIGHT_RE = re.compile(r'const BAR_HEIGHT_PX = (\d+);')


def visible_height_region() -> str:
    """The height function together with every registration that drives it.

    The registrations are the behaviour, not incidental setup: without the
    visualViewport hook the box stops tracking the keyboard, which is the exact
    defect the function exists to fix. So the tested region has to reach past the
    last listener, and a missing one has to fail as an assertion rather than as
    an opaque str.index error.
    """
    start = TRANSPORT_ADAPTER.index('  function applyVisibleViewportHeight()')
    last = "visualViewport.addEventListener('resize', applyVisibleViewportHeight);"
    assert last in TRANSPORT_ADAPTER[start:], 'the visualViewport resize hook is gone'
    end = TRANSPORT_ADAPTER.index('\n', TRANSPORT_ADAPTER.index(last, start)) + 1
    return TRANSPORT_ADAPTER[start:end]


def bar_height() -> int:
    match = BAR_HEIGHT_RE.search(TRANSPORT_ADAPTER)
    assert match, 'the bar height must be declared once'
    return int(match.group(1))


def test_bar_height_is_declared_once_and_referenced_everywhere():
    """Three places depend on the bar height, so only one may state it.

    The bar itself, the app box fallback and the script that measures the
    visible area all subtract or reserve it. A second literal would let the bar
    change and leave one of them stale, which is the same class of drift that put
    the composer below the fold in the first place.

    Each consumer is pinned at its own site rather than by scanning the adapter
    for the number: a substring sweep would fire on any unrelated pixel value
    that happens to end in the same digits.
    """
    declarations = BAR_HEIGHT_RE.findall(TRANSPORT_ADAPTER)
    assert len(declarations) == 1, f'BAR_HEIGHT_PX must be declared once, found {len(declarations)}'
    assert 'height:${BAR_HEIGHT_PX}px' in TRANSPORT_ADAPTER, 'the bar must size itself from the constant'
    assert 'calc(100dvh - ${BAR_HEIGHT_PX}px)' in TRANSPORT_ADAPTER, 'the app box fallback must read it too'
    assert '- BAR_HEIGHT_PX;' in visible_height_region(), 'the script must subtract the constant, not its own literal'


def test_app_root_height_reserves_the_mesh_bar():
    """The stylesheet fallback exists, and it reserves Mesh's own bar.

    The app declares its own h-dvh, so Mesh overrides it and states the box
    explicitly, otherwise the bar pushes the app one bar-height past the fold
    and body{overflow:hidden} makes that unreachable rather than scrollable.
    This only has to hold for the window before applyVisibleViewportHeight()
    runs, which is why it is pinned as a shape rather than as device behaviour.
    """
    match = re.search(r'#root\{((?:[^{}]|\{[^{}]*\})*)\}', TRANSPORT_ADAPTER)
    assert match, 'the adapter must state the app root height itself'
    assert re.fullmatch(
        r'height:calc\(100dvh - \$\{BAR_HEIGHT_PX\}px\)', match.group(1)), match.group(1)


def test_app_box_height_wiring_registers_every_trigger():
    """Each trigger that can change the visible height must be wired.

    The four registrations are what make the box follow a keyboard, a rotation
    and a late-injected script. Nothing else in the suite observes them, so a
    deletion here would only ever show up on a device.
    """
    for registration in (
        "document.addEventListener('DOMContentLoaded', applyVisibleViewportHeight",
        "window.addEventListener('resize', applyVisibleViewportHeight)",
        "window.addEventListener('orientationchange', applyVisibleViewportHeight)",
        "window.visualViewport.addEventListener('resize', applyVisibleViewportHeight)",
    ):
        found = TRANSPORT_ADAPTER.count(registration)
        assert found == 1, f'expected exactly one {registration}, found {found}'
    # The adapter can also be injected after parsing, so it must measure at once
    # rather than only waiting for an event that has already gone by.
    assert 'else applyVisibleViewportHeight();' in visible_height_region(), \
        'a document that is already parsed must be measured without waiting for DOMContentLoaded'


def test_app_box_height_follows_the_visible_viewport_not_dvh():
    """The app box must be sized from what is visible, not from a viewport unit.

    100dvh measures the layout viewport, and a real device showed it disagreeing
    with the visible area: the app box ran a full bar-height past the fold with
    the IME open. So a dvh-derived height cannot be repaired by a constant
    correction. visualViewport.height is the measure that already matches what
    the user can see, and it has to account for the bar as well.

    The wiring is driven for real here: the adapter text runs against stub DOM
    objects that record listeners, and each trigger is dispatched. Calling the
    function by hand would pass no matter which listeners were registered.
    """
    bar = bar_height()
    prel = '''
const assert=require('node:assert/strict');
const vm=require('node:vm');
const BAR_HEIGHT_PX=__BAR__;

// Evaluate the adapter text with a stub DOM that records every registration,
// because the point of the test is which events reach the function.
function boot(readyState, vvHeight){
  const docL={}, winL={}, vpL={};
  const add=(map,name,fn)=>{(map[name]||(map[name]=[])).push(fn);};
  const root={style:{}};
  const document={
    readyState,
    getElementById:id=>(id==='root'?root:id==='ocm-mesh-bar'?{}:null),
    addEventListener:(name,fn)=>add(docL,name,fn)};
  const window={
    innerHeight:900,
    visualViewport:{height:vvHeight,addEventListener:(name,fn)=>add(vpL,name,fn)},
    addEventListener:(name,fn)=>add(winL,name,fn)};
  vm.runInContext(__REGION__, vm.createContext({document, window, BAR_HEIGHT_PX}));
  return {root, docL, winL, vpL, window, document};
}

const loading=boot('loading',640);
assert.equal(loading.root.style.height,undefined,'nothing is measured before the DOM is ready');
loading.docL.DOMContentLoaded[0]();
assert.equal(loading.root.style.height,'__A__px','the DOMContentLoaded hook applies the height');

const ready=boot('complete',640);
assert.equal(ready.root.style.height,'__A__px','an already-parsed document is measured at once');
assert.equal(ready.docL.DOMContentLoaded,undefined,'no listener is armed once the event has fired');

ready.window.visualViewport.height=500;
ready.vpL.resize[0]();
assert.equal(ready.root.style.height,'__B__px','an IME shrinking the visible viewport shrinks the box');

ready.window.visualViewport.height=700;
ready.winL.resize[0]();
assert.equal(ready.root.style.height,'__C__px','a window resize re-measures');
ready.winL.orientationchange[0]();
assert.equal(ready.root.style.height,'__C__px','a rotation re-measures as well');

const noVisual=boot('complete',640);
delete noVisual.window.visualViewport;
noVisual.winL.resize[0]();
assert.equal(noVisual.root.style.height,'__D__px','innerHeight stands in where visualViewport is absent');

const bare=boot('complete',640);
bare.document.getElementById=()=>null;
bare.root.style.height='123px';
bare.winL.resize[0]();
assert.equal(bare.root.style.height,'123px','a not-yet-mounted app root is left alone');
'''
    script = (prel
              .replace('__BAR__', str(bar))
              .replace('__REGION__', json.dumps(visible_height_region()))
              .replace('__A__', str(640 - bar))
              .replace('__B__', str(500 - bar))
              .replace('__C__', str(700 - bar))
              .replace('__D__', str(900 - bar)))
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr




# ---------- reload control ----------


def reload_control_region() -> str:
    """The reload control: its handler and the bar construction that binds it.

    Sliced the same way as the layout wiring, so a missing binding shows up as
    an assertion instead of an opaque index error.
    """
    start = TRANSPORT_ADAPTER.index('  function ensureBarStyle()')
    last = '    return bar;\n  }'
    return TRANSPORT_ADAPTER[start:TRANSPORT_ADAPTER.index(last, start) + len(last)]


RELOAD_HARNESS = r'''
const assert=require('node:assert/strict');
const vm=require('node:vm');

// Only what ensureBar() touches: it builds the bar and binds handlers, and does
// no network work, so a minimal element stub is enough.
class Element {
  constructor(tag){
    this.tagName=tag.toUpperCase();this.children=[];this.className='';this.id='';
    this.textContent='';this.style={};this.attributes={};this.listeners={};this.parentNode=null;
  }
  append(...nodes){for(const node of nodes){node.parentNode=this;this.children.push(node);}}
  appendChild(node){this.append(node);return node;}
  insertBefore(node,before){node.parentNode=this;this.children.splice(this.children.indexOf(before),0,node);}
  setAttribute(key,value){this.attributes[key]=String(value);}
  getAttribute(key){return this.attributes[key]??null;}
  // Reflected IDL attribute: the real element maps it onto the content
  // attribute, so reading it back after a property assignment has to work.
  get type(){return this.getAttribute('type')??'';}
  set type(value){this.setAttribute('type',value);}
  addEventListener(type,listener){(this.listeners[type]??=[]).push(listener);}
  dispatch(type,event={}){for(const listener of this.listeners[type]??[])listener({target:this,preventDefault(){},...event});}
  descendants(){return this.children.flatMap(node=>[node,...node.descendants()]);}
  querySelector(selector){return this.descendants().find(node=>node.className.split(' ').includes(selector.slice(1)))||null;}
}

const body=new Element('body');
const head=new Element('head');
global.document={
  body,head,readyState:'complete',
  createElement:tag=>new Element(tag),
  getElementById:id=>[head,...head.children,...body.descendants()].find(node=>node.id===id)||null,
};
global.window=global;
global.location={reloads:0,reload(){this.reloads++;}};
global.MESH_VERSION='test';
global.BAR_CSS='';
global.BAR_HEIGHT_PX=36;
// The device menu has its own tests; here it only has to exist, because the
// bar binds it and this is about making sure the title does not shadow it.
function toggleDeviceMenu(){}

vm.runInThisContext(__REGION__);

const bar=ensureBar();
const title=bar.querySelector('.ocm-title');
assert.ok(title,'the bar must have a title control');
assert.equal(title.tagName,'BUTTON','the title has to be a button to be clickable and focusable');
assert.equal(title.getAttribute('type'),'button','and must not submit anything');
assert.ok(title.getAttribute('title'),'the control needs a hover hint');
assert.ok(title.getAttribute('aria-label'),'and a name for screen readers');
assert.equal(title.textContent,'OpenCode Mesh','the visible label stays the product name');

// Two buttons share this bar; one reloads and one opens a menu. A mis-bound
// handler here would reload the page every time a device was picked.
const device=bar.querySelector('.ocm-device-menu-button');
device.dispatch('click');
assert.equal(location.reloads,0,'the device button must not reload the page');

title.dispatch('click');
assert.equal(location.reloads,1,'clicking the title reloads');
title.dispatch('click');
assert.equal(location.reloads,2,'and keeps working for the next reload');
'''


def test_bar_title_reloads_the_page():
    """An installed WebAPK has no reload button, so the bar title has to be one.

    Native pull-to-refresh is not available on this page: the root scroller has
    no overscroll range, the body sets overscroll-behavior-y:none, and Chrome
    only offers the gesture on the root scroller. Making the root scrollable to
    unlock it would fight the fixed-height layout, so the title carries the
    reload. It has to be a real button, otherwise it is not focusable and a
    keyboard cannot reach the only reload the installed app has.
    """
    script = RELOAD_HARNESS.replace('__REGION__', json.dumps(reload_control_region()))
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_bar_title_keeps_the_plain_text_look():
    """The title became a button, so the browser's button chrome has to go.

    font:inherit has to come before font-weight:600, because the shorthand
    resets the weight and a rule that follows it would win -- the title would
    silently stop being bold.

    white-space:nowrap is not cosmetic. The title is the widest fixed label in a
    bar that also carries a variable-width transport string, so once the title
    is allowed to wrap it becomes the flex item that gives way, and a two-line
    title overflows the bar's 36px. Letting the transport absorb the squeeze
    instead keeps the control on one line.
    """
    rule = re.search(
        r'#ocm-mesh-bar \.ocm-title\{((?:[^{}]|\{[^{}]*\})*)\}', TRANSPORT_ADAPTER)
    assert rule, 'the title rule must exist'
    declarations = rule.group(1)
    for reset in ('font:inherit', 'border:0', 'margin:0', 'padding:0',
                  'background:transparent', 'cursor:pointer', 'white-space:nowrap'):
        assert reset in declarations, f'the title needs {reset}'
    assert declarations.index('font:inherit') < declarations.index('font-weight:600'), \
        'font:inherit resets the weight, so it has to precede font-weight:600'
    assert '#ocm-mesh-bar .ocm-title:focus-visible' in TRANSPORT_ADAPTER, \
        'the only reachable control needs a focus ring'
