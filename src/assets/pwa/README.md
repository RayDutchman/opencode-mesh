# Gateway PWA icons

The launcher icons served at `/_mesh/pwa/icon-192.png` and
`/_mesh/pwa/icon-512.png`. They are Gateway-owned so that installability never
depends on an online Agent.

## Provenance

- Source: `anomalyco/opencode` v2.0.18:
  - 192: `packages/desktop/icons/prod/android/mipmap-xxxhdpi/ic_launcher.png`
  - 512: `packages/desktop/icons/prod/icon.png`
- `packages/app/vite.icons.ts` publishes these files as
  `/icons/prod/web-app-manifest-192x192.png` and
  `/icons/prod/web-app-manifest-512x512.png` for `/site.webmanifest`.
- The committed copies preserve the upstream bytes, including **transparent
  rounded corners**. Do not fill, resize, or re-encode them: doing so changes
  the original artwork. Their hashes were also checked against the icons
  served by the running upstream application on 2026-10-08.
- License: the repository root `LICENSE` is **MIT License, Copyright (c) 2025
  opencode**. The notice is redistributed verbatim as `LICENSE-OpenCode.txt`.

## Measured results

Both icons are 8-bit non-interlaced RGBA PNG (`color type 6`), with transparent
corners and an opaque centre.

SHA-256 of the committed outputs:

- `icon-192.png`: `a2aedd1def885e3b7d7adc7668725c3772996f1699c4524252f752f55707101b`
- `icon-512.png`: `324bd6ab9499f006519209eaa883519f37b9373a59d6eb01235189d4ac67ea27`

`tests/test_v2_pwa.py` pins these hashes and the PNG geometry (declared size,
transparent corners and opaque centre), so an accidental or unreviewed replacement fails the
suite.

Mesh retains `purpose="any maskable"`; upstream declares `maskable` only.
Byte identity does not guarantee identical launcher masking on every platform.
An earlier Mesh revision generated flat icons from `favicon-v3.svg`; another
filled the upstream corners. Neither transformation is used now.
