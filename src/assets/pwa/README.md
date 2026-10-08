# Gateway PWA icons

The launcher icons served at `/_mesh/pwa/icon-192.png` and
`/_mesh/pwa/icon-512.png`. They are Gateway-owned so that installability never
depends on an online Agent.

## Provenance

- Source: `packages/ui/public/icons/prod/web-app-manifest-192x192.png` and
  `web-app-manifest-512x512.png` from `anomalyco/opencode`, the artwork the
  upstream manifest references as its installable icons.
- The upstream files have **transparent rounded corners**. The committed copies
  are edge-filled so every pixel is opaque: the manifest declares them
  `purpose="any maskable"`, and a maskable icon must fill its canvas or Android
  masks the transparent area to black. Only that fill separates the committed
  bytes from upstream; the artwork itself is unchanged.
- License: the repository root `LICENSE` is **MIT License, Copyright (c) 2025
  opencode**. The notice is redistributed verbatim as `LICENSE-OpenCode.txt`.

## Measured results

Both icons are 8-bit non-interlaced RGB PNG (`color type 2`, no alpha channel),
opaque and full-bleed, with the OpenCode mark centred.

SHA-256 of the committed outputs:

- `icon-192.png`: `6dee7f9abbcf1dec1393dccb98ccac8b33dc249c05b9dc969f7e09c0b3e2b843`
- `icon-512.png`: `4e887d25a349305165a108ec63c3faed4c67567fc54af9ea2b56a7364f2e20b9`

`tests/test_v2_pwa.py` pins these hashes and the PNG geometry (declared size,
opaque RGB, non-blank), so an accidental or unreviewed replacement fails the
suite.

The icons are declared `purpose="any maskable"`. An earlier revision generated
its own flat icons from `favicon-v3.svg`; that source vector was removed once the
upstream project switched its installable icons to this `prod` artwork.
