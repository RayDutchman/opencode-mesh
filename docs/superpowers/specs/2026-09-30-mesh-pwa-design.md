# Mesh 网关级单一 PWA 设计规格

**日期：** 2026-09-30
**基线：** 分支 `feat/android-apk`，HEAD `bcb5e1d`，运行 `1c31e10`，版本 `0.3.2`；上游契约以 `v2.0.18` 为准
**状态：** 写作时仅设计与计划，未改产品代码、未提交、未部署、未改 `android/`、未打 APK。**后续（2026-09-30）：已实现并先后部署 `2f5e845` 与 `814fda2`（身份隔离与匿名图标），未改 `android/`、未打 APK；真机 WebAPK 安装仍待用户验收，见 §7 待验项。**

## 1. 目标与已批准决定

由 Gateway 自管 manifest 与根 scope Service Worker，恢复 Mesh 网页的可安装 PWA，保留原生 OpenCode UI 与既有传输语义，作为 Android 客户端的可替代路径。

已批准的默认（不再逐项请示）：

- Gateway 级单一 PWA：`id` 为固定 `/_mesh/pwa`，`start_url`/`scope` 为 `/`，冷启动回落到网关根，由既有首页选择与恢复页决定设备。**（2026-09-30 用户批准的边界调整：原定 `id` 为 `/`，因同源旧原生应用已占用该身份导致启动器名称在 Mesh/OpenCode 间切换。）**
- manifest 与图标由 Gateway 提供，字节入库，不依赖任何在线 Agent。
- manifest 与 `/sw.js` 保持 Basic Auth 保护；manifest 链接注入 `crossorigin="use-credentials"`。**（2026-09-30 用户批准的边界调整：两个图标的精确路径 GET/HEAD 匿名可读，其余仍需凭据。）**
- `/sw.js` 由 Gateway 提供，接管原生注册的同 URL、同 scope `/`。
- Service Worker **不注册 `fetch` 处理，不读写 CacheStorage**。旧缓存留在浏览器磁盘上；新 worker 无 `fetch` 处理即不会使用它们，不需要任何清理动作。
- HTML 规范化注入 manifest 链接（替换既有链接、缺失时在 `<head>` 插入、去重），不为 PWA 让整页失败。
- 恢复页同样带 credentialed manifest 链接与内联注册。
- 新路径一律 `Cache-Control: no-cache`；`/sw.js` 附 `Service-Worker-Allowed: /`。
- 不改 `src/static_adapter.py`；不改业务认证；不改设备归属语义。

**保留的唯一边界（需用户明确接受）：** 网关不可达时冷启动，应用无法加载，显示浏览器自身的离线错误页。Mesh 不提供离线壳，也不提供网关不可达时的自定义提示页。网关可达但设备不可用时，既有恢复页照常工作。

## 2. 事实

### 2.1 已核实

**线上 HTTP（经主 agent 提供，curl 层）**

| 观测 | 含义 |
|---|---|
| 根 HTML 的 `manifest` 链接指向 `/_mesh/device/{id}/site.webmanifest` | `rewrite_device_html` 把 `href="/site.webmanifest"` 改写成设备路径，manifest 由**设备**提供 |
| 该 manifest 的 `id`/`start_url`/`scope` 全为 `/`，`display: standalone` | 上游 manifest 契约本身满足单应用语义 |
| 192/512 图标按裸路径 200 | 图标请求落到 catch-all，按默认设备代理到 Agent |
| `/sw.js` 200、`no-cache`、内容为上游 Workbox worker | `/sw.js` 无显式路由，落到 `proxy()` 并按默认设备代理 |
| 同一 `/sw.js` precache 中 `index.html` 的 SRI `sha256-Pf1gRFQ9…ZdcY=`，实际 Mesh HTML 为 `sha256-gcLud9Dj…PxQ=` | 预缓存副本与 Mesh 实际返回的 HTML **字节不匹配**（SRI 不同） |
| 导航 fallback 为 `/index.html`，denylist 未排除 `_mesh` | `/_mesh/**` 导航会命中 fallback 路径 |

**上游 v2.0.18 源码**

