# Device-Scoped OpenCode Tabs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every Mesh device a stable virtual OpenCode Server URL so the unmodified OpenCode Web frontend keeps separate native localStorage tabs, projects, and sessions per device.

**Architecture:** OpenCode will see `https://<mesh-host>/_mesh/device/<device_id>` as its server URL. Gateway strips that prefix, selects the device, and forwards the remaining HTTP/SSE/WS path unchanged. The injected Mesh bar will use the native OpenCode tab URL/session state, record the current device-scoped URL before switching, and restore a valid target-device tab instead of retaining an incompatible session ID.

**Tech Stack:** Python 3.11+, FastAPI, WebSocket, OpenCode Web frontend, browser localStorage, existing P2P DataChannel and Relay transport.

## Global Constraints

- Keep the internal namespace `/_mesh/`; do not expose a competing `/mesh/` namespace.
- Do not modify or fork OpenCode frontend source code.
- Reuse OpenCode native `localStorage` tabs/server/project records; do not create a second session database.
- IndexedDB remains reserved for OpenCode-native drafts and is not used for session identity.
- Preserve HTTP, SSE, WebSocket, PTY, P2P, and Relay behavior.
- Never commit real Gateway, OpenCode, or device credentials.

---

### Task 1: Add device-scoped gateway routing

**Files:**
- Modify: `src/main.py`
- Modify: `docs/protocol.md`

**Interfaces:**
- Add `Gateway.parse_device_route(path: str) -> tuple[str | None, str]`.
- Add `Gateway.select_device(device_id: str | None, req: Request) -> tuple[str, dict[str, Any], WebSocket | None] | None`.
- HTTP paths `/_mesh/device/<device_id>/<upstream-path>` and WebSocket paths `/_mesh/device/<device_id>/<upstream-path>` use the explicit device.

- [ ] **Step 1: Implement route parsing**

Recognize only `/_mesh/device/<non-empty-device-id>` and strip exactly that prefix. Return `(None, original_path)` for all existing routes. Reject traversal-like IDs containing `/`, `\\`, `.`, or empty components with a 404 response.

- [ ] **Step 2: Route HTTP requests to the explicit device**

In `proxy`, parse the route before the `_mesh/` control-route check. For a device-scoped request, select that device directly and forward the stripped path. Preserve the current default-device selection for ordinary paths. Keep `/global/health` and all other upstream paths unchanged after stripping.

- [ ] **Step 3: Route SSE using the stripped path**

Pass the stripped path to `stream_proxy`; preserve event-path detection and `Accept: text/event-stream`. The browser must receive the original upstream status, headers, heartbeats, and cancellation behavior.

- [ ] **Step 4: Route WebSocket upgrades to the explicit device**

For `/_mesh/device/<device_id>/<path>`, use the selected device and send `path="/<path>"` to the Agent. Keep the existing `/_mesh/ws/`, `/api/pty/`, and `/pty/` compatibility routes unchanged.

- [ ] **Step 5: Document the route contract**

Document that `/_mesh/device/<device_id>/api/...`, `/_mesh/device/<device_id>/event`, and `/_mesh/device/<device_id>/pty/...` are transparent device-scoped routes and that `/_mesh/*` remains reserved for Mesh control endpoints.

- [ ] **Step 6: Verify syntax and direct routes**

Run:

```bash
./.venv/bin/python -m py_compile src/main.py
```

Then authenticate against the deployed Gateway and verify both device-scoped `/global/health` paths return the corresponding OpenCode version.

### Task 2: Generate device-scoped OpenCode server identity

**Files:**
- Modify: `src/main.py`
- Modify: `src/static_adapter.py`

**Interfaces:**
- Add a browser helper `deviceServerUrl(deviceId)` returning `${location.origin}/_mesh/device/${encodeURIComponent(deviceId)}`.
- The Mesh adapter must preserve native request paths while using the device-scoped server URL for OpenCode navigation and storage identity.

- [ ] **Step 1: Expose the selected device in the transport manifest**

Return `device_id` and `server_url` from `/_mesh/transport-manifest`, where `server_url` is the device-scoped URL. Do not include credentials.

- [ ] **Step 2: Add device-scoped URL helpers to the injected adapter**

