# Gateway PWA icons

The launcher icons served at `/_mesh/pwa/icon-192.png` and
`/_mesh/pwa/icon-512.png`. They are Gateway-owned so that installability never
depends on an online Agent.

## Provenance

- Source vector: `packages/ui/src/assets/favicon/favicon-v3.svg` from
  `anomalyco/opencode` tag **v2.0.18**
  (the tag this project pins for the upstream V2 contract).
  - SHA-256: `e29bbe33380ad1c1ada9134b52f229d30e9776d60481512c9d81f2bb6f37def9`
  - Kept next to the outputs as `favicon-v3-2.0.18.svg` so the rasterization
    can be reproduced and audited.
- License: the repository root `LICENSE` at v2.0.18 is **MIT License,
  Copyright (c) 2025 opencode**; `packages/ui/src/assets/favicon/` carries no
  separate license file, so the root notice governs these assets. The notice is
  redistributed verbatim as `LICENSE-OpenCode.txt`
  (SHA-256 `625f0f619133f89bbbb2abe37369613dfa1885eba1e50d02170deb62cb6b`).

## Generation

`favicon-v3-2.0.18.svg` draws a full-bleed `#131010` background. A maskable
icon must keep its content inside the central circle of diameter 80%, so the
logo group is scaled to 0.75 and centered on the 512 canvas:

```xml
<g transform="translate(64 64) scale(0.75)"> ...paths... </g>
```

Rasterized with `rsvg-convert` 2.61.3 (`librsvg`), from the intermediate
`pwa-icon.svg` wrapper:

```bash
rsvg-convert -w 192 -h 192 pwa-icon.svg -o icon-192.png
rsvg-convert -w 512 -h 512 pwa-icon.svg -o icon-512.png
```

## Measured results

| File | Size | Content bbox | Max corner radius | Maskable safe radius (40%) |
|---|---|---|---|---|
| `icon-192.png` | 192x192 | x 60..131, y 51..140 | 56.9 px | 76.8 px — inside |
| `icon-512.png` | 512x512 | x 160..351, y 136..375 | 153.0 px | 204.8 px — inside |

SHA-256 of the committed outputs:

- `icon-192.png`: `fb83ff4391107a9cdf4534e7ea5a7de9941d03e44d45e472b7324dfeab538073`
- `icon-512.png`: `d8ee214f92544ec477bef8928e15b77a0de0f1ae5210f88f249562f285f80e9a`

`tests/test_v2_pwa.py` pins these hashes, the PNG geometry and the maskable
margin, so an accidental or unreviewed replacement fails the suite.

Regenerating with a different renderer will change the byte hashes. That is
expected: review the new icon, update this file and the test constants
together.