- `packages/app/src/entry.tsx`：`window.addEventListener("load", () => void navigator.serviceWorker.register("/sw.js"), { once: true })`。无 `scope`、无 `updateViaCache`，脚本在根 ⇒ 默认 scope `/`。
- `packages/app/index.html`：`<link rel="manifest" href="/site.webmanifest" />`（**无 `crossorigin`**）、`rel="icon"` 指向 `%OPENCODE_FAVICON%`、`rel="apple-touch-icon"` 指向 `%OPENCODE_APPLE_TOUCH_ICON%`、`theme-color` 为 `#fafafa`、两个 `*-web-app-capable` 为 `yes`。
- `packages/app/vite.pwa.ts`：`generateSW`、`registerType: "prompt"`、`manifest: false`、`navigateFallback: "/index.html"`、`navigateFallbackDenylist: [/^\/(?:api|auth)(?:\/|$)/, /^\/(?:_assets|assets)(?:\/|$)/]`、`globPatterns: ["**/*"]`、precache 项带 SHA-256 `integrity`；未开启 `skipWaiting`/`clientsClaim`。
- `packages/app/vite.icons.ts` + `manifest.json`：`site.webmanifest` 由插件生成（`public/site.webmanifest` 不存在），字段 `id`/`start_url`/`scope` 为 `/`、`display: standalone`、`theme_color`/`background_color` 为 `#080808`，两张 192/512 `purpose: "maskable"` 图标，源为 `packages/desktop/icons/{channel}/android/mipmap-xxxhdpi/ic_launcher.png` 与 `icon.png`。
- `packages/app/src/runtime/platform/pwa.ts`：`LAST_ROUTE_KEY = "opencode.pwa.last-route"`；`restorePwaRoute()` 仅在 `display-mode: standalone` 且 pathname 为 `/`、无 query/hash 时生效；允许 `/`、`/new-session`、`/server/:key/session/:id`。

**本仓库实现**

- `src/main.py:656` `routes()` 内的 `basic_auth_middleware`（660）只放行 `/_mesh/register`、`/_mesh/agent/*`、`/_mesh/deregister/*`。
- `src/main.py:942` catch-all `@app.api_route("/{path:path}", ...)`；未命中显式路由且路径以 `_mesh/` 开头时返回 404 JSON（963）。新 PWA 路由必须显式注册并早于 catch-all。
- `src/main.py:101` `rewrite_device_html()`：正则 `((?:src|href)\s*=\s*["'])/(?!/|_mesh/)` 改写根相对资源；`/_mesh/` 前缀因负向前瞻不被二次改写。
- `src/main.py:1050` 附近：设备 HTML 经 `rewrite_device_html(..., bootstrap=True)` 与 `inject_mesh_bar()` 后带 `Cache-Control: no-store`；恢复页同样 `no-store`（626）。
- `src/frontend.py`：`ASSET_ROOT = "/_mesh/ui/2/"`、`asset_prefix()`、`parse_asset_route()`、`adapt_entry()`。
- `src/static_adapter.py`：`syncNativeServers()` 已做 `opencode.pwa.last-route` 的设备化迁移；当前不注册也不注销 Service Worker。

**外部规范**

- MDN（Progressive web apps › Manifest，Deploying a manifest）：manifest 需要凭据才能获取时，`crossorigin` 必须为 `use-credentials`，**即使同源**。HTML 规范中 manifest 请求在无 `crossorigin` 时凭据模式为 `omit`。
- W3C Web App Manifest §1.6/§5：缺 `scope` 时"default scope"取 **`start_url` 去掉文件名、query、fragment**；`start_url` 也缺时取安装文档 URL。规范建议始终显式声明 `scope`。
- Chrome 官方博客（Revisiting Chrome's installability criteria）：自 Chrome 108（移动）/112（桌面）起，菜单安装不再要求 Service Worker 实现 `fetch()`；自动安装提示算法当时仍在调整，未确证 2026 年现状。
- 上游许可：`v2.0.18` 根 `LICENSE` 为 **MIT License, Copyright (c) 2025 opencode**（文件 SHA-256 `625f0f619133f89bbbb2abe37369613dfa1885eba1e50d02170deb62cb6b`）；`packages/ui/src/assets/favicon/` 下无独立 LICENSE，资产随根 MIT 通知分发。
- 图标源：`packages/ui/src/assets/favicon/favicon-v3.svg` @ v2.0.18，512×512 viewBox，SHA-256 `e29bbe33380ad1c1ada9134b52f229d30e9776d60481512c9d81f2bb6f37def9`。本机可用栅格化工具 `rsvg-convert` 2.61.3。

### 2.2 未捕获的观测（写作时快照，不得当成已观测）

