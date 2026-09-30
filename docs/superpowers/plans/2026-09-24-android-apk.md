# Android APK Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Deliver an installable Android APK containing the upstream OpenCode UI and Mesh transport integration.

**Architecture:** Keep upstream UI changes isolated at bootstrap. Bundle all UI assets. Keep business requests bound to explicit devices and preserve existing transport semantics.

**Tech Stack:** OpenCode v2.0.15 web frontend, Android WebView, existing Mesh JavaScript adapter; container integration selected after origin review.

**Spec:** docs/superpowers/specs/2026-09-24-android-apk-design.md

## Global Constraints

- No production deployment, no embedded credentials, no remote UI dependency.
- English code comments and script output; Chinese user documentation.
- No replay of mutations whose outcome is unknown.

## Review Focus

- Network/API failures must not return the bundled HTML fallback.
- Credentials must not cross the configured Gateway origin.
- Embedded assets must not silently fall back to a remote frontend version.
- Device switching must preserve request and PTY ownership.
- Activity recreation must preserve configuration without replaying actions.

## Tasks

- [x] Inspect pinned upstream frontend bootstrap, license and build inputs.
- [x] Prepare local build toolchain without changing production services.
- [x] Add isolated frontend packaging and source-level bootstrap integration.
- [x] Add Android container, configuration, authentication and file selection.
- [x] Add behavioral regression checks for URL/asset routing boundaries.
- [x] Build APK, inspect packaged resources and verify signature/checksum.
- [x] Review changes and document reproducible build and actual verification limits.
- [x] Deliver installable artifact and installation instructions.

## Execution status

General/explore background workers were attempted but their configured model is unavailable. Main implementer continues; a read-only reviewer was dispatched separately. No product implementation has been delegated successfully yet.

- Baseline: `bc7d86c`; branch `feat/android-apk`; 221 pytest tests passed with deprecation warnings treated as errors.
- Upstream pin verified with `git ls-remote`: v2.0.15 = `6f3639d82ed0760091792189b78f8eeb44f699b1`.
- Confirmed source bootstrap: `packages/app/src/entry.tsx` creates `createWebPlatform`; `src/runtime/platform/web.ts` production `getCurrentServerUrl()` returns `location.origin`. Adapt this source boundary before compilation and disable upstream service-worker registration in the packaged build.
- Installed tools: Bun 1.3.14, Node/npm, Java 11, adb. Android SDK and newer JDK require preparation. Downloads are isolated under `/tmp/opencode/android-toolchain`.
- Upstream shallow clone timed out; pinned codeload archive download started as fallback. No upstream source has been edited yet.
- Ruling: use a dedicated feature branch in the current checkout and avoid touching running Python modules; no separate worktree has been created. This keeps existing live service paths valid while new Android files remain isolated by directory.

### Implementation and verification checkpoint

- Ruling: use the Android framework WebView directly, with bundled resources intercepted at the user-selected HTTPS Gateway origin. Capacitor's separate local origin would require cross-origin transport changes; this choice keeps the existing Mesh adapter intact. No remote HTML/JS fallback is accepted.
- Ruling: use Java 11 with SDK 35 `aapt2`, `d8`, `zipalign`, and `apksigner` directly. This small framework-only application requires neither Gradle nor third-party Android runtime libraries. The newer JDK download was interrupted and is not required.
- SDK platform/build-tools archives were checked against the checksums in Google's repository manifest. Source came from the pinned upstream codeload archive. Bun's frozen-lockfile installation completed after the server restarted.
- The frontend build succeeded: 1,102 packaged resources, upstream license and a resource SHA-256 manifest. The first signed APK was approximately 10.07 MiB; v2/v3 signatures and package metadata verified. Rebuild after review fixes before delivery.
- Source-level integration waits for Mesh bootstrap and uses its explicit device Server URL. Service-worker generation/registration is disabled in the packaged build.
- Credential priming permits only the exact discovery GET with JavaScript disabled; stored credentials are offered once for that request and encrypted at rest using Android Keystore. Old WebView callbacks are guarded by instance identity. API requests remain native browser requests and are not replayed by Java.
- JVM URL-policy regression: initial missing implementation failed, then passed; the later discovery-only gate also failed before its implementation and passed afterward. Full suite reached 222 passing tests before additional source-pin tests.
- Desktop Chromium loaded the actual bundle against the existing Gateway: 72 locally fulfilled resources, zero missing resources, zero page errors, explicit device bootstrap, zero service workers. This validates the bundle, not Android WebView's HTTP-auth cache or native file picker.
- Android device installation has not been verified; `adb devices` did not complete within the probe timeout. No production deployment or service restart was performed.
- After the user repaired sub-agent availability, parallel documentation and source-pin hardening tasks were dispatched; read-only native-container review remains part of the final gate.
- Source-pin hardening completed with five original/patched entry-file SHA-256 pairs and optional archive verification. Dependency licenses and font notices are included. The main review caught an accidentally removed Bun build invocation; a stale-dist regression failed before restoring the invocation and passed afterward. Full suite: 239 passed with deprecation warnings treated as errors, using the project virtual environment.
- The worker's system-Python collection errors (missing uvicorn) were environment-specific; the repository virtual environment collected and passed the full suite. They were not ignored test failures.
- Final review: independent reviewer accepted preview delivery with Android runtime verification outstanding. A 15-second discovery watchdog was added; it is bound to the current WebView and removed on completion, replacement, or destruction. Native timeout behavior is compile-checked but not Android-runtime-tested.
- Final Ruling: deliver the installable preview without claiming Android feature acceptance; the user requested a package to install and no test handset is available. Relay PTY WebSocket authentication remains a real-device acceptance item; it is not evidence that HTTP/SSE chat necessarily fails.
- Final Ruling: keep Chromium's origin-scoped authentication cache for this preview. Same-origin credential replacement may require force-stopping the app; document it instead of adding a second transport/authentication implementation.
- Final minor (deferred): deprecated Android result/back APIs remain functional at the declared minimum SDK; no AndroidX dependency was introduced solely to replace them.
- Final minor (deferred): manually edited asset manifests are not supported; generated manifests have no empty entries. BufferedReader.readLine already removes line terminators.
- Final minor (deferred): no fixed HTTP auth realm requirement is added; priming is restricted to the exact configured-origin discovery request, and upstream proxy realms may differ.
- Delivery artifact: `opencode-mesh-0.1.0-preview.apk`, approximately 10.08 MiB; SHA-256 `6b7336cc179deef97773a994e69e18e073584ae692d4217d9ec033bca54b34d5`. V2/V3 signing verification passed; all 1,102 UI resource hashes and four license/notice files were checked inside the final APK. Persistent delivery copies and checksum files were verified byte-for-byte. Final test run: 239 passed; no production deployment, commit, or push was performed.

