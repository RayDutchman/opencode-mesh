# 维护、验证与会话交接

本文是新会话和维护者的工作入口。长期规则见根目录 [AGENTS.md](../AGENTS.md)，用户操作见 [README](../README.md)。

## 1. 先确认事实来源

### WebAPK 真机安装（2026-10-03，验证通过；站点侧无改动）

- **根因：手机所在网络访问不到 Google 的 WebAPK 铸造服务。** Chrome 在手机上直接向 Google 请求铸造参数，不经过本网关，因此这类失败在服务端不留日志、不报错、也不产生回退快捷方式。该网络下打开代理后，同一部手机立刻安装成功，站点代码一行未改。
- **使用前提（无法从服务端改善）**：安装要求手机网络能访问 Google 的 WebAPK 铸造服务。提前告知使用者即可。
- **站点侧无需为 WebAPK 改动**：Chrome 安装时不重取 manifest，沿用渲染器已解析的那份，所以 manifest 处于认证墙后无碍；图标匿名例外在失败与成功两次里都生效。此前的身份隔离与匿名图标两项调整本就正确，只是被这个外部条件掩盖了一整轮。
- **排查没有留下任何需要撤销的东西**：全程未部署诊断代码，也未放宽认证边界。
- 验证：用户的 Android 手机（最新版 Chrome）打开代理后安装成功。启动器名称与图标已确认稳定显示为 **OpenCode Mesh**（manifest 里的 `short_name`「Mesh」不被 WebAPK 采用，标签取 `name`，属正常行为）。仍未确认：standalone 冷启动、网关不可达时的离线表现。
- 可复用的教训：这类「无报错、服务端无痕」的失败，先查外部依赖的可达性，比在客户端日志里挖更快。

### 装成应用后输入框底部被裁掉（2026-10-03，已修复并真机验证）

- **根因是 Mesh 自己把 `#root` 的高度按整屏算，却让它从状态条下方开始。** 状态条占掉顶部 36px，`#root` 又是 `100dvh`，于是底部多出 36px 落在可见区域之外，而 `body{overflow:hidden}` 让这段既看不见也滚不到。发送键正好卡在里面，所以只有一半能点；开输入法后同样被切。
- **修复：用 `visualViewport.height` 决定高度，而不是任何 CSS 视口单位。** `#root` 由脚本按「可见高度 − 状态条高度」设内联高度，随键盘和旋转自动跟随。CSS 里的 `calc(100dvh - 36px)` 只作为脚本执行前的兜底。
- **为什么不能靠固定补偿量修：** 真机读数显示 `100dvh` 与实际可见高度会不一致（键盘一开就差一截），偏差量本身是变量，任何常数都修不准。
- **站点无法改善的一项：底部系统导航栏颜色（已结案）。** 装好后窗口高度等于系统可用高度、四个安全区 inset 全为 0，说明网页视口并未延伸到导航条下面——那一块由系统单独绘制成黑色，不受 `<meta name="theme-color">` 或 manifest 的 `theme_color`/`background_color` 控制（两者已写为 `#fafafa`）。对照：Chrome 自身应用的导航栏取页面背景色，因此会跟随主题。不要为此改 manifest。
- **未查清的一项：** 纯 CSS 版本（`calc(100dvh - 36px - env(safe-area-inset-bottom,0px))`）在真机上没有生效（读数等于 `100dvh` 原值）。原因未定位，不作推断；改为脚本后该声明不再承担正确性。排查期间的临时读数代码已删除。
- 验证：428 passed / 1 skipped；线上 revision 与本地 HEAD 一致、service active、21/21 项线上复核通过；真机确认输入框与发送键在键盘开、关两种状态下均完整可用，且 Chrome 浏览器内无回归。
- **不要写死只对某一台设备成立的数值。** 状态条高度 `BAR_HEIGHT_PX` 是 Mesh 自己 CSS 定的设计常量，状态条自身、app 兜底规则、可见高度脚本三处全部引用它；窗口相关的高度一律运行时量取。
- 可复用的教训：页面高度要用「用户真正看得见的那块」倒推。视口单位量的是布局视口，两者不等时按单位算出来的底部必然被裁掉。

### 装成应用后没有刷新入口（2026-10-03，改为点标题硬重载）

- **浏览器的下拉刷新在这个页面上不可用，不要再尝试打开它。** 实测两个各自独立的阻断点，都来自上游自身的 CSS：根滚动容器没有可超滚的余量（`scrollHeight == clientHeight`，因为是定高 SPA，滚动的是内部 `.scroll-view__viewport`），以及 `body` 上声明了 `overscroll-behavior-y:none`。此外 Chrome 只在根滚动容器上提供下拉刷新，不会从内部容器冒泡。
- **不要靠给 `body` 加 `overflow:auto` 来解锁它。** 那会给根造出滚动余量，但同时破坏上面的定高布局规则，正是本节上一条刚修好的东西。
- **刷新入口是状态条标题**：渲染为 `<button type="button">`，绑 `location.reload()`，保留产品名文字，抹掉按钮默认外观只留手型光标。
- **为什么不是下拉手势**：先做过一版横条下拉，完整实现要 130 行（阻尼、方向判定、四条触摸监听、点击守卫），其中约 20 行纯粹是为了吞掉「拖动落在设备按钮上仍会产生 click」。改成一个按钮只要约 17 行，误触保护靠「标题与设备按钮是两个独立按钮」而非手势长度，代价基本为零。手势唯一真正独占的优势是不会误触，不值这个代码量。
- **维护注意一**：`.ocm-title` 的样式里 `font:inherit` 必须排在 `font-weight:600` 前面。`font` 是简写属性，会把字重一起重置，顺序反了标题会静默变成非粗体。
- **维护注意二**：`.ocm-title` 必须有 `white-space:nowrap`。横条里传输状态文字是可变的，宽度不够时标题会先折行；两行标题有 40px，高过 36px 的横条，会压到下方内容上。**这是本次顺带发现的既存问题**：把标题换回 `<span>` 测得完全相同的折行，所以不是改成 button 带来的，而是窄横条加长传输文案时一直存在。已一并修掉。
- **已知代价（有意接受）**：标题没有可见的刷新图标，桌面端靠 `title` 提示，手机端只能靠摸索；标题紧邻设备按钮，两者之间没有分隔，误触标题会丢掉未发送的输入。硬重载不保留草稿。
- 验证：`test_bar_title_reloads_the_page` 与 `test_bar_title_keeps_the_plain_text_look`；15 项变异（删绑定、删重载调用、标题退回 span、删 type/title/aria-label、把绑定挪到设备按钮、`font:inherit` 顺序反了、逐个删掉按钮样式重置、删 `white-space:nowrap`、删焦点环）全部被测试抓到。真实 Chromium 里另外核过：标题单行、高度不超过横条、不与设备按钮重叠、真实点击确实整页重载、点设备按钮不会重载。全量 **430 passed / 1 skipped**（skip 恒为缺 API-35 `android.jar`）。

### 网关级 PWA（2026-09-30，实现 `2f5e845` + `814fda2`；09-30 曾部署、10-01 回退、**10-03 随合并 `91cd283` 重新部署并在线**）

- 身份隔离与匿名图标（2026-09-30，用户书面批准，提交 `814fda2`，当时已提交并部署；**该部署已于 10-01 回退，下面是历史记录**）：
  - manifest `id` 由 `/` 改为固定 `/_mesh/pwa`，`start_url` 与 `scope` 仍是 `/`。依据：浏览器按「源 + id」识别已安装应用，同源旧原生应用已占用 `id` `/`，复用会让两次安装合并成一个启动器条目，名称与图标随最后一次写入而变（手机 Chrome 实测名称在 Mesh/OpenCode 间切换）。`id` 只是身份标识，不要求可导航。
  - 认证中间件新增唯一匿名例外：**精确匹配** `/_mesh/pwa/icon-192.png` 与 `/_mesh/pwa/icon-512.png` 的 GET/HEAD。依据：Chrome 生成 WebAPK 时以 `CredentialsMode::kOmit` 取图标，带 Basic Auth 挑战会丢掉启动器图标；研究同时确认存在 bitmap fallback，因此这是兼容风险处置，**不是已证实的手机根因**。manifest、`/sw.js`、页面、API 及其他方法仍走认证：未认证写图标 401，认证写图标 405；变体路径（尾斜杠、大小写、未提供尺寸、子目录、后缀、编码斜杠）一律 401，白名单不可扩展为前缀。
  - 未改动旧 manifest 代理路由、未清任何缓存、未卸载旧应用、未改 Android。
- 部署核验（2026-09-30，提交 `814fda2`，主 agent 独立执行）：远端 revision 为 `814fda2c473ab6329c92c63b540a0f68cf03ac5c`，`opencode-mesh-gateway.service` active。线上 HTTPS 实测：匿名 GET/HEAD `/_mesh/pwa/icon-192.png` → 200 `image/png` 628 字节、`/_mesh/pwa/icon-512.png` → 200 1964 字节；匿名 POST 两个图标均 401；匿名 GET `/_mesh/pwa/manifest.webmanifest`、`/sw.js`、`/_mesh/devices` 均 401；已认证 manifest 为 `id=/_mesh/pwa`、`start_url=/`、`scope=/`、`name=OpenCode Mesh`；已认证 GET `/` → 200 且含新的 manifest 链接；已认证 POST 图标 → 405。仅部署 Gateway，未重启任何 Agent、未部署 PVE、未重打包 APK；版本仍为 0.3.2，tag `91de7aa` 未动。以上均为 HTTP 层核验，**不等同浏览器安装通过**。
- 前次部署（2026-09-30，提交 `2f5e845`）：已合入原工作分支、推送 `origin/main` 并仅部署 Gateway；服务 active 与完整 `.mesh-revision` 核对通过。线上 HTTPS 的 manifest、192/512 图标与 `/sw.js` 均返回 200 且图标/SW 与本地字节一致，首页及恢复页包含 credentialed manifest；**该版本匿名 manifest/SW/图标均为 401**，上述匿名图标例外由 `814fda2` 引入。未重启 Agent、未改 OpenCode 认证、未重打包或删除 Android。
- 目标：恢复 Mesh 网页 PWA，同时不引入离线缓存。manifest 与 Service Worker 由 Gateway 提供，不依赖在线 Agent；所有导航与业务请求不经 Service Worker、不被重放；Mesh 代码不读写 CacheStorage，也不枚举删除任何旧缓存。
- 实现（分支 `feat/mesh-pwa`，worktree `.worktrees/mesh-pwa`，基线 `8b87e95`；曾合入运行分支并先后部署 `2f5e845` 与 `814fda2`，**该两次部署均已回退，当前不在线上**）：
  - `src/frontend.py` 新增 PWA 路径常量、`pwa_manifest_document()`、`pwa_service_worker_source(version)`、`load_pwa_icons()`、`normalize_pwa_links()`。`src/main.py` 在设备 catch-all 之前注册 `/sw.js`、`/_mesh/pwa/manifest.webmanifest`、`/_mesh/pwa/icon-192.png`、`/_mesh/pwa/icon-512.png`：仅 GET/HEAD，其他方法 405（避免落到设备转发），`Cache-Control: no-cache`，`/sw.js` 附 `Service-Worker-Allowed: /`，脚本内嵌 `src.__version__`；图标缺失时 `Gateway.__init__` 直接报错。
  - Service Worker 脚本只有 `install`（`skipWaiting`）与 `activate`（`clients.claim`），无 `fetch` 处理函数、无 `respondWith`、无 `importScripts`、无 `caches`。旧 Workbox precache 保留但不再使用，登记为风险 R-4：Mesh 不清理它。
  - `rewrite_device_html()` 先删除所有 `rel="manifest"`/`rel="icon"`/`rel="apple-touch-icon"` 链接再在 `</head>` 前插入唯一一组网关链接，manifest 带 `crossorigin="use-credentials"`（Basic Auth 需要凭据随请求发送）；缺标签或无 `</head>` 的片段只追加不报错；规范化幂等。`og:image` 保持设备路径。`OFFLINE_PAGE` 带同一组链接并内联注册 `/sw.js`，仍为 `no-store`。
  - 规范化判定用 stdlib `html.parser.HTMLParser`，不用正则（2026-09-30 评审修订）：`rel` 按空格分 token，只有真实 `rel` 属性命中才算 PWA 链接，`data-rel`/`x-rel`/`aria-rel` 不再误删整条 stylesheet/preload 标签；标签范围取解析器给出的原文，引号内的 `>` 不再截断标签、`<script>`/`<style>`/HTML 注释里的同文本不再被改写；插入点取解析出的真实 `</head>` 起始偏移。文档按 latin-1 解码后只做字节拼接，编辑区间之外的字节逐字保留。实测 500 字节应用壳单次 0.058 ms，1.4 MB 文档 163 ms（线性开销，真实路径是应用壳导航）。
  - `pyproject.toml` 增加 `[tool.setuptools.package-data]`，`pip wheel` 已验证 wheel 内含 `src/assets/pwa/*`。
