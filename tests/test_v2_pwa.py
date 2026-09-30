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
import math
import re
import struct
import subprocess
import tempfile
import time
import zlib
from pathlib import Path

import httpx
import pytest

from src import __version__
from src.frontend import (PWA_ICON_DIR, PWA_ICON_SIZES, PWA_LINK_BLOCK, asset_prefix,
                          normalize_pwa_links, pwa_service_worker_source)
from src.main import Gateway, OFFLINE_PAGE, rewrite_device_html

# Provenance of the derived launcher icons, recorded in src/assets/pwa/README.md.
# Upstream source: packages/ui/src/assets/favicon/favicon-v3.svg @ anomalyco/opencode v2.0.18
# (MIT, Copyright (c) 2025 opencode), rasterized with rsvg-convert 2.61.3.
ICON_SHA256 = {
    192: 'fb83ff4391107a9cdf4534e7ea5a7de9941d03e44d45e472b7324dfeab538073',
    512: 'd8ee214f92544ec477bef8928e15b77a0de0f1ae5210f88f249562f285f80e9a',
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


async def _fetch_icons(client: httpx.AsyncClient) -> dict[int, bytes]:
    icons = {}
    for size in PWA_ICON_SIZES:
        response = await client.get(f'/_mesh/pwa/icon-{size}.png')
        assert response.status_code == 200, size
        icons[size] = response.content
    return icons


def _decode_png_rgb(body: bytes) -> tuple[int, int, bytearray]:
    """Decode the 8-bit non-interlaced RGB PNGs committed here, without an image dependency."""
    assert body[:8] == b'\x89PNG\r\n\x1a\n', 'icons must be real PNG bytes'
    width = height = 0
    compressed = bytearray()
    position = 8
    while position < len(body):
        length, kind = struct.unpack('>I4s', body[position:position + 8])
        chunk = body[position + 8:position + 8 + length]
        if kind == b'IHDR':
            width, height, depth, color, _, _, interlace = struct.unpack('>IIBBBBB', chunk)
            assert (depth, color, interlace) == (8, 2, 0), 'icons must be 8-bit non-interlaced RGB'
        elif kind == b'IDAT':
            compressed += chunk
        elif kind == b'IEND':
            break
        position += 12 + length
    raw = zlib.decompress(bytes(compressed))
    stride = width * 3
    pixels = bytearray(height * stride)
    previous = bytearray(stride)
    for row in range(height):
        start = row * (stride + 1)
        filter_type = raw[start]
        line = bytearray(raw[start + 1:start + 1 + stride])
        for index in range(stride):
            left = line[index - 3] if index >= 3 else 0
            up = previous[index]
            upper_left = previous[index - 3] if index >= 3 else 0
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
    return width, height, pixels


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

    assert document['id'] == '/' and document['start_url'] == '/' and document['scope'] == '/'
    assert document['display'] == 'standalone'
    # A per-launch handoff parameter would break last-route restore and single-app identity.
    assert '?' not in document['start_url'] and '#' not in document['start_url']
    sizes = {icon['sizes']: icon for icon in document['icons']}
    assert set(sizes) == {'192x192', '512x512'}
    for icon in sizes.values():
        assert icon['src'].startswith('/_mesh/pwa/')
        assert icon['type'] == 'image/png'
        assert set(icon['purpose'].split()) == {'any', 'maskable'}
    assert document['theme_color'] == document['background_color'] == '#fafafa'


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


def test_icons_keep_their_content_inside_the_maskable_safe_zone(tmp_path):
    """``purpose="any maskable"`` only holds while the logo survives an aggressive circular mask.

    A maskable icon is cropped to a circle of diameter 80%, and its background
    must be opaque: a transparent canvas is masked to black on Android.
    """
    async def scenario():
        gateway = _gateway(tmp_path / 'registry.json')
        async with _client(gateway) as client:
            return await _fetch_icons(client)
    icons = asyncio.run(scenario())

    for size, body in icons.items():
        width, height, pixels = _decode_png_rgb(body)
        assert (width, height) == (size, size)
        background = bytes(pixels[:3])
        assert background == b'\x13\x10\x10', 'the icon paints its own full-bleed background'
        xs, ys = [], []
        for y in range(height):
            for x in range(width):
                offset = (y * width + x) * 3
                if bytes(pixels[offset:offset + 3]) != background:
                    xs.append(x)
                    ys.append(y)
        assert xs, 'the icon must not be a blank canvas'
        center = (size - 1) / 2
        radius = max(math.hypot(x - center, y - center) for x, y in zip(xs, ys))
        assert radius <= 0.4 * size, f'content at {radius:.1f}px exceeds the {0.4 * size:.1f}px safe radius'
        assert abs(center - (min(xs) + max(xs)) / 2) <= 1 and abs(center - (min(ys) + max(ys)) / 2) <= 1


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

def test_pwa_surface_stays_behind_basic_auth(tmp_path):
    """Installability must not become a way around the Gateway credential."""
    async def scenario():
        gateway = _gateway(tmp_path / 'registry.json')
        gateway.registry.devices['device-a'] = {'device_id': 'device-a', 'name': 'Device A',
                                                 'last_seen': time.time(), 'ws': object()}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app),
                                     base_url='http://test') as client:
            for path in ('/sw.js', '/_mesh/pwa/manifest.webmanifest', '/_mesh/pwa/icon-192.png',
                         '/_mesh/pwa/icon-512.png'):
                denied = await client.get(path)
                assert denied.status_code == 401, path
                assert 'Basic' in denied.headers['www-authenticate']
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