## Approved UI follow-up (2026-09-25)

- [x] Replace the native toolbar with an APK-only themed menu in the existing Mesh bar; preserve recovery controls before page startup.
- [x] Synchronize page colors with native settings and system-bar backgrounds, preserving keyboard and gesture insets.
- [x] Add the pinned OpenCode Web logo as launcher/adaptive icons and increment the preview version.
- [x] Independently review integration, run behavioral checks, rebuild and verify the signed APK using the existing preview key.
- [x] Update installation notes and deliver a new versioned file without overwriting the first preview.

Two background implementation workers own disjoint areas: Java/APK-only JS/frontend packaging and tests; manifest/icon resources/native APK packaging. The parent owns integration review, documentation, final build and delivery. Existing uncommitted work and production services are preserved.

An ENOSPC interruption truncated only the newly appended heading in this file; the preceding record was verified intact and the heading restored. With explicit user permission, the re-downloadable Bun cache at `~/.bun/install/cache` was removed, restoring approximately 3.8 GiB of root filesystem space. Temporary build artifacts remain on the separate `/tmp` filesystem. Both workers were paused and then resumed; no production services were changed.

Icon integration: the worker preserved the actual `favicon-v3.svg` path data and added adaptive resources, resource compilation, manifest versionCode 2 / versionName 0.1.1-preview, and version-derived APK naming. Parent review corrected the safe-area calculation: the original outer bounds are x=128..384, y=96..416 rather than a centered 256px square. A centered 0.75 scale places the farthest corner at 32.42dp within the 33dp safe-circle radius; real `aapt2 compile` passed after this adjustment. The original delivery signing certificate SHA-256 is `12d79b6d8b9c7adae319b0d0ac2a3763ef8aaa17e45fbd30aaebb62667b9c11e`; final delivery must match it. Worker-generated temporary APKs use a different test key and are not delivery candidates.

Parent integration review fixed API 26–29 access to the API-30 `WindowInsets.CONSUMED` field by retaining legacy decor fitting/adjustResize there; API 30+ now explicitly opts into edge-to-edge before consuming insets, with display-cutout bottom included. Navigation-bar contrast scrims are disabled where supported. Native settings use an explicit dark/light dialog theme, page-derived background and contrasting input tint; translucent colors use the incoming theme's base rather than the previous theme. These Android paths compile against SDK 35 but still require real-device rendering checks.

Independent verification after integration: 251 full pytest checks passed with deprecations-as-errors; diff whitespace check passed. Pinned-source frontend build and signed native packaging succeeded. Chromium loaded 73 packaged resources against the real Gateway, with zero missing resources/page errors and no service workers; menu settings/reload and live dark/light color reports passed with native scheme navigation replaced by a recorder. This is not a WebView native-callback or IME acceptance claim. Candidate certificate matches the original preview; manifest is versionCode 2 / 0.1.1-preview and all packaged resource bytes match the build output. The first reviewer provider failed; a separate general-agent read-only review was requested.

Delivery: `opencode-mesh-0.1.1-preview.apk`, 10,585,171 bytes, SHA-256 `e2dd1d52814179fbaccc12f0cb428596d32e6ac1610a4bf999d869015fa886c9`. All 1,103 bundled resource hashes verified inside the APK; v2/v3 signatures verified and the certificate matches 0.1.0. Copies and checksum files were written to the existing Windows delivery folder and the private dated artifact archive, with exclusive creation and post-copy hash checks. The 0.1.0 file was preserved. Integration review at delivery was the parent's review of worker changes; the supplemental review arrived afterward, as recorded below. No commit, push or production deployment was performed.

Supplemental review disposition: verified pinned `public/oc-theme-preload.js` reads stored/system appearance and sets HTML theme, color scheme and background synchronously, before the SPA's network bootstrap; the proposed always-light initial-frame diagnosis is therefore unsupported. The password field uses `TYPE_TEXT_VARIATION_PASSWORD`, so prefilled does not mean visibly unmasked or readable by page JavaScript. Absence of a threading annotation does not establish an off-UI-thread WebView callback; no crash was reproduced and no speculative threading change was made. Scheme handling and native rendering remain real-device acceptance items.

Deferred minor: native recovery controls currently cover discovery/priming failures, not arbitrary post-priming main-frame load errors. Missing packaged entry HTML could leave no usable web menu, although the delivered bundle's complete resource checks passed. A Gateway outage on reload is not itself proof of that case: entry HTML and scripts are local and the Mesh bootstrap has its own failure UI. Consider a targeted post-priming main-frame-error recovery test before extending the native status flow. No executable files or delivered APK were changed following this review.