本小节是**设计写作时**的状态，保留原样不改写；其后的浏览器验收结论见 §2.3。

- **线上浏览器是否因缺 `crossorigin="use-credentials"` 而拿不到 manifest：当时未观测。** 只知道规范要求该属性、且当前 HTML 缺该属性；浏览器层是否真的 401、是否影响安装，均无捕获证据。列为风险 R-1 与待验项 V-1。
- **旧 Workbox worker 是否实际影响过页面加载：因果未证实。** 只知道 SRI 字节不匹配、`/_mesh` 导航在 fallback 范围内。
- **浏览器是否会给 SW 脚本请求带上 Basic Auth 凭据：当时未观测**（V-2）。
- SRI 不匹配的**读路径**行为随 Workbox 版本而异，未在本轮确认；本设计不依赖该行为。

### 2.3 浏览器验收后的观测更新（2026-09-30，本地 loopback）

范围限定：真实 Chromium + 隔离 profile + **临时 Gateway（无 Agent）** + 走浏览器原生 Basic Auth 挑战（未使用 `Network.setExtraHTTPHeaders` 塞 `Authorization`）。因此本节证据**不覆盖线上 HTTPS、真实设备 HTML 页面与 Android 手机**。

- 恢复页 200，且该导航**不是**由 Service Worker 应答（无 fetch 处理函数的直接观测）。
- manifest 200，HTML 内链接为 `crossorigin="use-credentials"` 的网关链接；CDP manifest 无解析错误，installability errors 为空。
- `/sw.js` 以同 URL 接管旧 fixture worker，根 scope `/` 安装/激活完成，未 401 → R-2 在此环境下不成立。
- 旧 CacheStorage 条目与 localStorage sentinel 完整保留，Mesh 未新增任何缓存条目。
- 192/512 图标经网络层 200（628/1964 字节）。headless 下 CDP `Page.getManifestIcons` 的 optional `primaryIcon` 为空——既不证明图标失败，也**不构成启动器图标已验证**。

仍为**未观测**：线上 HTTPS 下缺 `crossorigin` 的原始表现（V-1 的另一半）、V-3 安装提示、**手机冷启动 last-route 归属（V-5）**、真实安装与启动器图标（V-6）。

## 3. 缺陷与设计要点

1. manifest 与图标来自被代理的设备与默认设备路由：默认设备不可用时，manifest/图标请求会失败或落到恢复页。
2. 设备 HTML 的 manifest 链接缺 `crossorigin` 属性（R-1）。
3. `/sw.js` 由默认设备提供，其 precache 副本与 Mesh HTML 字节不匹配，导航 fallback 的 denylist 不含 `_mesh`。

处置：让旧 worker 退场，manifest 与图标改由网关提供，并补上规范要求的 `crossorigin` 属性。**不依赖对旧缓存行为的任何假设**。

## 4. 方案

### 4.1 Service Worker

```js
// OpenCode Mesh gateway service worker, version <__version__>
// Replaces the upstream OpenCode worker registered at the same URL.
// There is no fetch handler on purpose: Mesh never serves cached content,
// and HTML/API/SSE/WebSocket traffic keeps its existing HTTP cache behavior.
// Any cache left by the previous worker stays untouched and unused.
self.addEventListener('install', () => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});
```

- 无 `fetch`、无 `caches`、无 `importScripts`、无 `respondWith`。**不枚举、不删除任何缓存**，也不碰 `localStorage`/IndexedDB。
- 内嵌 `src.__version__` 注释行，保证每次发布字节变化，更新检查必然成功。
- `skipWaiting()` + `clients.claim()`：上游 `registerType: "prompt"` 且未开 `skipWaiting`/`clientsClaim`，不主动接管则旧 worker 会一直控制已打开页面直到所有客户端关闭。新 worker 无 `fetch` 处理，接管在功能上中性（不经 SW 缓存），且 `controllerchange` 不会自动重载页面。
- **保证边界**：无 `fetch` 处理只保证请求不经 SW 缓存返回；请求仍遵循既有 HTTP 缓存语义（HTML `no-store`、`/_mesh/ui/2/**` 命名空间资源、API/SSE/WS 各自的既有策略），不额外承诺"必然到达 network"。

### 4.2 路由与资源

常量放 `src/frontend.py`（与 `ASSET_ROOT` 同处）：