- 资产：`src/assets/pwa/icon-192.png`、`icon-512.png` 由上游 `packages/ui/src/assets/favicon/favicon-v3.svg` @ `anomalyco/opencode` **v2.0.18**（仓库根 LICENSE 为 **MIT，Copyright (c) 2025 opencode**，原文以 `LICENSE-OpenCode.txt` 随包分发）派生：logo 组 `translate(64 64) scale(0.75)` 居中以满足 maskable 安全区，`rsvg-convert` 2.61.3 栅格化。源文件、生成命令、实测内容边界与 SHA-256 记录在 `src/assets/pwa/README.md`，并由 `tests/test_v2_pwa.py` 固定哈希与 PNG 几何。
- 验证：全量 pytest **380 passed, 1 skipped**（基线 356 + 新增 24，跳过项为 Android API-35 工具链缺失）。新增 `tests/test_v2_pwa.py`（23 项）覆盖脚本结构与 `node --check`、无 CacheStorage 路径、PWA 请求不进设备、manifest 字段与 `id` 稳定性、图标哈希与 PNG 几何、maskable 安全区（自带 zlib 解码，不新增依赖，实测 bbox/半径与生成时的 PIL 读数一致）、HTML 替换/插入/去重、恢复页包含项、图标由 Gateway 构造时载入、认证方法矩阵（图标匿名 GET/HEAD 200 与带 query 200、未认证写 401、认证写 405、manifest/SW/页面/API 仍 401、六个变体路径 401），以及评审后新增的 6 项 HTML 规范化边界（`data-rel`/`x-rel`/`aria-rel` 不误删、引号内 `>` 整标签删除无残渣、`script`/`style`/注释内文本不变、`rel` token 列表与大小写、截断文档不抛错、整条 `rewrite_device_html` 管线字节级幂等）。`tests/test_v2_bootstrap.py` 的 last-route 用例已重写并改名 `test_restored_last_route_owns_the_page_while_a_pending_request_stays_with_its_device`：预置指向 Device A 的 `opencode.pwa.last-route`、默认设备为 Device B，**切换时该请求尚未 settle**（真实在途），断言它只发一次、留在 B、不被 resolve 或取消，并显式断言切换后最终目标为 A；另断言切换不改投默认 Server 的裸路径请求、已带设备前缀的 URL 不被二次改写。四个反向变异（在途即 settle / 不恢复路由 / 切换后重放 / 发出时即错设备）均如期失败，断言非空。边界调整另有七个反向变异全部如期失败：`id` 取 manifest 路径、`id` 回到 `/`、白名单改前缀 `/_mesh/pwa/`、白名单改前缀 `/_mesh/`、写方法也匿名、manifest 也匿名。`compileall`、`node --check`（适配器与 `/sw.js`）、`pip wheel` 含 `src/assets/pwa/*`、`git diff --check` 通过。
- 浏览器验收（2026-09-30，独立探针 `/tmp/opencode/mesh-pwa-probe/run_probe.py --worktree <本worktree>`，PASS）：**范围为本地 loopback + 真实 Chromium + 隔离 profile + 临时 Gateway（无 Agent）+ 浏览器原生 Basic Auth 挑战**，未使用 `Network.setExtraHTTPHeaders` 塞 `Authorization`。
  - 已观测：恢复页 200 且该导航**不是**由 Service Worker 应答；manifest 200，HTML 内为 `crossorigin="use-credentials"` 的网关链接，CDP manifest 无解析错误、installability errors 为空；`/sw.js` 以同 URL 接管旧 fixture worker、根 scope `/` 安装与激活完成且未 401；旧 CacheStorage 条目与 localStorage sentinel 完整保留、Mesh 未新增任何缓存条目；`icon-192/512` 网络层 200（628/1964 字节）。
  - 由此得到的结论：V-1 的注入侧、V-2、V-4 在**本环境**成立（V-2 不 401 → R-2 在此不成立，因此暂不需要"单个脚本免认证"回退）。
  - 不能据此宣称：headless 下 CDP `Page.getManifestIcons` 的 optional `primaryIcon` 为空，既不证明图标失败，也**不构成启动器图标已验证**；本轮无真实 OpenCode 2.0.18 设备页面（观察到的是恢复页），因此**设备 HTML 规范化的浏览器侧行为、真实设备会话与业务链路未在浏览器验证**。
- 仍为未验证（不得当作通过）：线上 HTTPS 下缺 `crossorigin` 的原始表现（V-1 另一半）、Chrome 2026 自动安装提示（V-3）、**手机冷启动 last-route 的最终目标与在途归属（V-5）**、真实安装与启动器图标与冷启动（V-6，用户手机验收）。Android 手机与线上环境均未验证，验收 V-6 前不评估是否可移除 Android 客户端。
- 边界调整的验证状态：新 `id` 与匿名图标已有单元/ASGI 层证据（`tests/test_v2_pwa.py`）与线上 HTTP 层证据（`814fda2` 部署核验，见上），但 **V-7 真机 WebAPK 安装仍未验证**——`2f5e845` 时代手机反馈「安装后无桌面图标、名称在 Mesh/OpenCode 间切换」，重装后是否出现独立图标、名称是否稳定为 Mesh、standalone 冷启动与业务体验，全部待用户手机验收；本次 HTTP 核验与旧的浏览器 PASS 记录（对应 `2f5e845` 之前的代码）都不能替代真机安装证据。旧应用不卸载、不清缓存，重装后预期为两个独立启动器条目。
- 保留边界（用户已明确接受）：网关不可达冷启动显示浏览器自身离线错误页，Mesh 无离线壳；网关可达但设备不可用时走既有恢复页。
- 收尾复验：实现子会话限流后，主 agent 核对已保存的审查修正，独立运行全套得到 **377 passed / 1 skipped**，隔离 Chromium 探针再次 PASS，diff 检查通过。当前 OpenCode 上游 401 故障独立于本轮 PWA；未修改生产认证配置或重启生产服务。

### 离线设备的浏览器本地 503 门禁（2026-10-02，已实现并部署 `50120ec` → 修范围缺陷 `f45a964`；线上浏览器端到端已验证）

- 起因：VPS 访问日志观测到，页面停在已离线设备时上游 OpenCode V2 SDK 仍以约 1.2s 固定节奏重试 `/api/event`，单个离线设备约 3018 次/小时；这些请求此前由适配层原样转发，Gateway 每次都立即返回 503。每 10s 一次的 `/api/info` RTT 探测属 Mesh 内部流量，不在本门禁范围。
- 实现（`src/static_adapter.py`）：`window.fetch` 在任何传输选择之前调用 `offlineDeviceRefusal(url, input, init)`。设备归属复用 `scopeNativeRequest` 的判据（显式 `/_mesh/device/{id}` 前缀优先，裸路径绑定 `state.defaultDevice`，`/server/` 与非设备 `/_mesh/` 路径不解析设备），不新增第二套 URL 解释。
- 判据必须比 Gateway 保守：同源、`Accept` 不含 `text/html`、快照中存在该设备且 `online === false`，并且最近一次成功 `/_mesh/devices`（5s 轮询）不超过 `DEVICE_SNAPSHOT_TTL_MS = 15000`（容忍三次漏轮）。`setDeviceStatusUnknown()` 把 `state.deviceSnapshotAt` 置空，因此发现失败或 10s 内未返回都会作废快照；`refreshDeviceStatus()` 与 `syncNativeServers()` 在每次成功发现后写入时间戳。
- 应答与 `Gateway.offline_response` 语义一致：503、`content-type: application/json`、正文 `{"error":"Specified device offline or not found","device_id":"<id>"}`（测试用真实 `Gateway` 实例产出服务端应答，逐字节比对本地应答）。方法不限——服务端对任何方法都这样回答，本地只是复现，不另立「哪些请求可以走」策略；因为什么都没发出，「已发 mutation 不重放」不受影响。
- 导航例外：`Accept` 含 `text/html` 的请求一律放行，由 Gateway 返回离线恢复页，判据与 `Gateway.wants_html` 一致。接受头从 `init.headers` 和 `Request` 输入两处读取；读不出来时按导航处理（多发一次请求只是浪费，错误拒绝会顶掉恢复页）。
- 未改动：`src/main.py`、Gateway 路由、离线页、P2P/Relay 选择、认证、`window.WebSocket`、`scripts/`、`android/`、PWA、`reconnect_seconds`。
- 已知边界（已在架构文档登记）：Mesh RTT 探测与 `/api/info` 探测直接用 `nativeFetch`，不在门禁内；`/server/...`、控制面、跨源请求永远走既有路径。
- 实现约束：`tests/test_v2_bootstrap.py` 与 `tests/test_v2_transport.py` 会截取适配器源码片段单独求值，因此写入快照时间戳的两处必须就地内联，不能抽 `applyDeviceSnapshot()` 之类的辅助函数，否则片段内 `ReferenceError`。

**第一版范围缺陷与线上证据（`50120ec`，已部署）**

