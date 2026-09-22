# V2 最小传输 Implementation Plan

**Goal:** 在原生 V2 多 Server 基础上打通透明 P2P 与 Relay。

**Architecture:** 复用现有传输库；首先找到实际请求失败边界，再收敛状态和前端兼容逻辑。

**Tech Stack:** Python、FastAPI、httpx、websockets、aiortc、浏览器 WebRTC。

**Spec:** `docs/superpowers/specs/2026-09-23-v2-minimal-transport-design.md`

## Global Constraints

- 保留数据库和用户配置，保留 README 的 V1 基线。
- 不自动重放结果未知的 mutation；不通过改写业务请求体掩盖传输错误。
- 不要求手机安装 VPN；优先复用成熟库。

## Review Focus

- Request 与 init 的请求头/请求体覆盖语义。
- HTTP hop-by-hop 头与重新编码后的长度。
- 204、HEAD、取消及 SSE 生命周期。
- 设备切换时在途请求和终端的归属。
- 用户编辑 Server 名称后不会被设备同步覆盖。

## 执行记录

- [x] 核实 V1 基线并记录 README。
- [x] 抓取实际浏览器请求，与原生及 Relay/P2P 对比。
- [ ] 为确定根因编写失败行为测试，实施最小修复。
- [ ] 精简 V1 与全局设备推断逻辑，验证原生 Server 身份。
- [ ] 执行 pytest、Node 行为与语法检查、浏览器消息及终端检查。
- [ ] 独立审阅修改，部署并记录可验收项及限制。

## 调查记录与执行决定

- 采用主 agent 内联执行，子 agent 仅负责有边界的调查和测试。沿用用户授权的当前主线部署工作区。
- 真实 Chromium 新建验收会话：P2P 下发消息并得到 `MESH_OK`；禁用浏览器 RTC 后同样操作 `/agent` 返回 400，消息未提交。
- 同一上游无效 session 请求：普通 JSON 返回带 Content-Type 的 JSON 400；加入 Transfer-Encoding: chunked 后返回无 Content-Type 的空 400。httpx 构造后同时存在 Content-Length 与 Transfer-Encoding。
- Ruling: 先修复已证实的逐跳 HTTP 头转发，再精简前端 — 避免把大规模替换当成根因修复；代价是分两步验证部署。
- 浏览器测试依赖安装在 `/tmp/opencode/browser-check`，不进入产品运行依赖。