```text
PWA_SW_PATH = '/sw.js'
PWA_MANIFEST_PATH = '/_mesh/pwa/manifest.webmanifest'
PWA_ICON_192 = '/_mesh/pwa/icon-192.png'
PWA_ICON_512 = '/_mesh/pwa/icon-512.png'
```

路由在 `src/main.py` 的 `routes()` 内注册，**早于 catch-all**，仅 `GET`/`HEAD`：

| 路径 | 响应 | 关键头 | 无凭据 |
|---|---|---|---|
| `/sw.js` | §4.1 脚本，版本行注入 `src.__version__` | `text/javascript; charset=utf-8`、`Cache-Control: no-cache`、`Service-Worker-Allowed: /` | 401 |
| `/_mesh/pwa/manifest.webmanifest` | 代码生成的 JSON | `application/manifest+json`、`Cache-Control: no-cache` | 401 |
| `/_mesh/pwa/icon-192.png`、`icon-512.png` | `src/assets/pwa/` 字节 | `image/png`、`Cache-Control: no-cache` | 200（GET/HEAD，唯一匿名例外） |

**图标匿名例外（2026-09-30 用户批准的边界调整）**：Chrome 在生成 WebAPK 时以 `CredentialsMode::kOmit` 取 manifest 图标，带 Basic Auth 挑战会丢掉启动器图标。因此认证中间件放行**精确匹配**这两个图标路径的 `GET`/`HEAD`。约束：manifest、`/sw.js`、页面、API 与其他方法仍需凭据（未认证写图标 401、认证写图标 405）；白名单按精确路径集合匹配，变体路径（尾斜杠、大小写、未提供尺寸、子目录、后缀、编码斜杠）仍 401，不得扩成前缀。研究同时确认存在 bitmap fallback，因此这是兼容风险处置，不等于已证实的手机根因。

图标字节放包内 `src/assets/pwa/`，以 `Path(__file__).resolve().parent / "assets" / "pwa"` 解析；缺失时启动即显式报错。`scripts/install.sh` 用 `pip install -e` 且 `WorkingDirectory` 为安装目录，包内数据随检出可用；若将来改 wheel 分发需补 `package-data`（记录为约束）。

测试需断言：这些路径**不产生任何设备请求**（假 Agent 计数为 0），且不依赖已注册设备即可 200。

manifest 文档：

```json
{
  "name": "OpenCode Mesh",
  "short_name": "Mesh",
  "id": "/_mesh/pwa",
  "start_url": "/",
  "scope": "/",
  "icons": [
    {"src": "/_mesh/pwa/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
    {"src": "/_mesh/pwa/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}
  ],
  "theme_color": "#fafafa",
  "background_color": "#fafafa",
  "display": "standalone"
}
```

- `scope` 显式声明：缺省时规范取 `start_url` 的父路径（此处恰为 `/`），显式声明可避免依赖缺省行为。
- `start_url` 不带 `?mesh_device=`：该参数是一次性 handoff，设备选择由既有首页选择与恢复页负责。
- **`id` 为 `/_mesh/pwa`（2026-09-30 用户批准的边界调整）**：浏览器按「源 + id」识别已安装应用，同源旧原生应用已占用 `id` `/`，两者会合并为一个启动器条目，名称与图标随最后一次写入而变。`id` 按规范以 `start_url` 为基准解析，是身份标识而非入口，因此不要求可导航；`start_url` 与 `scope` 保持 `/`，冷启动行为不变。旧已安装应用不卸载、不清缓存。
- `theme_color`/`background_color` 与原生页面 `background-color: var(--v2-background-bg-deep, #fafafa)` 及 `OFFLINE_PAGE` 浅色底一致（上游用 `#080808`）。纯外观。

### 4.3 HTML 规范化

`rewrite_device_html()` 在通用根相对改写之前处理 PWA 链接，且**不让页面失败**：

1. 把所有 `rel="manifest"` 链接（属性顺序不敏感，数量不限）替换为唯一规范标签：`<link rel="manifest" crossorigin="use-credentials" href="/_mesh/pwa/manifest.webmanifest" />`。
2. HTML 中没有 manifest 链接时，在 `<head>` 内插入该标签；无 `<head>` 时按现有 HTML 片段处理方式退化为追加，不抛错。
3. `rel="icon"` 与 `rel="apple-touch-icon"` 整标签替换为指向 `/_mesh/pwa/icon-192.png` 的标签（`rel="icon"` 必须整标签替换，避免遗留 `type="image/x-icon"` 与 PNG 不符）。
4. 产物以 `/_mesh/` 开头，通用改写的负向前瞻 `(?!/|_mesh/)` 不会二次改写。