Add helpers to encode/decode the OpenCode server key used in `/server/<base64>/session/<id>` URLs. Ensure decoding is URL-safe and never treats arbitrary external URLs as Mesh device routes.

- [ ] **Step 3: Keep native OpenCode API calls working**

When OpenCode calls the device-scoped URL, the browser fetch/WebSocket/EventSource adapters must leave the `/_mesh/device/<device_id>/...` prefix intact so Gateway routing selects the same device. P2P messages must carry the stripped upstream path plus the selected device identity already represented by the DataChannel.

- [ ] **Step 4: Verify native-compatible routing**

In the browser, fetch the device-scoped `/api/health`, `/project`, and `/provider`; verify that the response comes from the selected device and that `localStorage` contains distinct server keys for the two device URLs.

### Task 3: Reuse native tabs for switching and recovery

**Files:**
- Modify: `src/main.py`
- Modify: `src/static_adapter.py`

**Interfaces:**
- Browser storage remains OpenCode-native: `opencode.global.dat:server`, `opencode.window.browser.dat:tabs`, `tabs.recent`, `tabs.info`, and `tabs.closed`.
- Add `captureCurrentTab()` and `restoreDeviceTab(deviceId)` in the injected Mesh bar script.

- [ ] **Step 1: Capture the current native tab before switching**

Read the current `/server/<encoded>/session/<id>` URL and associate it with the current manifest device. Do not create a duplicate Mesh session index. The native OpenCode tab record remains authoritative.

- [ ] **Step 2: Build a target-device tab candidate**

Read native tabs and choose, in order: the most recent session whose decoded server URL equals the target device URL; a target-device tab listed in `tabs.recent`; then the target device’s most recent project from the native server record.

- [ ] **Step 3: Validate the target session before navigation**

After setting the target device cookie, query the target device-scoped `/api/session/<id>` or legacy `/session/<id>`. Use the candidate only if it returns a non-error response. If it is missing, remove only that stale tab entry and fall back to the target project/list view.

- [ ] **Step 4: Navigate to the target native route**

Navigate to the target device’s encoded server URL and session ID when valid. If there is no valid session, navigate to the target device-scoped server root, not the public root and not the old device’s session URL.

- [ ] **Step 5: Preserve OpenCode’s own tab behavior**

Do not rewrite OpenCode’s tab storage format, session messages, draft IndexedDB, workspace layout, or project database. Mesh only supplies the stable device-scoped server identity and validates the selected route.

### Task 4: Improve device bar and login interaction

**Files:**
- Modify: `src/main.py`

- [ ] **Step 1: Use a real login form**

Use `<form>` and `submit` handling so clicking the button and pressing Enter execute the same login path. Preserve the existing JSON endpoint and error message.

- [ ] **Step 2: Render a wrapping device list**

Use a flex-wrapping device container with a bounded scroll area. Mark the manifest-selected device with `当前` and a distinct style. Show online/offline state without changing OpenCode layout behavior.

- [ ] **Step 3: Switch using recovery logic**

Replace `location.reload()` with `captureCurrentTab()`, cookie switch, target-session validation, and target-route navigation. Disable the clicked button during the transition and show a recoverable error if switching fails.

### Task 5: Compatibility migration and validation

**Files:**
- Modify: `src/main.py`
- Modify: `docs/opencode-web-capability-matrix.md`
- Modify: `README.md`

- [ ] **Step 1: Preserve old public URLs**

Keep ordinary public URLs working for existing bookmarks. If a URL has no device prefix, route it through the selected device as today. Only new and switched tabs use the device-scoped server identity.

- [ ] **Step 2: Validate both current devices**

Verify separately for `GTi15-Ultra` and `ehang-box`: login, project list, session list, provider response, SSE, P2P candidate pair, and Relay fallback.

- [ ] **Step 3: Validate session switching**

Open one session on each device, switch A → B → A, and confirm each device returns to its own session URL without a “session not found” error.

- [ ] **Step 4: Validate stale-session recovery**

Use a deliberately invalid target session ID and confirm the UI falls back to the target device’s project/session list without sending that ID to the wrong device.

- [ ] **Step 5: Update documentation**

Document native storage reuse, the device-scoped URL format, the compatibility behavior for old URLs, and the fact that optional cross-browser synchronization is not part of the first implementation.