- 部署：主 agent 已推送 `fix/offline-device-request-gate` 并仅部署 VPS Gateway，线上 HTML 确认包含门禁代码；适配器在页面加载时注入，必须刷新所有标签页才生效。
- 刷新后的实测（主 agent 取证）：12:44:41 与 12:44:42 两次 `GET /` 都在 12:44:21 网关重启之后，确认浏览器拿到了新适配器；`/_mesh/devices` 每 5 秒正常轮询、`/api/info` 正常探测，但 `GET /_mesh/device/<另一台已注册设备>/api/event` 仍以约 50 次/分钟持续返回 503。
- 结论：第一版门禁**范围不足**，是实现缺陷而非部署失败。`window.fetch` 里既有的直通判定 `virtualDeviceId(url.pathname) !== state.manifest?.device_id` 会在「请求设备 ≠ 页面设备」时直接 `return nativeFetch(...)`，而门禁排在其后，因此永不执行。用户的页面同时订阅多台设备（页面自己是一台，另一台是已注册 Server 的事件流），这是上游 OpenCode 的正常用法；只有页面自己的设备被覆盖。
- 修正（本轮）：门禁移到直通判定之前，改为按显式 `/_mesh/device/<id>` 前缀逐台判断，不再要求等于 `state.manifest.device_id`；导航请求按上文例外放行。跨源、`/_mesh/` 非 device 路径、`/server/...` 的排除逻辑因此从「直通判定提供」改为「门禁自身提供」，不再依赖拦截位置。
- 验证：全量 pytest **400 passed, 1 skipped**（基线 397 + 3；`tests/test_v2_offline_gate.py` 14 项）。RED 阶段 4 项失败，分别是：他机离线设备仍返回 200（两处）、导航被本地拒绝（两处）；其余 10 项既有测试全通过，说明新期望不与既有行为冲突。反向变异全部如期失败：删除 Accept 排除（两项导航测试）、把门禁移回直通判定之后（3 项他机断言）、删除门禁内的同源检查（跨源）、删除 `requestDeviceId` 的 `/_mesh/`+`/server/` 守卫（控制面）、Accept 只读 `init.headers`（`Request` 输入的导航测试）。
- 第二轮部署与**线上浏览器端到端验证**（2026-10-02，提交 `f45a964`，主 agent 独立执行）：仅部署 VPS Gateway，远端 revision 与 `.mesh-revision` 一致、服务 active、线上 HTML 含新门禁代码。随后用隔离 Chromium（独立 profile，浏览器原生 Basic Auth）直接访问线上 `https://mesh.example.com/`：页面自身设备为 `device-a`，快照中 Device B（`device-b`）为 `online === false`；令页面执行 `fetch('/_mesh/device/device-b/api/event', {headers:{accept:'text/event-stream'}})` 得到**本地 503、`content-type: application/json`、正文与 `Gateway.offline_response` 一致，且该请求未产生任何网络请求**；同一离线设备的 HTML 导航（`accept: text/html`）返回 **200 `text/html`** 并确实发往服务器，即恢复页未被本地拒绝。两者合起来证明「他机离线设备被本地拦截」与「导航例外」在真实浏览器中成立。脚本 `/tmp/opencode/livegate/check.py`，未纳入仓库。
- **未验证**：用户浏览器中已加载的旧适配器仍会继续发出该噪声，**必须刷新页面**才换上新适配器；「刷新后 VPS 上该设备的 `/api/event` 是否归零」尚待日志核对。另外仅验证了该 JSON/HTML 两例，未穷举其他方法与其他端点。

### 网关访问日志降噪（2026-10-01，已实现并部署 `1c56330`，主机侧限额另行处理）

- 起因：VPS 上 24 小时内约 165k 行日志中 158k 行来自 `opencode-mesh-gateway.service` 的 uvicorn 访问日志，journald 已限制 200M。噪声主体不是「每请求一行」，而是适配层每 5 秒一次的 `/_mesh/devices` 刷新、P2P 未连通时每 10 秒一次的 RTT 探测、离线页 3 秒轮询，全部是 200；`GET /_mesh/devices HTTP/1.1" 200 OK` 占绝大多数。
- 结论先行：**uvicorn 没有官方的「只留错误」开关**。`access_log=False` 会清空访问通道全部 handler（`uvicorn/config.py` `configure_logging`），`log_level` 同时调整 `uvicorn.error`/`uvicorn.access`/`uvicorn.asgi` 三者，连启动行一起压掉；`UVICORN_ACCESS_LOG`、`UVICORN_LOG_LEVEL` 环境变量对程序内 `uvicorn.run()` 无效，因为 `auto_envvar_prefix` 是 click CLI 特性。
- 实现（`src/main.py`）：`AccessStatusFilter` 只在 `Gateway.lifespan` 内装到 `uvicorn.access` 的 handler，**不替换 uvicorn 的 `LOGGING_CONFIG`**——整体替换会连 `uvicorn.error` 一起覆盖，是本项最大的实现陷阱。状态码取自 `record.args` 末位（`AccessFormatter` 解包为 `client_addr, method, full_path, http_version, status_code`）。WebSocket `[accepted]` 行只有两个参数、不含状态码，无法解析时一律保留，宁可多留也不静默丢错。
- 配置键沿用扁平 snake_case + `cfg.get(键, 默认)` 风格，未引入嵌套 `mesh.log.*`：`access_log`（默认 `true`）、`access_log_status_min`（**默认未设置**）、`log_level`（默认 `info`）。`access_log_status_min` 缺省即保持全量访问日志，因此**升级不改变任何现有部署的日志形态**，降噪必须由用户显式配置。
- `log_level` 必须校验：uvicorn 自身是 `LOG_LEVELS[self.log_level.lower()]` 索引，未知值抛 `KeyError` 会在启动时崩。校验后非法值回退 `info`（含大小写与空白归一）。
- 入口重构：`uvicorn.run()` 参数改由 `gateway_server_options(cfg)` 推导，`main()` 只剩 `uvicorn.run(Gateway(cfg).app, **gateway_server_options(cfg))`；`host`/`port`/`timeout_graceful_shutdown=5`/`ws_max_size` 取值与原先硬编码一致，有测试锁定。
- 未改动：`scripts/install.sh` 的 systemd unit（`upgrade.sh` 只替换 `src`/`scripts`/`pyproject.toml`，永不重写 unit，加 `EnvironmentFile` 不会随升级生效）、`uninstall.sh`、PWA 与 `android/`。`reconnect_seconds` 死键未动。
- 验证：全量 pytest **410 passed, 1 skipped**（基线 380 + 新增 30，跳过项为 Android API-35 工具链缺失）。新增 `tests/test_gateway_logging.py` 30 项，覆盖 200/101/302 拒绝与 400/401/404/500/503 放行、无状态码记录保留、`uvicorn.error` 的 handler 未被装 filter、缺省配置下 200 仍完整记录、重复 lifespan 不叠加 filter、配置键到 `uvicorn.run` 实际参数的映射（真实 `Gateway` + monkeypatch `uvicorn.run`，不启动服务）、`log_level` 六个合法值与八类非法值回退。四个反向变异全部如期失败：`>=` 改 `>`、把 filter 装到 `uvicorn.error`、`resolve_log_level` 不做校验、lifespan 不装 filter。RED 阶段 29 failed / 1 passed，唯一通过项是「缺省配置保持原有全量访问日志」这一反向守卫。
- 真实进程证据（`/tmp/opencode/livecheck/`，真实 uvicorn Server + 真实 Gateway，非 ASGI 直驱）：`access_log_status_min=400` 时两次 `GET /_mesh/devices` 200 与未知路由 503 中，只剩 500/503 与全部启动关闭行；不配置时两次 200 照旧输出；`access_log=false` 时访问行全无但 `Exception in ASGI application` 仍在。uvicorn 的 handler 由 `Config.configure_logging()` 安装，因此只能在真实 stdout 上观察，测试里改用隔离 logger。
- 部署（2026-10-01，提交 `1c56330`，主 agent 独立执行）：已推送 `origin/main` 并仅部署 VPS Gateway；远端 revision 与 `.mesh-revision` 核对一致、服务 active，首页与 `/_mesh/devices` 均 200。**VPS 的 `gateway.json` 未添加任何新键**，因此线上访问日志形态与部署前一致——本轮只交付了可配置能力，降噪需显式配置。未重启任何 Agent、未部署 PVE、未重打包 APK；版本仍 `0.3.2`。
- 主机侧 journald 限额（2026-10-01，非仓库改动）：VPS 的 `/etc/systemd/journald.conf` 原本只有一个空的 `[Journal]` 段，没有任何保留上限，日志只增不减（当时 `/var/log/journal` 已达约 1.2G、24 小时内 96% 条目来自 gateway）。已新增 `/etc/systemd/journald.conf.d/mesh-limits.conf`（`SystemMaxUse=200M`、`SystemKeepFree=512M`）并重启 `systemd-journald`，把占用从 1.2G 降到约 181M；原 `journald.conf` 未改动。这属于主机配置，**不会随 `upgrade.sh` 部署，仓库不代改**，重装主机后需重建该 drop-in。
- **未验证**：其他 uvicorn 版本下 `record.args` 结构未测（本项依赖该内部约定，上游升级需真实浏览器复验）；`access_log_status_min` 上线后的实际下降幅度未实测（因为 VPS 尚未配置该键）；未做 systemd `LogRateLimit*` 对比实测（属 Mesh 之外的方案，未采纳——按 service 丢消息会连错误和启动行一起丢）。
- 下一步：如要降噪，在 VPS `gateway.json` 加 `access_log_status_min: 400` 并重启 gateway，然后核对 `/api/event` 等 503 流量是否仍占多数（状态码过滤不区分响应体，**已知 503 不会被过滤掉**，需另见离线设备本地门禁一条）；不满意直接删除该键即回到全量。

### 未知健康状态允许验证连接（2026-09-29，已部署本机与 VPS）

- 用户确认：Agent 在线但健康 unknown（旧 Mesh 不上报、报告过期等）保持灰色，允许手动点击并由实际 `/api/info` 验证 OpenCode V2；明确失败与 Agent 离线继续不可选。
- `available` 保留已确认健康的含义；尝试连接资格独立判断。验证失败保持原目标，不静默改投；恢复页仅已确认 available 才自动刷新，避免 unknown 循环刷新。
- 已用公开接口核实未更新设备健康 unknown 时 `/api/info` 仍返回 200、OpenCode 2.0.18，说明缺少健康报告并非业务不可用。
- 运行提交 `1c31e102dd35a4722171e593ac056c022b8309e7` 已推送并仅部署本机两个 Agent 与 VPS Gateway，服务 active、revision 核对通过。线上 HTML 与恢复页已包含新选择逻辑，旧设备 unknown 的 `/api/info` 仍为 200/V2。未部署远程 Agent/PVE、未重打包 APK；实际浏览器点击验收留待用户。
- 回归验证：基线适配器会拒绝可用旧 Agent 的 unknown handoff，新实现通过；全套 pytest **356 passed, 1 skipped**（Android API-35 工具链缺失）。覆盖缺字段/过期 unknown 灰色可选、V2 验证成功进入、失败保持目标以及恢复页不自动刷新未知设备。评审发现自动默认选择也需验证 unknown 目标，已用红绿测试补齐；验证失败继续原目标重试，不改投其他设备。

### 上游 OpenCode 健康探测（2026-09-29，已部署，用户浏览器验收通过）