理由：为 PWA 而让无关的 HTML 变体整页 500 是不可接受的风险；`adapt_entry` 的 fail-closed 只保留在既有的入口契约上。`og:image`/`twitter:image` 继续走设备路径，不扩大范围。

**2026-09-30 评审修订（保留上文规格，只补匹配机制）**：上述第 1、3 条按"真实 `rel` 属性的 token 列表"判定，不再用正则匹配整标签。原因是正则无法区分三件事：`\brel` 会命中 `data-rel`/`x-rel`/`aria-rel`（从而删掉整条 stylesheet 标签）；`[^>]*>` 会在引号内的 `>` 处提前截断并留下残渣；`<script>` 字符串与 HTML 注释里的同样文本也会被改写。实现改用 stdlib `html.parser.HTMLParser` 定位真实标签与真实 `</head>` 边界，用 latin-1 解码使字符偏移等于字节偏移，只做字节拼接而不重新序列化整份 HTML。规格层面的目标（唯一规范标签、幂等、缺失与无 `</head>` 时不抛错）不变。

### 4.4 恢复页

`OFFLINE_PAGE`（`src/main.py:158`）增加同一 credentialed manifest 链接，以及内联 `navigator.serviceWorker.register('/sw.js', {scope: '/'}).catch(() => {})`。目的：在"网关可达、设备不可用"状态下页面仍有可安装元数据与已接管的 worker。页面继续 `no-store`。

### 4.5 与既有语义的相互作用

- **设备归属不变**：冷启动进入首页选择路径；显式设备页面与 `/server/{key}/...` 路由不变；结果未知的 mutation 失败即失败，不重放、不改投。
- **传输不变**：worker 不经手请求 ⇒ 页内 P2P、Relay HTTP、SSE、WebSocket、PTY 行为不变。WebSocket 升级本就不经过 SW。
- **last-route 冷启动**：`restorePwaRoute()` 在 standalone 且 `/` 无 query/hash 时把 `opencode.pwa.last-route` 恢复为 `/server/{key}/session/{id}`；Mesh 已把该 key 迁移为设备化 Server URL。既有启动路径可能先落到默认设备、再随恢复路由切到目标设备——本设计**不要求零预连接或"只协商一次"**，只验证最终目标与在途归属（V-5）。
- **缓存面**：SW 不写任何缓存，因此没有版本键、容量上限或跨账号残留；换密码/换账号无需 SW 清理。原生前端自身在 localStorage/IndexedDB 的数据按浏览器 origin 隔离，Mesh 不清不迁。
- **契约升级**：`adapt_entry` 契约变化时按既有约定递增命名空间（如 `/_mesh/ui/3/`），不依赖 SW 参与失效。

## 5. 资产与许可

- 新增 `src/assets/pwa/`：`icon-192.png`、`icon-512.png`、`LICENSE-OpenCode.txt`（上游 v2.0.18 根 LICENSE 原文，MIT）、`README.md`（源路径、固定 tag、源文件 SHA-256、生成命令与工具版本、产物 SHA-256、许可结论）。
- 源：`packages/ui/src/assets/favicon/favicon-v3.svg` @ v2.0.18（MIT，无独立资产许可文件，随根 MIT 通知分发）。派生产物按 MIT 要求随附版权与许可声明。
- 渲染：`favicon-v3.svg` 画布 512×512，图形外接范围约 x=128..384、y=96..416；居中 0.75 缩放后四角最远点距中心 ≈154px，小于 maskable 安全区半径 0.4×512≈205px。声明 `purpose: "any maskable"`，同一对文件覆盖 any 与 maskable。
- 产物尺寸与内容边界必须在生成后实测并记录，不用推算值代替。
- 提交派生产物，不在构建或运行时下载。

## 6. 实现落点

