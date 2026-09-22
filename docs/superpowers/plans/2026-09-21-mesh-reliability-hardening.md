# Mesh Reliability Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. This plan is being executed inline by the primary agent; no sub-agent will modify code.

**Goal:** 修复 Mesh 的流数据丢失、连接永久阻塞、P2P 大帧和浏览器适配问题，并统一安装卸载行为与文档，使 Relay 和 P2P 在本地及现网部署中保持可验证、可恢复。

**Architecture:** 保留现有单文件 Gateway/Agent 架构和 Gateway/Agent/P2P 三层边界。先在 `src/main.py` 中统一控制消息超时、流状态、响应上限和双向分片，再在 `src/static_adapter.py` 对齐浏览器端状态机；最后修复 shell/systemd/文档。协议变更同时修改两端，不增加新的外部依赖。

**Tech Stack:** Python 3、FastAPI/Starlette、httpx、websockets、aiortc、原生浏览器 Fetch/XHR/WebSocket/EventSource、Bash、systemd。

## Global Constraints

- 现有 WSL 到 Windows 的 Relay 路径必须继续可用。
- ehang-box 的局域网 P2P 路径必须继续可用。
- 不修改 Lucky、Xray、Hysteria、FRP、防火墙或真实 OpenCode。
- 所有批次先在本地验证，全部完成后才部署公网和设备。
- 不提交真实凭据、token、设备身份或运行时状态。
- 不引入新的外部依赖。
- 不自动创建 Git commit；每批以工作区 diff 和验证结果作为 checkpoint。

## 文件地图

- Modify: `src/main.py`，Gateway/Agent 控制连接、代理流、P2P 消息和生命周期。
- Modify: `src/p2p.py`，P2P 编码/解码辅助逻辑和清理边界。
- Modify: `src/static_adapter.py`，浏览器 Fetch/XHR/WS/SSE、P2P/Relay 回退和状态栏。
- Modify: `scripts/install.sh`，安装配置、HTTP Gateway、systemd 和 token 输出。
- Modify: `scripts/uninstall.sh`，模式校验、数据保留和 linger 清理。
- Modify: `scripts/deploy-agent.sh`，远程写配置、linger、HTTP Gateway 和启动顺序。
- Modify: `config/gateway.example.json`、`deploy/Caddyfile.example`、`deploy/*.service.example`，统一端口和作用域。
- Modify: `README.md`、`docs/protocol.md`、`docs/opencode-web-capability-matrix.md`，同步行为和公开信息。
- Create: `tests/test_mesh_reliability.py`，只使用现有 Python 运行环境验证协议状态、限制和清理逻辑。

---

### Task 1: 建立可靠性回归基线

**Files:**
- Create: `tests/test_mesh_reliability.py`
- Modify: `pyproject.toml` only if the existing test command requires an explicit test path; do not add dependencies.

**Interfaces:**
- Consumes: existing `src.main` helpers and protocol constants.
- Produces: deterministic local checks for stream messages, size limits, install/uninstall shell behavior, and frontend message sequencing.

- [ ] **Step 1: Add tests for the currently failing P2P stream sequence**

  Assert that a status frame followed by two chunks and an end frame produces one response plus both chunks and a close event; assert that an error frame closes with an error. Keep the test independent of a real browser by modeling the frame reducer.

- [ ] **Step 2: Add tests for size and sequence guards**

  Cover a payload exactly at the configured limit, one byte over the limit, a duplicate sequence, a missing sequence, and a repeated end frame. Expected behavior is acceptance only for the valid sequence and a bounded error for all invalid sequences.

- [ ] **Step 3: Run the focused baseline tests**

  Run: `python -m pytest -q tests/test_mesh_reliability.py`

  Expected before implementation: the new regression cases that describe existing defects fail; no unrelated collection error is allowed.

### Task 2: Harden Gateway/Agent control and stream protocol

**Files:**
- Modify: `src/main.py:150-220, 300-620, 650-850, 880-1000`
- Modify: `src/p2p.py` only where shared body/frame encoding is required.
- Modify: `tests/test_mesh_reliability.py`

**Interfaces:**
- Consumes: existing `send_to_device`, `enqueue_stream`, P2P message handlers, `max_request_bytes`, and device registry.
- Produces: one bounded control-send helper, one bounded payload reassembler, explicit stream overflow errors, and deterministic connection cleanup.

- [ ] **Step 1: Define bounded protocol constants and helpers**

  Add internal constants for control-send timeout, maximum response bytes, maximum P2P message bytes, and chunk size. Add a helper with the equivalent behavior of `send_control(device_id, message, timeout)` that wraps every Agent WebSocket send in `asyncio.wait_for`; it must close the device connection on timeout and raise the existing proxy-level error type.

- [ ] **Step 2: Replace unbounded control sends**

  Route WebSocket open/data/close, stream start, cancel, P2P offer, and browser-close messages through the helper. Keep the existing 10-second HTTP request timeout and use a shorter bounded control timeout for control frames. Ensure each caller converts timeout into a response/stream error instead of leaving a handler pending.

