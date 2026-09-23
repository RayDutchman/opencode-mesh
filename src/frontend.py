"""V2 bootstrap adapter: an isolated resource namespace separates the native cache; only the verified entry contract is replaced."""

import re
import base64
from urllib.parse import quote


ASSET_ROOT = '/_mesh/ui/1/'


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
    match = re.fullmatch(r'/_mesh/ui/1/([^/]+)(/_assets/.+)', path)
    if not match or '..' in match[2].split('/'):
        raise ValueError('Invalid frontend asset route')
    return match[1], match[2]


def adapt_entry(path: str, body: bytes) -> bytes:
    if not re.fullmatch(r'/_assets/index-[\w-]+\.js', path):
        return body
    # The getter must be unique in a real V2.0.6 bundle; if a newer build changes
    # the structure, fail explicitly instead of guessing a replacement.
    getter = rb'(function [\w$]+\(\)\{return )location\.origin(\})'
    if b'currentServerUrl' not in body or b'defaultServerUrl' not in body or len(re.findall(getter, body)) != 1:
        raise ValueError('Unsupported OpenCode bootstrap contract')
    patched = re.sub(getter, rb'\1window.__ocmBootstrap.serverUrl\2', body)
    return b'await window.__ocmBootstrap.ready;\n' + patched