| 文件 | 改动 |
|---|---|
| `src/frontend.py` | PWA 路径常量与 manifest 文档生成函数 |
| `src/main.py` | `routes()` 新增 4 条 GET/HEAD 路由（早于 catch-all）；`rewrite_device_html()` 新增 PWA 链接规范化与插入；`OFFLINE_PAGE` 增加 manifest 链接与内联注册 |
| `src/assets/pwa/*` | 图标字节、MIT 许可、来源与哈希记录 |
| `tests/test_v2_pwa.py` | 新增回归（见 §7.1） |
| `tests/test_v2_bootstrap.py` | 增加 standalone 冷启动归属回归（见 §7.1 第 8 项） |
| `docs/architecture.md` | 新增 PWA 章节（单应用 PWA、worker 边界、离线语义） |
| `docs/maintenance.md` | 追加实现状态、验证证据与未验证项（该文件当前含未提交的 Android 改动，只追加不覆盖） |
| `src/static_adapter.py` | **不改**；仅当实测发现 worker 脚本被 HTTP 缓存复用时才重新评估 |
| `android/**` | **不改、不删、不打包** |

## 7. 验证

### 7.1 自动化（pytest / Node）

1. `/sw.js` 返回 Mesh 脚本：含版本行、有 `install`/`activate`、**无** `fetch` 监听、无 `caches` 引用；提取后 `node --check` 通过。
2. 源码级与脚本级断言：Mesh 代码与 SW 脚本都不存在 CacheStorage 读写删路径。
3. 四条 PWA 路径不产生设备请求；无设备注册时仍返回 200。
4. manifest 字段断言：`id`/`start_url`/`scope` 为 `/`、`display: standalone`、含 192/512 图标、`purpose` 合法、`start_url` 无 query。
5. 图标字节存在、`Content-Type: image/png`、尺寸与 SHA-256 与记录一致。
6. HTML 规范化：既有 manifest 链接被替换为 credentialed 网关链接；缺失时插入；重复时去重；`/_mesh/` 路径不被二次改写；`rel="icon"`/`apple-touch-icon` 指向网关图标；**manifest 链接缺失不得抛错**。**2026-09-30 评审补充**：还须覆盖 `data-rel`/`x-rel`/`aria-rel` 不被误判、属性值内引号 `>` 时整标签删除无残渣、`<script>`/`<style>`/HTML 注释内的同文本不被改写、`rel` token 列表与大小写、截断文档不抛错，以及整条 `rewrite_device_html` 管线的字节级幂等。
7. 恢复页含 manifest 链接与 `/sw.js` 注册，仍为 `no-store`；manifest 与 `/sw.js` 无凭据访问为 401。**（2026-09-30 用户批准的边界调整后：两个图标路径的匿名 GET/HEAD 为 200，未认证写为 401、认证写为 405，六个变体路径仍 401，页面与 API 仍 401；`id` 固定为 `/_mesh/pwa` 且跨请求稳定、不随 manifest 路径或设备变化。）**
8. standalone 冷启动：预置指向 Device A 的 `opencode.pwa.last-route`、默认设备为 Device B，断言**最终路由与在途请求归属为 A**，切换前已发出的请求不被改投或重放。不要求"只协商一次"。**2026-09-30 评审补充**："在途"必须是切换时**尚未 settle** 的真实请求（先前版本先 await 完成再切换，不构成在途证据），并显式断言最终目标为 A。该用例只固定**适配器在 native app 恢复路由之后**的契约：路由移动通过 `history.replaceState` 模拟，未执行上游 `restorePwaRoute` 与 display-mode standalone 冷启动本身（需手机，见 V-6）。
9. 全量 `.venv/bin/python -m pytest -q -p no:cacheprovider -W error::DeprecationWarning -rs`、`git diff --check`。

### 7.2 隔离浏览器

- 全新 Chromium，`--user-data-dir` 置于忽略目录 `data/diagnostics/pwa-profile/`；不复用用户真实 profile，不清用户浏览器存储。
- **主证据：在隔离 profile 内走浏览器原生 Basic Auth 挑战**（弹窗输入凭据），这样 manifest 与 SW 脚本请求的凭据行为是真实行为。CDP `Network.setExtraHTTPHeaders` 强塞 `Authorization` 只能作辅助手段——它会掩盖 `crossorigin` 与凭据模式的差异。凭据不写入仓库、日志或文档。
- 观测点：
  - DevTools Application → Manifest（配合 CDP `Page.getAppManifest`）：manifest 200/401、解析错误、installability error。
  - `navigator.serviceWorker.getRegistration('/')`：`scriptURL === '/sw.js'`、`scope === '/'`、`active.scriptURL` 为 Mesh 脚本；网络面板确认 `/sw.js` 是否 200。
  - `await caches.keys()`（只在页面控制台读取，不在 SW 内）：确认旧 Workbox 缓存仍在（预期保留）且 Mesh 未新增任何缓存条目。
  - `/_mesh/**` 导航、设备会话发消息：确认无 SW 介入导致的回退。
