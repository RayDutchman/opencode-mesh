# Gateway PWA icons

The launcher icons served at `/_mesh/pwa/icon-192.png` and
`/_mesh/pwa/icon-512.png`. They are Gateway-owned so that installability never
depends on an online Agent.

## Provenance

- Source: `packages/ui/public/icons/prod/web-app-manifest-192x192.png` and
  `web-app-manifest-512x512.png` from `anomalyco/opencode`, the same artwork the
  upstream manifest references as its `maskable` icons.
- The files are committed verbatim (no re-rasterization); changing the upstream
  version means re-copying them and updating the recorded hashes below.
- License: the repository root `LICENSE` is **MIT License, Copyright (c) 2025
  opencode**. The notice is redistributed verbatim as `LICENSE-OpenCode.txt`.

## Measured results

Both icons are 8-bit non-interlaced PNG with an alpha channel (`color type 6`).
They draw a dark rounded square (transparent corners, opaque centre) with the
OpenCode mark centred.

SHA-256 of the committed outputs:

- `icon-192.png`: `a2aedd1def885e3b7d7adc7668725c3772996f1699c4524252f752f55707101b`
- `icon-512.png`: `324bd6ab9499f006519209eaa883519f37b9373a59d6eb01235189d4ac67ea27`

`tests/test_v2_pwa.py` pins these hashes and the PNG geometry (declared size,
transparent corners, opaque centre), so an accidental or unreviewed replacement
fails the suite.

The icons are declared `purpose="any maskable"`, matching the upstream manifest.
An earlier revision generated its own icons from `favicon-v3.svg` and asserted a
40% maskable safe zone; that generator and its source vector were removed once
the upstream project switched its installable icons to this `prod` artwork.