- [ ] **Step 3: Add bounded bidirectional frame assembly**

  Define one JSON chunk envelope containing `message_id`, `sequence`, `data`, and `final`. Use it for P2P request bodies, WebSocket data, ordinary responses, and stream chunks. Reject duplicate, skipped, oversized, or incomplete sequences and remove assembly state in a `finally` block.

- [ ] **Step 4: Enforce response limits before base64 expansion**

  Track decoded byte count while receiving Agent response data. Abort with a bounded error once the response limit is exceeded; do not create a second full-size copy merely to check the limit. Apply the same limit to Relay queue accumulation and P2P reassembly.

- [ ] **Step 5: Fix stream queue overflow semantics**

  Change `enqueue_stream` so `QueueFull` emits one `stream_error` with a stable overflow reason and closes the stream instead of evicting old chunks. Ensure the consumer removes queue state and pending futures on all normal, error, timeout, and cancellation paths.

- [ ] **Step 6: Fix device and peer lifecycle**

  Reject or close the old same-device control connection before replacing it. On control reconnect, close all old P2P peers and fail pending request/answer futures. Make cleanup idempotent and ensure old connections cannot update `last_seen` or resolve current pending requests.

- [ ] **Step 7: Improve registration retry and recovery**

  Parse 429 separately in the Agent registration loop and use exponential backoff with jitter. Add an enroll-token-protected recovery operation or a documented automatic identity regeneration path for a persisted device token that no longer matches Gateway state.

- [ ] **Step 8: Run focused backend verification**

  Run: `python -m pytest -q tests/test_mesh_reliability.py`, `python -m py_compile src/main.py src/p2p.py`, and `python scripts/check_auth.py`.

  Expected: all focused tests pass; Python compilation and authentication smoke checks pass.

### Task 3: Repair browser transport and P2P behavior

**Files:**
- Modify: `src/static_adapter.py:100-470`
- Modify: `src/p2p.py` only if browser-visible frame encoding requires a matching helper.
- Modify: `tests/test_mesh_reliability.py`

**Interfaces:**
- Consumes: Task 2 chunk envelope and stable stream error reasons.
- Produces: correct `MeshFetch`, `MeshXMLHttpRequest`, `MeshWebSocket`, and `MeshEventSource` behavior over P2P or Relay.

- [ ] **Step 1: Fix P2P stream reducer ownership**

  Keep a stream entry after the initial status frame resolves the first response. Enqueue later chunks and close only on end; on error, call the stream controller error path and delete all associated state. Add a transport-close cleanup that settles every pending request and stream exactly once.

- [ ] **Step 2: Add browser-side chunk encode/decode**

  Implement the same `message_id`/`sequence`/`data`/`final` envelope as Task 2. Chunk browser request bodies and WebSocket data before `channel.send`; reassemble Agent responses with a byte limit and reject invalid sequences.

- [ ] **Step 3: Correct binary conversion**

  Convert Blob with `arrayBuffer()`. Convert TypedArray/DataView using `byteOffset` and `byteLength`, not the whole backing buffer. Preserve `binaryType` when delivering incoming data and retain exact byte content.

- [ ] **Step 4: Add XHR and WebSocket protocol support**

  Route XMLHttpRequest open/send/response handling through the existing fetch transport logic. Include requested WebSocket subprotocols in `ws_open`, pass them to Agent, and expose the negotiated protocol. Preserve existing text/binary semantics.

- [ ] **Step 5: Add P2P retry and route-device guards**

  Make initial P2P failure schedule bounded exponential retries. Every retry reads the current route device ID. On device switch, cancel old retry timers and close the old peer before starting the new route.

- [ ] **Step 6: Add SSE reconnect behavior**

  Reconnect `MeshEventSource` on retryable transport errors with bounded backoff, send `Last-Event-ID` on the next request, and preserve event parsing across chunk boundaries. Do not reconnect on explicit close or authentication failure.

- [ ] **Step 7: Add safe request IDs and cleanup**

  Use `crypto.randomUUID()` when available and a cryptographically unimportant local fallback in insecure contexts. Clear timers, streams, peers, and pending entries on page unload, explicit close, route change, and failed negotiation.

- [ ] **Step 8: Run frontend verification**

  Run: `node --check src/static_adapter.py` only if the adapter is JavaScript extracted by the repository workflow; otherwise run the repository’s existing adapter syntax check. Execute the Node message-sequence harness from `tests/test_mesh_reliability.py` or its equivalent and verify status/chunk/end, error, binary offset, retry, and Last-Event-ID cases.

  Expected: no syntax error; all browser transport regression cases pass.

### Task 4: Fix installation, deployment, and uninstall safety

**Files:**
- Modify: `scripts/install.sh`
- Modify: `scripts/uninstall.sh`
- Modify: `scripts/deploy-agent.sh`
- Modify: `deploy/opencode-mesh-agent.service.example`
- Modify: `deploy/opencode-mesh-gateway.service.example`
- Modify: `config/gateway.example.json`
- Modify: `deploy/Caddyfile.example`
- Modify: `tests/test_mesh_reliability.py`