- 真实安装、启动器图标、冷启动、离线冷启动属**用户手机验收**（V-6），隔离 profile 不能替代。

### 7.3 证据台账

**已核实**：§2.1 全部条目（线上 curl 层观测、上游 v2.0.18 源码、本仓库实现、规范条款、上游 MIT 许可与图标源哈希）。

**本地 loopback 浏览器已捕获**（2026-09-30，真实 Chromium + 隔离 profile + 临时 Gateway 无 Agent，范围与限制见 §2.3）：V-1 的注入侧、V-2、V-4 在此环境成立；**不外推到线上 HTTPS、真实设备页面与手机**。

**待验证**：

| 编号 | 待验项 | 影响 | 当前状态（2026-09-30） |
|---|---|---|---|
| V-1 | 注入 `crossorigin="use-credentials"` 后 manifest 在目标浏览器可获取；以及当前无该属性时线上是否真的取不到 | 决定 R-1 是否成立 | 注入侧已在本地 loopback 观测（manifest 200）；线上 HTTPS 的原始表现仍未观测 |
| V-2 | SW 脚本请求能否携带 Basic Auth 凭据（不 401） | 若不能，接管在部分浏览器失效；届时再决定是否豁免单个脚本（需用户批准） | 本地 Chromium 已观测为不 401；其他浏览器未验 |
| V-3 | Chrome 2026 自动安装提示是否仍要求 fetch handler | 只影响提示是否自动出现，不影响菜单安装与 standalone 价值 | 未验（installability errors 为空不等于自动安装提示已验证） |
| V-4 | 旧 worker 是否被及时替换、旧 precache 是否确实不再被使用 | 验证缺陷 3 处置有效 | 本地 fixture worker 已被同 URL 接管、旧缓存保留且无新增；线上未验 |
| V-5 | standalone 冷启动恢复 last-route 后的最终目标与在途归属 | 影响 PWA 冷启动体验 | **未验**：本轮浏览器无真实 2.0.18 页面与手机冷启动，仅有自动化回归 |
| V-6 | 真实安装、启动器图标、冷启动、网关不可达时的离线表现 | 决定是否可替代 Android 客户端 | 未验（用户手机验收；启动器图标未验证） |
| V-7 | 新 `id` `/_mesh/pwa` 与匿名图标在真实手机 Chrome 上是否让 WebAPK 拿到图标与稳定名称 | 决定这次边界调整是否真的解决启动器问题 | 已部署 `814fda2`（2026-09-30）并完成线上 HTTP 核验（匿名图标 200、匿名 manifest/SW/API 401、manifest 字段正确）；**真机 WebAPK 安装、启动器图标与冷启动仍未验证**，待用户手机验收 |

## 8. 风险与回滚

| 风险 | 缓解 |
|---|---|
| R-1 注入 `crossorigin` 前，manifest 可能本就取不到（未观测） | 注入后由 V-1 给出结论；文档不预判浏览器行为 |
| R-2 SW 脚本请求 401 → 接管失效，旧 worker 继续控制页面 | V-2 先测；回退需用户批准（单个固定脚本免认证），不接受"清空全部缓存"类方案 |
| R-3 脚本顶层抛错导致 install 失败 | 脚本极简无分支，`node --check` + 结构断言 |
| R-4 旧 precache 长期占用浏览器配额 | 记录为已知取舍：留存量由用户在浏览器清除站点数据时释放；Mesh 不做破坏性清理 |
| R-5 图标许可或 maskable 几何不合规 | MIT 通知随附；产物尺寸与内容边界实测记录；哈希固定 |
| R-6 恢复页重复注册造成控制台噪声 | `register()` 同 URL 幂等；仅在 `no-store` 恢复页出现 |

回滚：还原 Gateway 版本即可（`id`/`start_url`/`scope` 未变，已安装应用身份不变）。注意只撤 `/sw.js` 路由会让上游 Workbox worker 重新注册，因此回滚应整体回到本设计之前的 Gateway 版本。

## 9. 明确不做

不做离线业务壳、不做 SW 静态资源缓存、不做任何 CacheStorage 清理、不重放请求、不放宽业务认证、不改设备归属语义、不要求"只协商一次"或零预连接、不改 `android/`、不移动既有发布 tag、不为 PWA 让无关页面失败。