- 目标：区分 Mesh Agent 控制在线与本机 OpenCode 可用；保留原设备、身份及业务传输，不因探测失败重放请求或关闭控制连接。
- Agent 独立探测 `/api/info`，完成后等待 5 秒，单次总期限 2 秒；连续两次失败后标记不可用，一次成功恢复。结果变化与心跳均携带健康状态及单调时钟年龄，Gateway 以 30 秒新鲜度计算 `upstream_health`、`available`；`online` 仍指控制连接。健康数据不持久化。
- 菜单和顶部状态定期刷新；离线页区分 Agent offline、OpenCode unavailable/authentication failed、unknown。页面入口遇到新鲜明确失败时返回原目标恢复页；首页手动 handoff 选择页面来源，API/POST 转发保持原语义。
- 回归覆盖探测认证、错误分类、两失败一恢复、总超时、健康字段类型、age、持久化隔离、控制 ping/hello 清理、HTML 入口与跨设备 handoff。修正了旧测试把第一次超时当成最终不可用的错误预期，以及测试协程尚未启动便取消时误要求执行 finally 的问题。
- 浏览器人工验收待用户操作：先停止/恢复 OpenCode（Agent 保持运行），再停止/恢复对应 Agent，分开观察状态与恢复。本轮尚未主动停服进行故障复现；Android APK 不重打包。
- 验证：全套 pytest **352 passed, 1 skipped**；跳过项为 Android API-35 `android.jar` 缺失。JavaScript 语法与 `git diff --check` 通过；部署后再记录服务和健康 API 证据，单元测试不等于浏览器停服验收。
- 部署：运行提交 `6fb3d702e16dde14321c998a1515c0c984c10f30` 已推送并部署至 Gateway、远程 Agent 及本机两个 Agent；revision 与 active 独立核对通过。三个已更新 Agent 的公开健康为 healthy/available，业务 `/api/info` 均 200 JSON；未更新 Agent 的控制在线但健康 unknown/available=false，符合旧 Agent 契约。未执行真实停服测试，等待用户浏览器配合；版本仍为 0.3.2，未移动发布标签。
- 后续用户已自行完成 OpenCode 下线/恢复、Agent 下线/恢复的浏览器测试，报告结果符合预期；按其反馈移除恢复页可选设备名称的下划线，点击选择行为保留。

| 问题 | 权威来源 |
|---|---|
| 当前工作区、改动和提交 | `git status`、`git log`、实际 diff |
| 产品版本 | `src/__init__.py` 的 `__version__` |
| 发布变化 | [CHANGELOG](../CHANGELOG.md) 与对应 Git tag |
| 当前架构与职责 | [architecture.md](architecture.md) |
| 消息、错误和取消契约 | [protocol.md](protocol.md) 与实现、行为测试相互核对 |
| 某次历史验证 | 标明日期和版本的 `superpowers/plans/` 记录 |
| 某部署实际运行版本 | 目标安装目录的 `.mesh-revision`、服务状态与联网验证 |

历史计划保留当时的假设、失败尝试和验收结果；过时方案不得覆盖当前规则。旧能力矩阵、路由目录也是历史资料。若当前文档与实现冲突，先复现并记录差异，而非静默选择其中一方。

## 2. 维护范围与模块地图

同机多实例使用 `config/agents.json` 和 `--instance`；内部身份文件由程序派生，不手工填写。运维脚本仅有 `install.sh`、`uninstall.sh`、`upgrade.sh`。旧配置仍可运行，调整为统一配置时核对有效配置和身份，区分 daemon-reload 与真正重启；仅安装实例必须 disabled/inactive 且尚无注册副作用。生命周期回归见 `test_agent_instances.py`、`test_instance_install.py`、`test_instance_release.py`，认证边界见 `test_auth_boundaries.py`。代码是否已发布以 Git 和部署 revision 为准。

当前维护 OpenCode V2；最近发布验收使用上游 **2.0.6**。这不是对所有未来 V2 版本的兼容承诺。产品版本从源码读取，不在交接入口重复维护。

| 文件 | 责任 |
|---|---|
| `src/main.py` | Gateway/Agent 编排、认证、设备路由、HTTP/SSE/WS 代理 |
| `src/p2p.py` | WebRTC 接入、分片、大小与装配预算、背压和清理 |
| `src/static_adapter.py` | 浏览器 URL/fetch/WS 适配、设备发现、通道选择及状态栏 |
| `src/frontend.py` | 集中启动契约适配、静态资源命名空间、旧书签迁移 |
| `android/` | 内置固定版 OpenCode 前端的 Android WebView 预览客户端；构建、安装与验收限制见 `android/README.md` |
| `tests/test_mesh_reliability.py` | 代理、分片及生命周期回归 |
| `tests/test_v2_*.py` | 实际 Node 浏览器接口行为及 V2 错误、启动、上传、WS 边界 |
| `tests/test_v2_offline_page.py` | 统一在线判据（列表/路由/P2P/浏览器 ws gate）、离线页行为与轮询失败/永不返回恢复（ASGI 直驱 websocket + Node 真实页面脚本） |
| `tests/test_v2_offline_gate.py` | 离线设备请求的浏览器本地 503 复现（含他机显式目标）、导航例外、快照新鲜度预算与方法规则、控制面/跨源/`/server/` 不被门禁 |
| `tests/test_gateway_logging.py` | 访问日志状态码过滤、`uvicorn.error` 通道不受影响、缺省行为不变、配置键到 `uvicorn.run` 参数的映射与 `log_level` 回退 |

原生 OpenCode 管理 Server 名称、项目、会话和终端 UI；Mesh 仍注入适配脚本与状态栏。当前活动设备使用一条 P2P 通道，其他 Server 请求按明确地址走 Relay。

### 关键决定与已排除的错误方向

- Relay 空 400 曾由解码后保留 `Transfer-Encoding`、同时生成 `Content-Length` 引起。解决点是 HTTP 分帧头，不是补 `{}` 或改写模型字段。
- 绝对 `/api/...` URL 会丢失路径型 Server 基址；在 URL 构造时保留设备作用域，不能用当前页面选择重路由所有后台请求。
- Gateway origin 不作为额外业务 Server；设备显示名、默认选择和稳定身份是不同概念。
- 默认设备短时不可达时保持旧项目的 canonical 归属；首次默认选择可以选择在线设备，显式用户选择保持。
- 上传超限回 Relay 要保留已读前缀和剩余字节；不依赖全量 `arrayBuffer()` 探测，也不自动重放已发 mutation。
- 离线设备的 503 由 Gateway 和适配层两处「一致复现」，不是两套策略：判据比服务端保守（只认 15s 内快照里的显式 `online === false`，且导航请求放行），宁可放行也不误判可达设备、也不顶掉离线恢复页；任何方法都被应答，因为服务端对任何方法都这样回答。覆盖范围按显式设备前缀逐台判断：页面订阅多台设备时每台离线设备都有独立重试循环，只挡页面自己那一台是不够的。

## 3. 开发与检查

需要 Python 3.11+ 和 PATH 中可用的 Node.js；Node 用于执行浏览器接口行为测试。新环境建立项目虚拟环境：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e . pytest
.venv/bin/python -m pytest tests/ -q -W error::DeprecationWarning
.venv/bin/python -m compileall -q src tests
git diff --check
```

适配器独立语法检查（生成物放在项目忽略目录）：

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
from src.static_adapter import TRANSPORT_ADAPTER
target = Path('data/diagnostics/adapter-check.js')
target.parent.mkdir(parents=True, exist_ok=True)
script = TRANSPORT_ADAPTER.split('<script id="ocm-transport-adapter">', 1)[1].split('</script>', 1)[0]
target.write_text(script.replace('__OCM_VERSION_JSON__', '"check"'))
PY
node --check data/diagnostics/adapter-check.js
```

文档整理需校验相对链接、示例脱敏、历史状态标记及 diff 范围；修改测试夹具后运行测试。对源码行为变更，以能复现问题的测试为依据，避免仅检查源码字符串。

## 4. 浏览器验收与证据

UI 或传输行为变更应按受影响范围检查：

1. 新浏览器的 Server 列表没有 Gateway 别名；已有用户名称和外部 Server 保留。
2. Device A/Device B 切换后，会话、请求地址和终端主机归属一致。
3. P2P 与强制 Relay 各发送一次带唯一标记的测试消息，确认本次助手回复，避免匹配旧记录。
4. PTY 创建、connect-token、WS 连接、文本/二进制帧及终端输入输出均验证。
5. 关闭 P2P 后，后续请求可回 Relay；对结果未知的在途 mutation 不做自动重放。
6. 启动发现失败可恢复；取消、关闭及弱网重连有对应生命周期证据。
7. 页面设备离线时请求立即得到 503（页面内报错而不是长时间等待）；设备恢复后 ≤5s 自动恢复，无需刷新页面。订阅的其他离线设备不再持续产生 503；在浏览器里打开离线设备的地址仍显示 Gateway 恢复页，而不是裸 JSON。

浏览器自动化不是当前 pytest 的一部分。正式回归测试受 Git 管理；临时探针、日志、截图及 manifest 可保存在忽略的 `data/diagnostics/`，长期证据另行备份。归档脚本可能有旧版本假设和环境路径，先审阅再运行。公开记录只写脱敏方法、结果及限制，不复制真实身份或认证头、ticket、会话正文。

### 尚存边界

- P2P offer 调用方取消后，Agent 仍可能完成无人接收的协商并暂留 peer；原子槽位预留仅限制并发总量，不解决取消关联。此项已受控复现，尚未实施新的 offer 取消或未连接 peer 期限策略。
- 用户报告的偶发可见双提示符未稳定复现；已有采样包含 ANSI 清行重绘，不能文本去重或宣称根治。
- 浏览器上传有界不代表端到端流式上传：Gateway 仍全量缓冲请求体；单个超大源块仍可能产生内存峰值。
- Python/JS 的部分稳定错误 reason 双端维护，需要同步核对。
- 上游入口契约升级须重新验收；VPS 同机独立 Agent 目前是部署设计，不能据此声称已实际安装验证。
- PWA、不同移动浏览器及实际移动网络不是完整自动验收覆盖；模型供应商限流、额度错误与 Mesh 传输故障分别归因。
- 离线设备的本地 503 只覆盖 `window.fetch`：`window.WebSocket`、Mesh 自身的 RTT 与 `/api/info` 探测都不经过该判据；`/server/...`、控制面与跨源请求永远走既有路径。导航请求（`Accept: text/html`）有意放行，因此打开离线设备地址仍由 Gateway 返回恢复页。

## 5. 发布与部署