**Interfaces:**
- Consumes: existing environment variables and current root/user installation modes.
- Produces: safe explicit uninstall modes, consistent 18080 Gateway examples, correct linger handling, and atomic remote Agent setup.

- [ ] **Step 1: Add uninstall mode validation tests**

  Exercise `uninstall.sh` with no argument, `agent`, `gateway`, `all`, and an invalid value in a temporary fake installation directory. Expected: no-argument and invalid input exit nonzero without deleting data; valid modes select only their documented service scope.

- [ ] **Step 2: Implement safe uninstall behavior**

  Remove the default `all` behavior. Validate mode before constructing service names or calling `rm -rf`. Preserve `data/` in place when `MESH_KEEP_DATA=y`; otherwise print the exact deletion scope before proceeding. Disable only the matching user linger scope after unit removal.

- [ ] **Step 3: Make remote deployment atomic**

  In `deploy-agent.sh`, transfer files, write and chmod the local config, validate its JSON and paths, then enable/restart the service. Enable linger before starting a user service and report when `systemctl --user` is unavailable. A failed config write must not leave an enabled crash-loop.

- [ ] **Step 4: Align HTTP Gateway and unit templates**

  Apply the same HTTP scheme detection and `allow_insecure_gateway` warning as `install.sh`. Make user/system unit paths and `WantedBy` targets match their installation scope. Use `18080` consistently in Gateway config and Caddy examples.

- [ ] **Step 5: Reduce token exposure**

  Replace the printed command containing inline `MESH_ENROLL_TOKEN` with a command that reads the token interactively or from an explicitly supplied environment file, and ensure logs never print the token value.

- [ ] **Step 6: Run script verification**

  Run: `bash -n scripts/install.sh scripts/uninstall.sh scripts/deploy-agent.sh`, the temporary uninstall mode harness, and `python scripts/check_auth.py`.

  Expected: all syntax and safety checks pass; no repository or temporary test creates a tracked credential.

### Task 5: Synchronize configuration and documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/protocol.md`
- Modify: `docs/opencode-web-capability-matrix.md`
- Modify: `config/gateway.example.json`
- Modify: `deploy/Caddyfile.example`
- Modify: `deploy/*.service.example`
- Modify: `tests/test_mesh_reliability.py`

**Interfaces:**
- Consumes: final protocol names, limits, retry behavior, installation modes, and port from Tasks 2-4.
- Produces: user-facing documentation that is executable and does not expose private environment details.

- [ ] **Step 1: Document the complete frame contract**

  Describe request/response, stream start/chunk/end/error, WebSocket open/data/close, ping/pong, cancellation, registration, P2P offer, chunk sequence rules, limits, and retry/error semantics in `docs/protocol.md`.

- [ ] **Step 2: Correct capability claims**

  Update the capability matrix to distinguish verified P2P SSE, Relay SSE, reconnect support, XHR support, binary WebSocket behavior, and known version assumptions. Remove claims that exceed the implemented behavior.

- [ ] **Step 3: Rewrite deployment examples**

  Use the actual 18080 local Gateway port and current service scope in all examples. Keep the public Gateway URL configurable rather than embedding private machine addresses or runtime device identities.

- [ ] **Step 4: Update README behavior and recovery instructions**

  Document explicit uninstall modes, data retention, token handling, HTTP test warning, P2P/Relay selection, retry behavior, and recovery for stale device identity. Keep the user-facing README concise.

- [ ] **Step 5: Scan tracked files for secrets and private addresses**

  Run a repository search for known private IPs, runtime device IDs, token-shaped values, and the live Gateway credential. Remove only tracked documentation leaks; do not touch ignored runtime files.

### Task 6: Full local verification and deployment checkpoint

**Files:**
- Modify only files required by failed verification; do not make unrelated cleanup changes.

**Interfaces:**
- Consumes: completed Tasks 1-5.
- Produces: verified release candidate and deployment report.

- [ ] **Step 1: Run all static checks**

  Run Python compilation, shell syntax checks, adapter syntax checks, `python scripts/check_auth.py`, and `python -m pytest -q`.

  Expected: all commands pass with no untracked credentials.

- [ ] **Step 2: Run local end-to-end checks**

  Start a local Gateway, Agent, and fake OpenCode. Verify HTTP, SSE, WebSocket, PTY, Relay, P2P, P2P initial failure and retry, route-device switch, large framed payloads, stream overflow, cancellation, and control timeout.

- [ ] **Step 3: Inspect the final diff**

  Run `git status --short`, `git diff --check`, and `git diff --stat`. Confirm only intended source, scripts, docs, tests, and design/plan files changed. Do not commit automatically.

- [ ] **Step 4: Deploy once after all local checks pass**

  Update VPS Gateway, WSL Agent, and ehang-box Agent using the existing deployment paths. Do not alter unrelated VPS services or real OpenCode configuration.

- [ ] **Step 5: Perform live acceptance**

  Verify Gateway authentication, public mobile Relay access, WSL online state, ehang-box P2P state and RTT, SSE/WebSocket/PTY behavior, device switching, reconnects, and offline page behavior. Record any residual issue before claiming completion.