依据任务授权更新版本、CHANGELOG、完成测试和审阅后创建新 tag；不要移动旧 tag。部署命令及回滚流程以 [README](../README.md#更新已有部署推荐) 为准。文档整理通常无需递增运行版本或重启服务。

只在 `src/` 与 `tests/` 内的改动（例如访问日志配置）随 `upgrade.sh` 正常生效，因为升级只替换 `src`、`scripts`、`pyproject.toml`。`config/gateway.json` 不被升级覆盖，改配置后重启对应实例即可。systemd unit 里的改动**不会**随升级生效——`upgrade.sh` 从不重写 unit，只能重装或手工编辑。日志量本身不是部署问题：journald 上限由 `[Journal]` 的 `SystemMaxUse` 控制，属于主机配置，仓库不代改。

服务 `active` 只是进程存活，交付还需核对 `.mesh-revision`、设备在线状态及受影响链路。若维护会话运行于被管理的 OpenCode 服务内，重启它可能中断执行，操作前核实进程依赖。

### 发布前必查：Service Worker 的字节契约

`src/frontend.py` 的 `pwa_service_worker_source()` 把 `src.__version__` 内嵌进 `/sw.js` 的首行注释，浏览器靠比较字节来判断要不要装新 worker。**因此：改动该函数的同一个提交必须递增 `src/__init__.py` 的 `__version__`。**

原因是本项目的发版方式：`upgrade.sh` 卡的是 `.mesh-revision`，不是版本号，所以一次版本号下可以发多次部署。漏掉递增的后果是已装的 WebAPK 继续用旧 worker，**服务端没有任何报错**。这不是理论风险，`maintenance.md` 里多次「版本仍为 0.3.2」的记录就是这种情况。

同时注意：改 `__version__` 会改变 `/sw.js` 字节，这是有意的；反过来，仅仅部署新 revision 而不动该函数，字节不变、worker 也不变，属于正常情况。

## 6. 文档脱敏与更新规则

- 虚构设备使用 `Device A`/`Device B`、`device-a`/`device-b`；Gateway 使用 `https://mesh.example.com`。
- 示例公网 IP 使用 RFC 5737 地址段，如 `192.0.2.10`；个人路径使用 `/home/example/`。回环地址与项目默认端口属于技术配置，不是开发机器身份。
- 官方文档、项目公开仓库地址、公开提交哈希与版本可保留；真实部署映射留在忽略的本地配置中。
- 脱敏当前文件不会擦除 Git 历史；若历史含真正的密钥，另行安排凭据撤销与历史处理，不擅自重写共享历史。
- 改变架构、协议或维护边界时同步更新对应当前文档；历史记录加状态说明，不把当时的失败改写成成功。

### 注释整理候选（独立于功能修改）

精简重复代码字面的说明；将会话式排查叙述迁入记录；保留通道绑定、取消顺序、预算和身份归属等设计原因。单独审阅注释 diff，避免大范围语言替换掩盖逻辑修改。

## 7. 压缩或结束会话前的交接模板

本轮交接（离线设备门禁的范围修正：覆盖他机离线设备 + 导航例外）：

```text
任务目标与当前授权范围：
  修正上一轮已部署版本（50120ec）的范围缺陷：门禁移到 fetch 内既有的他机直通
  判定之前，按显式 /_mesh/device/<id> 逐台判断；同时为 Accept: text/html 的导航
  请求放行，让 Gateway 的离线恢复页仍然生效。线上证据：刷新页面后另一台已注册
  设备的 /api/event 仍约 50 次/分钟 503（12:44:41、12:44:42 两次 GET / 确认拿到
  新适配器，12:44:21 网关重启之后）。
  授权范围：src/static_adapter.py、tests/test_v2_offline_gate.py、文档与 CHANGELOG；
  禁止提交/推送/部署/重启，禁止改 src/main.py、scripts/、android/、PWA、
  reconnect_seconds，不指定 model、不派子 agent。本会话未提交、未推送、未部署。
基线提交 / 当前提交 / 未提交改动：
  基线与 HEAD 均为 d497850（分支 fix/offline-device-request-gate，worktree
  .worktrees/offline-gate）。未提交改动：src/static_adapter.py、
  tests/test_v2_offline_gate.py、docs/architecture.md、docs/maintenance.md、
  CHANGELOG.md。
已完成与证据：
  offlineDeviceRefusal(url, input, init) 移到直通判定之前；新增 acceptsHtml(input,
  init)（读 init.headers 与 Request 输入，读不出按导航处理）与门禁自身的同源检查；
  他机显式前缀不再要求等于 state.manifest.device_id。
  RED：4 项失败（他机离线仍 200 两处、导航被本地拒绝两处），其余 10 项既有测试通过。
  全量 400 passed / 1 skipped（基线 397 + 3；test_v2_offline_gate.py 共 14 项）。
  五个反向变异全部如期失败：删 Accept 排除、门禁移回直通判定之后、删门禁内同源检查、
  删 requestDeviceId 的 /_mesh/ + /server/ 守卫、Accept 只读 init.headers。
  适配器 node --check、compileall、git diff --check 通过。
未完成、阻塞、未复现问题：
  本轮未部署、未做真实浏览器端到端验收；VPS 上他机 /api/event 的 503 是否归零待部署后
  用日志核对。第一版「页面自己设备被本地拒绝」也仍只有单元证据。
  有意不覆盖：window.WebSocket、Mesh RTT 与 /api/info 探测、/server/...、控制面、跨源。
验证命令、结果与适用版本：
  /home/chenweibo/opencode-mesh/.venv/bin/python -m pytest -q -p no:cacheprovider
  -W error::DeprecationWarning -rs
  → 400 passed, 1 skipped。Python 3.14 / Node v22。
关键决定、理由和代价：
  门禁按显式设备前缀逐台判断：页面订阅多台设备是 V2 正常用法，每台离线设备都有独立
  重试循环；代价是排除逻辑必须自己承担，不再依赖拦截位置。
  导航放行是唯一的请求形状例外，判据与 Gateway.wants_html 对齐：本地拒绝会顶掉恢复页，
  代价只是多发一次请求。读不出 Accept 时偏向放行。
  其余决定沿用上一轮：设备归属复用 scopeNativeRequest；TTL 15000ms；方法不限；
  不抽 applyDeviceSnapshot()（bootstrap/transport 测试会截取源码片段求值）。
下一步与涉及文件：
  用户批准后由主 agent 提交并部署，然后核对 VPS 日志里他机 /api/event 是否归零；
  浏览器验收：页面设备与其他订阅设备离线时不再持续 503；地址栏打开离线设备仍是恢复页。
相关提交、当前文档与脱敏历史记录：
  CHANGELOG.md [Unreleased]；docs/architecture.md §6.1/§6.2/§6.5/§10.1/§10.5/§12.5；
  docs/maintenance.md §1/§2/§4/§7。
本地私有证据是否存在、是否已备份：
  绿色版本适配器副本 /tmp/opencode/adapter-green2.py 及变异对照副本在 /tmp/opencode/，
  未纳入仓库，无需备份。
```

### 网关访问日志降噪（2026-10-01，已提交并部署 1c56330）

```text
任务目标与当前授权范围：
  降低网关 uvicorn 访问日志噪音，做成配置项并可定义 log level；默认行为不变。
  用户批准范围：src/main.py、新测试、config 示例、docs 与 CHANGELOG；
  禁止改 scripts/unit、android/、PWA 与 reconnect_seconds。部署由主 agent 执行。
基线提交 / 当前提交 / 未提交改动：
  基线 db1990e（PWA 工作分支）；实现提交 1c56330，基于 8b87e95（main）。
  改动：src/main.py、tests/test_gateway_logging.py、
  config/gateway.example.json、docs/architecture.md、docs/maintenance.md、
  CHANGELOG.md、README.md。
已完成与证据：
  AccessStatusFilter + Gateway.lifespan 装配 + 三个配置键
  （log_level / access_log / access_log_status_min）+ 入口参数收敛到
  gateway_server_options()。全量 410 passed / 1 skipped；四个反向变异如期失败；
  真实 uvicorn 进程三种配置均已核对；VPS 部署后 revision 与 active 核对通过。
未完成、阻塞、未复现问题：
  其他 uvicorn 版本的 record.args 结构未验证；VPS 未启用 access_log_status_min，
  故实际下降幅度未实测。状态码过滤不覆盖 503，离线设备重试噪声需另有对策。
验证命令、结果与适用版本：
  /home/chenweibo/opencode-mesh/.venv/bin/python -m pytest -q -p no:cacheprovider
  -W error::DeprecationWarning -rs → 410 passed, 1 skipped（基线 380）。
  uvicorn 0.53.0 / Python 3.14 / Linux。git diff --check 通过。
关键决定、理由和代价：
  装 filter 而非替换 LOGGING_CONFIG（后者会误伤 uvicorn.error）；
  access_log_status_min 缺省不设置（升级不改变现有部署行为）；
  状态码依赖 uvicorn 内部 record.args 约定，是已登记的上游风险；
  无法解析的记录保留而非丢弃；不改 systemd unit（upgrade.sh 从不重写 unit）。
下一步与涉及文件：
  用户按需在 gateway.json 加 access_log_status_min 并重启 gateway。
相关提交、当前文档与脱敏历史记录：
  提交 1c56330；CHANGELOG.md [Unreleased]；docs/architecture.md §10.1/§10.2/§10.5/§10.6；
  docs/maintenance.md §1/§2/§5/§7；README.md 配置参考。
本地私有证据是否存在、是否已备份：
  一次性探针在 /tmp/opencode/livecheck/，未纳入仓库，无需备份。
```

### 全面审查修复（2026-09-29，部署结果待续记）

- 基线 `04adb39`；用户已批准审查报告中的修复，并授权验证后提交、推送和部署。
- 审查依据：[审查报告](code-review-2026-09-29.md)；执行清单：[修复计划](superpowers/plans/2026-09-29-review-remediation.md)。保留未提交 Android 工作，本轮不重打包 APK。
- 三组分别修改生命周期脚本、浏览器入站装配、Gateway/Agent 限额。复审已要求补齐：不同调用环境的锁一致性和中断进程组清理、普通响应逐跳头、包含发送中帧的 WS 预算、溢出关闭的单次调度、发送失败后 worker 退出、畸形分片归属和迟到数据清理。
- 上述收尾已完成；主审最终完整回归 **317 passed、1 skipped**（缺少 Android API-35 `android.jar`），三份 Shell 分别语法检查、适配器 `node --check`、`git diff --check` 通过。
- 生命周期沙盒验证跨脚本互斥、TERM 后先结束 pip 子进程组再恢复、guardian 随父进程退出释放；新增 fake-stat 与普通/悬空 holder 链接拒绝测试，新建多层安装目录回归先红后绿。跨 UID 的真实 root/普通用户切换未在此环境实测；普通用户不能共同管理另一账户的安装。
- 固定锁位于 sticky、root 所有的 `/tmp`，以真实目录 hash 区分；信任可修改安装目录的 owner，锁目录与 holder 不随 `uninstall all` 删除。SIGKILL 不可捕获，不承诺自动回滚；中断或恢复失败保留的资料须人工核对。
- 浏览器非法帧不能通过伪造业务 ID 取消其他请求；HTTP/SSE 终态墓碑有界，正常长流按已完成逻辑帧释放 payload。64 MiB 仅是装配 payload 预算，不是含 JSON/base64/复制的进程峰值内存上限。
- 本轮不解决下述已披露的 offer 取消关联缺口，不把并发预留或有限去重窗口宣称为完整生命周期/永久 exactly-once 保证。实际部署 revision、设备连通性在完成后续记。

### 离线页设备重选路由修复（2026-09-29，已验证）

- 用户报告点击离线页在线设备后进入 `/_mesh/device/{id}`，前端报路由错误。该地址是合法 API/Server 基址，但不是前端页面路由；此前测试和评审把两者混淆，原文中的“路径型切换入口正确”结论撤回。
- 本次真实设备 API 核对目标上游为 OpenCode 2.0.18，取其当前入口验证：原生首页 `/` 与 `/server/:serverKey/session/:id` 可用，不存在裸 `/server/:serverKey` 首页。按用户要求不新增旧版兼容，不移除离线页或在线设备列表。
- 已批准方案：点击设备进入 `/?mesh_device=<id>`，启动适配器在原生前端初始化前验证并消费一次性选择，写入该设备的首页选择并清除旧设备目录选择；保留默认 Server 偏好、外部 Server 与其他用户状态。成功后移除 handoff 参数，保留其余 query/hash；显式目标不可用时不静默改投其他设备，已有会话深链不被参数覆盖。
- 验证计划：真实适配器 Node 行为回归、完整 pytest，以及隔离 Chromium 中以本地修复替换线上页面适配器的点击验收。浏览器验收不主动断开生产设备，不修改用户浏览器存储；该方式不是已部署验证。
- 修复与浏览器验收准备由显式指定的 `openai/gpt-5.6-terra` 子 agent 并行完成。评审发现校验前仍可能对旧默认设备启动 P2P，已补充完整适配器红灯回归并修正：未验证的显式选择不启动 manifest/offer、设备轮询重连或 RTT 探测，业务 fetch 等待 bootstrap；验证完成后只协商所选设备。覆盖不存在、离线、非 V2 目标及恢复，不只检查 storage 写入。尚未提交、推送、部署或打包；已有 Android 未提交工作保留。
- 主 agent 在最终修复上独立运行完整 pytest：283 passed、1 skipped（Android UI 编译测试缺 API-35 `android.jar`），`git diff --check` 通过。隔离 Chromium 点击验收再次通过：预设另一设备为默认及首页选择，从保留的离线页点击真实在线目标，进入 `/` 并消费参数，bootstrap 与原生首页选择均指向点击设备，默认偏好保持，首页元素可见且无 page error。测试只在隔离 context 替换本地适配器、模拟离线目标及发现列表，其他前端资源来自当前 2.0.18；不等同于线上已部署。
- 后续 UI 需求已获用户确认，正在实施：顶部当前设备名称框改为设备菜单；菜单及离线页状态点对齐 OpenCode 原生圆角、字号、阴影、圆点大小/颜色与深浅主题。路由修复已先完成验证，UI 改动另行回归。

### 设备切换菜单与状态圆点（2026-09-29，已验证）

- 用户批准：沿用顶部横条高度，将当前设备名称框改为按钮；展开设备列表，标记当前项、在线绿点/离线红点，离线不可点击，在线项通过已验证的首页 handoff 切换。离线页继续保留；其状态圆点也统一原生视觉风格。
- 已核对当前 2.0.18 ServerHealthIndicator：圆点直径 6px，成功色浅 `#7add71`/深 `#12c905`，失败色浅 `#ed4831`/深 `#fc533a`。菜单复用页面主题 token，服务器行按当前入口 13px、440 字重、20px 行高、6px 圆角及原生浮层阴影对齐；不凭旧版或通用绿色猜测。
- 菜单打开后独立刷新发现列表，只更新展示，不调用会迁移原生 Server 偏好的启动同步函数。状态获取失败显示未知，旧绿点不得继续可点击；关闭/重开需隔离迟到响应。使用独立样式命名，避免与 Android 的 ⋮ 菜单冲突。
- 离线页无上游 CSS，内联最小主题与状态点样式，优先沿用已保存的明暗偏好，缺省跟随系统，不依赖远程字体或资源。两项实现及隔离浏览器视觉验收准备交由显式指定的 GPT-5.6 Terra 并行处理；尚未提交、部署或重打包 APK。
- 实现收尾修正：固定定位按视口夹取，显式合法字体属性，正确原生状态 token；稳定浮层容器与键盘导航，10 秒 deadline 覆盖响应体解析，旧 body 不覆盖新轮询。主 agent 追加冒泡键事件红测，修复方向键被面板和 document 重复处理；真实 Chromium 复现刷新禁用按钮时焦点丢失，改为临时停留稳定容器，仅在用户未移走焦点时恢复原设备项。
- 另补完整适配器红测修复首页 handoff 等待边界：设备验证成功即允许原生 UI 启动，P2P 后台协商不再阻塞入口至 40 秒；首个业务 fetch 仍最多等 1.2 秒后向明确所选设备走 Relay，未验证目标仍保持隔离。
- 最终独立验证：完整 pytest 291 passed、1 skipped（Android UI 编译测试缺 API-35 `android.jar`），`git diff --check` 通过。隔离 Chromium 在 390px 视口验证菜单真实点击、当前项、离线禁用、安全名称、6px 状态点、浅深主题背景与字号/圆角/阴影、离线页保存偏好优先系统、轮询刷新焦点保持，结果 PASS、无 page error。使用当前真实 2.0.18 前端资源配合本地适配器替换与模拟发现列表，未验证 Android 原生运行，也不代表已部署。

### 设备菜单静默刷新收尾（2026-09-29，未部署）

后续发布状态：已发布 `v0.3.2`，运行提交 `91de7aab2ef81d02eb7c70d7bcb0b1cb4b2db5b1`；VPS Gateway、本机两个 Agent、已恢复在线的远程 Agent 均已部署，服务 active、revision 一致。四台正式设备发现均在线，逐设备 `/api/info` 返回 200 JSON；公网 HTML 已包含静默刷新适配器且无旧 `markDeviceMenuRefreshing`。以下“未部署”描述开发验证时点；本轮未重打包 APK、未操作 PVE。用户已授权后续在批准范围内完成验证后直接提交、推送、部署，无需重复确认这三个动作；新的功能设计仍按范围确认。

- 菜单轮询改为 5 秒；首次查询立即显示 Loading，已有有效展示时后台查询不再禁用条目、改写状态或显示 Updating。成功结果的设备状态、名称、列表和当前项均未变化时保留真实 DOM 条目，避免焦点及圆点闪烁；首次空列表、错误后的空列表成功恢复仍会替换 Loading/错误状态为正常空列表。
- `tests/test_v2_device_menu.py` 先以首次空列表停留 Loading、空列表静默轮询以及当前项变化未更新 `aria-current` 复现，再修正展示有效性与已展示当前设备的守卫。回归同时覆盖 pending 保留可点击状态、失败/超时未知状态和恢复、关闭/重开迟到响应隔离、10 秒正文 deadline、键盘冒泡与焦点保持。
- 本轮完整 `.venv/bin/python -m pytest -q -p no:cacheprovider -W error::DeprecationWarning -rs`：291 passed、1 skipped（Android UI 编译测试缺 API-35 `android.jar`）；提取适配器 `node --check` 与 `git diff --check` 均通过。未提交、推送或部署；现有 Android 未提交源码和产物均保留。

### 本轮发布状态（2026-09-29）

- 用户授权提交、推送和部署，明确不重打包 APK。上述两节“尚未提交/部署”是开发验证时点；网页代码已提交 `02187e0961f4cd3b9e0f243dc957de042121520f` 并推送远端 `main`，产品版本仍为 0.3.1，未移动已有版本标签。
- VPS Gateway 首次 SSH 超时，连通性恢复后升级脚本成功部署 `02187e0` 并恢复服务。本机两个 Agent 的运行源码与提交一致，已重启、确认 active，并记录同一 revision；为保留未提交 Android 工作，本机未用整目录替换升级。
- ehang-box 未部署：SSH 报 `Could not resolve hostname ehang-box`，当前 SSH 配置展开后的 hostname 仍为该别名；需恢复可解析地址或提供当前 SSH 入口后继续。PVE 未操作。已有 Android 源码及 APK 均保留。

### 先前维护状态（2026-09-25）

- 线上运行提交为 `dff5709`，已推送远端 `main`，包含前台 P2P 健康探针。Gateway、远程 Agent、本机两个 Agent 的服务状态及 revision 已核对；四台设备 API 返回 200，公网 HTML 确认包含最终探针代码。这是部署检查，不是完整浏览器验收。
- 用户在安卓 16 手机浏览器实测锁屏恢复有明显改善，继续浏览器验证；尚未宣称所有移动网络/终端恢复场景验收完成。
- `v0.3.1` 标签之后的变更已汇总到 `CHANGELOG.md` 的 Unreleased；运行版本仍为 0.3.1，待验证稳定后另行安排版本号、标签与发布。
- 本轮收尾范围：修复 Agent WebSocket 空关闭原因异常并补回归、同步维护记录。不将它认定为锁屏故障根因。
- Android 工作暂缓，保留未提交源码、测试与既有预览包；0.1.1-preview 内置旧适配器，不会随 Gateway 更新获得锁屏恢复修复。多设备 P2P 连接池暂不推进。
- 下方记录保留各阶段验证证据；其中“未发布”表示未制作新版本标签，不代表尚未部署。历史验证时点状态以本节及对应后续记录更新为准。

### WebSocket 关闭原因收尾（2026-09-25，部署已授权）

- `Agent.local_ws` 曾将缺省或空关闭原因转为 `None`；实际 websockets 17.1 在序列化关闭帧时调用字符串编码，因而抛出 `AttributeError`。现在缺省、空串及显式 null 均传空字符串，正常非空原因与关闭码保持原值。
- 在 `tests/test_mesh_reliability.py` 增加真实本地 WebSocket 服务与客户端回归，覆盖上述四种输入并断言上游收到的关闭码/原因及控制端结果。修复前复现异常关闭 1006 和序列化异常，修复后四种输入均通过；不以宽容 mock 代替帧验证。
- 主维护者复核实际 diff 并独立运行完整 pytest（`-W error::DeprecationWarning`）：`275 passed in 7.63s`，`git diff --check` 通过。关闭码转换未改动；修复的是连接处理任务的异常，不据此宣称整个 Agent 进程会崩溃。
- 未做跨进程部署或浏览器/真机端到端验收，也未验证所有受支持的 websockets 版本。本缺陷与此前锁屏无响应的因果关系未证实。
- 用户已授权提交、推送并部署本项代码、测试、CHANGELOG 和状态更正；提交前完整复跑 `275 passed in 7.79s`。部署结果以各端 `.mesh-revision` 与服务检查为准；既有 Android 工作保留，版本标签发布仍待用户浏览器验证稳定。

### 前后台恢复旧 P2P 前台健康探针（2026-09-25，未发布）

- 实施基线 `258 passed`，分支 `feat/android-apk`；探针代码、回归测试与相关文档已提交为 `dff5709` 并部署。原有 Android 未提交工作保留。
- 任务（用户已批准）：修复用户现场安卓 16 锁屏后 P2P 无延迟但 API 全挂——页面从后台恢复时遗留的旧 open P2P 通道可能陈旧（通道活着但 Agent 侧不再转发）。实现前台健康探针：`visibilitychange` 可见或 bfcache `pageshow` 恢复时若遗留旧 open P2P 通道，立即发送探针 ping 并在 3s 内等待匹配 pong；无 pong 则淘汰旧通道并后台重建；验证期间新 HTTP/WebSocket 请求暂时走 Relay；mutation 不重放、迟到结果不影响新连接。
- 关键决定：探针用独立 `state.probe = { channel, generation, pingT, timer }` + `state.probing` 标志，绝不占用周期 ping 槽位（`pingSent`）；`failProbe` 先清探针、按 channel+generation 身份守卫后走 `releaseCurrentAttempt(true)` + `failTransport(new Error('P2P disconnected; request outcome may be unknown'))` + `runAttemptForCurrentDevice()`，文案沿用现有断开路径；`clearProbe()` 挂入 `failTransport` 顶部，覆盖 close/kick/切设备全部 teardown；visibilitychange hidden 分支直接 `clearProbe()`（不淘汰通道），visible/pageshow 保持原顺序调 `resetRttAfterBackground()` + `beginForegroundProbe()` + `onForegroundResume()`；移除旧的立即 ping 块由探针取代其作用；pong 匹配先探针（`message.t === probe.pingT`）再周期 `pingSent`；fetch/WS 包装器在 probing 时走 Relay、`transportInfo` 显示 Relay；重复 visibility/pageshow 不重启不延长 3s 时限，探针期间再次隐藏取消在途探针不执行过期淘汰，重新可见开启新完整窗口；初始可见不当作锁屏恢复、不触发探针；服务端 pong 用 envelope（`data` 内层含 `t`），不用丢 `t` 的旧 `deliver` helper。
- 红绿：新增 13 项 Node 行为测试先写后改，红阶段 `13 failed`，实施后文件 `13 passed`；全套 `271 passed`（基线 258＋13，`-W error::DeprecationWarning`），compileall、`git diff --check`、提取适配器 `node --check` 通过。独立探针 `/tmp/opencode/probe_background_stale_open.py` 的场景 A 断言在修复后按设计失效，属预期。
- 测试说明：`s.closed === true` 不可观察——`runAttemptForCurrentDevice→connectP2P` 入口立即置 `closed=false`，改用旧通道 disposed + 传输绑定到新 negotiating channel 断言；探针 ping 同步 `send()` 入 `channel.sent`，`deliverPong` 需守卫重建后已 detach 的旧通道 `onmessage`。
- 主 agent 收尾复核：新增 3 项边界回归，分别先复现同毫秒取消/重启探针误认旧 pong、事件循环延迟时超时 pong 抢先于定时器保留旧连接，以及初始建连等待结束后绕过探测门禁。红阶段分别为 `2 failed, 13 passed` 和 `1 failed, 15 passed`。探针 `pingT` 改为独立唯一字符串标识（Agent 原样回传 `t`），另存 `startedAt` 测延迟；完成路径校验 channel/generation 和实际截止时间；初始 fetch 等待前后均检查 probing。
- 最终验证：完整 `.venv/bin/python -m pytest -q -p no:cacheprovider -W error::DeprecationWarning` 为 `274 passed in 7.53s`（前台健康测试共 16 项）；`git diff --check` 和实际适配器 `node --check` 通过。独立 reviewer 服务报错未完成，由主 agent 复核并补上述红绿证据。未提交、未部署、未重新打包 APK；安卓浏览器锁屏现场仍待用户验证。
- 后续状态：上述最终验证段末尾的“未提交、未部署”为部署前时点；此后已提交、推送和部署 `dff5709`，用户报告安卓浏览器锁屏恢复明显改善。未重新打包 APK，未进行开发者真机全链路验收。
- 下一步：用户继续浏览器验证，特别是反复锁屏、网络变化和终端恢复；结果回填本节。

### APK UI 层：菜单、主题同步与原生连接状态（2026-09-25，未发布）

本地预览产物已生成：`opencode-mesh-0.1.1-preview.apk`（versionCode 2，10,585,171 字节），SHA-256 `e2dd1d52814179fbaccc12f0cb428596d32e6ac1610a4bf999d869015fa886c9`。复用 0.1.0 签名，可覆盖安装；原预览文件保留。全部 1,103 个资源的包内 hash 和 v2/v3 签名通过校验。源码尚未提交/推送，生产服务未部署此 Android 工作。

- 基线 `bc7d86c`，分支 `feat/android-apk`；与 logo worker 并行（后者拥有 Manifest/`build_apk.py`/`res/`，本项不触碰）。盘满事故后先核对无 ENOSPC 截断再续写；本项全部写入已验证。
- 任务（用户已批准）：去掉 `MainActivity` 独占高度的原生 Gateway/Reload 工具栏；新增 APK 专用 `android/ui.js`，在传输适配器 `#ocm-mesh-bar` 右侧追加 ⋮ 菜单（Gateway settings/Reload，仅用 `var(--v2-*)` token）；把实际网页背景与明暗同步到原生窗口/系统状态导航栏（target 35 手势背景透明由 window 背景承接，不承诺设置手势 pill 颜色），安全区/键盘用 insets 适配；原生 Gateway 对话框跟随主题；首次配置/priming/失败必须有可见连接状态与 Retry/修改 Gateway，不依赖未加载网页菜单。
- 关键决定：桥接不用 `addJavascriptInterface`，而是受限 `ocm-app://` 主帧导航（settings/reload/theme），Java 侧以“当前 view 身份 + 主帧 + priming 完成（本地 UI 就绪）+ 页面为固定 Gateway 同源”门槛放行，其余一律拦住；主题回传只含 `r/g/b/a/dark` 整数、绝不携带凭据，颜色由浏览器计算（computed style 经 canvas `getImageData` 归一化，兼容嵌套 `var()`/oklch，不把原始 token 当 hex）。`AppScheme.java` 纯 JVM 负责严格解析/门禁/alpha 合成；`build_frontend.py` 把 `ui.js` 原样打包为 `web/mesh-ui.js` 并更新 index.html 的 pinned patched hash（新 hash `12baf8b3…`），package 必含 mesh-ui.js。认证 priming 精确 GET、JS disabled、15s deadline、view 身份守卫、无 mutation 重放、文件选择器全部保留；`src/static_adapter.py` 未改动。
- 红绿：`tests/test_android_ui.py`（Node DOM/Canvas shim 驱动真实 `ui.js` 行为 + JVM `AppScheme` 契约 + API-35 android.jar 编译门）与 `tests/test_android_frontend_build.py` 首轮红 `19 failed, 8 passed`，实施后目标 27 项全绿。主 agent 合并并修正 API 26–29 insets 兼容、API 30+ 显式 edge-to-edge、原生弹窗主题后，独立完整复跑 **251 passed**（`-W error::DeprecationWarning`），diff 检查与真实 SDK 构建通过。
- 浏览器证据：固定版源码重建后，Chromium 对真实 Gateway 加载 73 个 APK 本地资源，0 缺失、0 page errors、0 service worker；将原生 scheme 导航替换成记录器后，设置/重载菜单以及深浅背景主题回传均通过。图标使用固定上游 Web logo，调整至 adaptive icon 安全圆内。
- 未验证/限制：本次 0.1.1 未做真实 Android 设备/模拟器验收——`ocm-app://` 拦截、系统栏/弹窗主题、insets/键盘均待真机复验；手势 pill 颜色属于系统行为。用户对 0.1.0 的整体认可不代替这些新增路径验收。旧准备目录只恢复 index.html 的原始入口后重新执行固定 hash 的 prepare，前端与原生包已经重建；不再需要重新下载上游或安装依赖。根分区空间有限，临时产物放独立 `/tmp/opencode` 文件系统。
- 后续状态：UI 与 logo 已合并构建并交付上述 0.1.1-preview。用户决定暂缓 APK，保留源码与产物，后续恢复时再纳入最新适配器并按 android/README.md 第 4 节验收。

### 统一设备在线判据与离线页行为（2026-09-24，未发布）

后续部署状态：本项已提交为 `bc7d86c` 并推送、部署三端，服务 revision 与正式设备 API 检查通过。以下“未提交/未部署”仅描述当时开发验证阶段，不表示仍待部署；真实离线页浏览器验收尚无完整记录。

- 基线 `88c0d09`，工作区此前干净；本轮改动未提交：`src/main.py`、新增 `tests/test_v2_offline_page.py`、`docs/maintenance.md`、`docs/architecture.md`。
- 任务：统一 `/_mesh/devices` 的 `online` 与 `Gateway.is_online` 为同一判据（`ws` 存在且 `last_seen` 在 45 秒内，缺 `last_seen` 的旧状态保持在线兼容）；离线页明确当前目标设备名称（仅 `textContent`，防 XSS）、其他设备在线时不再宣称无设备、列出实时 online/offline 与在线设备的路径型切换入口 `/_mesh/device/{id}`；仅原目标恢复才 `location.reload()`，无 target 时绝不自动改投/重放；轮询失败清空旧绿灯、显示状态未知，下一次成功轮询恢复；保留 `no-store` 与 3s 轮询；`transport-manifest.p2p.enabled` 与 `p2p/offer` 改用同一新鲜度判据，但不新增陈旧连接的 close 生命周期逻辑。评审收尾两项：浏览器 WebSocket 显式设备分支从「仅看 `ws` 布尔」统一为同一新鲜度判据（stale 以 4403 拒绝且不动在途 Agent 连接）；每次轮询加 10 秒 deadline（AbortController + 定时器清理），永不 settle 的请求被终止、状态未知、守卫释放、迟到响应不得覆盖未知状态。
- 关键决定：提取模块级 `device_online(device, stale_after=45)` 为唯一实现，`Registry.public()`、`Gateway.is_online()`、manifest/P2P gate、浏览器 WebSocket 入口全部引用；切换入口使用前端适配器已用作设备身份与探测的路径型条目 `/_mesh/device/{id}`（与 `serverTabUrl` 同款，已被既有 bootstrap 测试验证），不猜测其他路由；离线页 `update()` 中目标在线时仅 reload；`setUnknown()` 清空列表，避免上一轮绿灯被当作实时结果；`fetching` 守卫防止轮询叠加，且必须由 10 秒超时终止旧请求（`AbortController` 可用时 abort；不可用时超时仍释放守卫并丢弃迟到结果），成功路径 `finish()` 清理定时器。家庭 PVE 原“绿灯”是真实在线状态，判据统一后仍在线的设备保持绿灯。
- 红绿：首轮新增 8 项行为测试先写后改，红阶段 `7 failed, 1 passed`，实施后 `8 passed`。评审收尾再增 5 项（3 项 ASGI 浏览器 ws gate ＋ 2 项 Node 真实脚本 hung/deadline），修正驱动 websocket 的辅助器嵌套 `asyncio.run` 后红阶段 `3 failed, 10 passed`（stale 分支被 accept、无 deadline 时 hung 永久卡住轮询与迟到数据覆盖，均按预期失败），实施后文件 `13 passed`；全套 `220 passed`（`-W error::DeprecationWarning`，基线 215 ＋ 5），compileall、`git diff --check`、提取页面脚本 `node --check` 通过。
- 测试：ASGI 用隔离 `state_file` 覆盖 stale/fresh/legacy 设备的 `online`、`default_device`、离线页 `no-store`、manifest/P2P 判据；直接驱动 ASGI websocket 通道覆盖显式设备的 stale→4403（不动在途连接）、fresh→桥接、未注册→4403；Node 执行真实 OFFLINE_PAGE 脚本（脚本化 fetch + DOM shim + 假时钟，新增 `hang` 永不返回与 `delay` 迟到两种响应、记录 fetch 的 `signal`），覆盖目标离线他机在线不 reload 且切换链接正确、目标恢复 reload 恰好一次、无 target 时在线设备不被隐藏且不自动切换、已知/未知敌对设备名仅作文本不执行、轮询失败（网络错误与 500 响应）状态未知与恢复、hung 轮询 10 秒后 abort 且下一轮恢复、迟到响应不覆盖未知状态，避免纯字符串断言。
- 未验证：均为 Node/ASGI 行为测试，未做真实浏览器复验（延续此前停止真实复现的要求）；未部署。离线页依赖 `li.append`/`encodeURIComponent` 等现代浏览器 API，未在旧浏览器验证。
- 下一步：用户独立审阅 diff；如需真实浏览器验收按本文档第 4 节执行，并把结果追加到本文档。

部署前复核：新增迟到轮询完成不得释放新轮询守卫的行为用例，先失败再将 `finish()` 改为幂等；旧迟到响应测试曾依赖提前释放守卫，现按第二轮 22 秒超时、24 秒再次轮询的正确时间线断言。主维护者独立全套验证 **221 passed**。用户已授权提交并部署三端，真实页面观察由用户进行；本记录的“未部署”描述为验证时点状态，实际部署以各端 `.mesh-revision` 为准。临时验收设备已通过其设备身份注销接口清理，四台正式设备保留。

### 浏览器适配器重连事件监听（2026-09-24，未发布）

后续部署状态：本项已提交为 `88c0d09` 并推送、部署三端；用户现场网络切换效果与后续前台健康探针验证分别记录，不以 Node 测试替代真实浏览器验收。

- 基线 `b40be41`（0.3.1），工作区此前干净；本轮改动未提交：`src/static_adapter.py`、`docs/architecture.md`、`docs/maintenance.md`，新增 `tests/test_v2_reconnect_network.py`。
- 任务：为浏览器适配器的 Relay 重连加入网络事件监听（`window.online` + 可选 `navigator.connection` change）、300ms 防抖 + 5s 冷却、取消任意未打开阶段（初始 ICE、重试 ICE、等待通道打开）的旧协商并立即新尝试、单一有效 attempt 与 generation 守卫、旧协商链的 close/catch/finally 不得污染新尝试、已 open P2P 不拆线、前台恢复/bfcache 恢复（>15s 陈旧）经同一受冷却控制的提示入口提前重试、后台重试不让业务 fetch 等待 1.2s（仅首轮保留）、指数退避兜底、40s 总 deadline 中断从 createOffer 到通道打开的全部阶段、事件风暴不产生并发/资源泄漏。`online` 只是 hint，不据此关闭 Relay。
- 关键决定：hint 取消并立即重建任意尚未打开阶段的旧协商——包括首轮协商（防抖+冷却已限频，单次 hint 的重建代价有界），已打开的通道是唯一例外；旧版本写成"进入 WebRTC 阶段回到退避、首轮不打断"，经独立审查后按已批准方案修正，相关测试同步更正。统一 teardown `releaseCurrentAttempt`：先推进 generation、解绑旧通道 handler、abort controller、关闭旧 peer、清理双 timer，再立即跑新尝试，避免旧链 catch/finally 经生成期匹配污染新链（退避安排、退避值、controller 槽、`isInitialAttempt`）以及 close 同步回调重入。协商链的 then/catch/finally 均以创建的 generation 守卫，`connectP2P` 的 finally 用 `state.activeController === controller` 归属守卫清理。`createOffer`/`setLocalDescription`/`setRemoteDescription` 纳入 deadline/取消等待，过期链不再向新网络发 offer。foreground/pageshow 走同一防抖+冷却 hint 入口，重叠提示合并单飞。`runAttemptForCurrentDevice` 显式置 `isInitialAttempt=false`（它从不服务页面 bootstrap），hint 取消首轮后启动的新尝试不继承 1.2s 等待；kick 路径同样调用 `failTransport` 失效未 open 旧传输上的 pending/stream，防止通道已死但 close 未派发时悬挂（原测试断言 `isInitialAttempt===true` 仅用于归属验证，现按策略改为 false）。manifest 解析后把 `routeDeviceId` 同步为实际服务设备，避免无显式选择页面被 2s 周期检查误判为设备切换。
- 证据：20 项 Node 行为测试运行真实 TRANSPORT_ADAPTER（可控假时钟 + 脚本化 fetch/RTCPeerConnection/channel，且 `Date.now` 与假时钟同步；新增 ICE/OFFER 停滞开关、静默关闭通道与 pageshow 触发）。按修正意图先更新测试，两次红阶段分别为 `8 failed, 10 passed` 与追加 2 项后的 `3 failed, 17 passed`；实施后 `20 passed`，全套 `207 passed`（`-W error::DeprecationWarning`），compileall、`node --check` 提取的适配器、`git diff --check` 均通过。
- 测试侧更正说明：原第 4 项（"提示期间初始协商保持"）与第 12 项（"提示后开等回到退避"）固化了驳回的错误意图，已改写为"初始协商可被提示失效重建"与"开等被提示立即取消重建且不等旧 30s 退避"；测试 6 因 foreground 走防抖入口需 `advance(300)`；测试 3 增加新 controller 不被旧 finally 清空的断言。
- 未验证：全部为模拟网络事件的 Node 行为测试，未做真实浏览器/真实网络复现（用户此前要求停止真实复现）；`navigator.connection` 缺失分支仅由 harness 空对象覆盖；createOffer 停滞与 40s 总时限未在上游真实页面复核。
- 下一步（如需继续）：用户独立审查 diff；如需真实复验，按 `docs/maintenance.md` 浏览器验收方法进行，并将结果追加到本文档。

### Relay → P2P 切换在途请求调查（2026-09-25，隔离测试完成，现场未复现）

- 用户在浏览器和 APK 中均遇到子任务入口无响应，刷新可恢复；会话内标签切换曾报 `ClientError: Transport`，原因分别为 `TypeError: Failed to fetch` 和 `AbortError: Aborted`。用户认为常发生于 Relay → P2P 切换，不限定设备。尚未复现因果链。
- 已核对交付 APK 0.1.0/0.1.1 的传输脚本与当前源码；线上入口的内联适配器与 APK 0.1.1 相同（仅忽略首尾空白）。错误栈中的 `mesh-transport.js:968:232` 对应原生 fetch 旁路分支，但缺少现场请求 URL，不能认定命中了其中哪一个条件。
- APK 固定前端 2.0.15，受查设备入口的上游为 2.0.6。两版子会话导航和请求队列实现一致；2.0.15 另有工具运行时自动刷新消息的行为，不将版本差异本身当作根因。正常 Server 选择和会话导航走 SPA 状态/路由，不是新窗口或主帧重新加载。
- 共享 P2P 取消处理会把信号原因改为固定 `AbortError`，超时原因可能因此丢失；这是已确认的语义差异，不等于已定位无响应原因。上游队列的响应头超时不覆盖排队和响应体读取；响应头释放队列槽位也不等于释放整个会话同步去重键，不宣称请求总时长有 60/120 秒上界。
- 当前授权：在隔离 Node harness 中执行真实适配器，覆盖 Relay 请求尚未结束时 P2P 成功建立、响应体仍在读取、旧请求失败/取消及后续新请求的通道归属；检查未知结果 mutation 不重放。先记录测试证据，尚未授权生产断网、重启、清理存储或部署。
- 若需要运行时诊断，只记录相对时间、临时关联编号、方法/API 路径类别、通道及协商代次、结果/取消原因名称；不记录凭据、URL 查询参数、会话标识、请求体或响应内容。尚未加入运行时诊断代码。
- 隔离结果：新增 `tests/test_v2_relay_p2p_switch.py`，运行真实适配器配合模拟 fetch/WebRTC 和假时钟。7 项覆盖响应头尚未返回的设备作用域 GET/POST 跨通道建立、响应体/SSE 在途、旧 Relay 取消/失败、新请求通道归属、初始 1.2 秒等待和建连失败回退，以及 P2P 断连后的已发 mutation 失败且不重放。补测时修正了测试自身的冷却时序，并让模拟先收到协商 answer 再打开通道；重连后的新请求也验证至响应体结束和 pending/streams 清空。
- 主 agent 独立验证：新文件 `7 passed`；`.venv/bin/python -m pytest -q -p no:cacheprovider -W error::DeprecationWarning` 为 `258 passed in 8.14s`；`git diff --check` 通过。未修改产品适配器、Android 或部署；保留既有未提交 Android 工作。
- 结论边界：这些受控时序中未发现“P2P 打开直接打断旧 Relay 请求”，不是用户故障已修复，也未覆盖真实网络/浏览器事件、上游队列与同步去重、原生 WebView。下一步需把现场请求进入适配器、响应头、正文结束、取消及通道变化关联起来，定位卡在请求排队、传输还是应用同步状态；不凭模拟通过新增猜测性修复。

### V2 预加载资源作用域修复（2026-09-23，未发布）

- 基线提交 `9a32167`。真实上游 2.0.14 的预加载器将依赖路径拼为 `/_assets/...`；同一 CSS 在 Gateway 根路径返回 404，在明确设备路径返回 200，确认请求丢失来源设备。
- `src/frontend.py` 为已验证的唯一预加载路径构造函数注入来源设备前缀；`src/main.py` 传入该设备身份。命名空间从 `/ui/1/` 更新为 `/ui/2/`，隔离旧 immutable 缓存；未知或歧义契约明确报错。
- 回归覆盖 CSS/JS 依赖、三种字符串引号、未知与歧义契约、Gateway 实际适配响应。187 项 pytest 通过（含 DeprecationWarning 作为错误），compileall 与 diff 检查通过。
- Chromium 通过请求拦截应用本地适配器，未修改生产部署。改为等待输入框可见后，三个设备的会话深链接和刷新均出现输入框，无 pageerror；原报错的 CSS 和关联 JS 从来源设备命名空间返回 200。探针仍因某次 `button-*.css` 请求超时退出 1，不能宣称整体验收全绿；该 CSS 单独复查两种设备路由均返回 200，超时原因尚未确定。
- 用户补充：问题可能由 Agent 在升级等过程中突然断连触发，并要求停止继续复现和浏览器验证，直接部署到本机、远程 Agent 和 Gateway。该触发条件尚未主动复现；此前深链接和刷新测试不等同于断连场景验收。修复在 Gateway 生效，不要求清理设备数据或浏览器存储。

### v0.3.0 生命周期验收（2026-09-23）

后续 v0.3.1 改进：无参数升级自动发现 user/system 服务并按安装目录选择；校验目标后展示版本与提交，相同提交不重启。成功后清理临时恢复资料，恢复失败保留。179 项 pytest 通过，其中 PTY 使用模拟 systemctl 验证非仓库当前目录启动、多个安装目标、输错编号重试与取消；升级执行测试覆盖原运行集合、重复提交、回滚与临时资料清理。本轮没有自动执行生产升级，用户自行验收。

- 发布提交 `cd53a21`，标签 `v0.3.0`；175 项 pytest 通过，Shell 语法和 diff 检查通过。
- 本机真实 PTY 自动化交互：安装临时具名 Agent（6 项提示、服务 active）→ 交互升级到该标签（6 项提示）→ 卸载临时实例（3 项提示）。使用真实 systemd，而非 mock。
- 升级后两个原有 Agent 均 active，配置内容与身份文件哈希不变；临时单元已移除，临时身份归档到仓库外，运行数据目录仅保留原有身份。
- 首次验收因探针错误匹配 Gateway 提示而超时，未完成安装；修正匹配与超时退出后重跑，三项流程退出码均为 0。此验收不等同于所有人工交互场景或浏览器链路重新验收。
- 当前仅完成本机运行版升级；其他主机的部署状态须分别核对，不由 Git 推送或标签推断。

```text
任务目标与当前授权范围：
基线提交 / 当前提交 / 未提交改动：
已完成与证据：
未完成、阻塞、未复现问题：
验证命令、结果与适用版本：
关键决定、理由和代价：
下一步与涉及文件：
相关提交、当前文档与脱敏历史记录：
本地私有证据是否存在、是否已备份：
```

新会话先核对 Git 与文件，再依交接继续；不要重复执行已经完成的计划，也不要将本地忽略目录的存在视为新克隆仓库的前提。
