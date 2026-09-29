# 维护、验证与会话交接

本文是新会话和维护者的工作入口。长期规则见根目录 [AGENTS.md](../AGENTS.md)，用户操作见 [README](../README.md)。

## 1. 先确认事实来源

### 上游 OpenCode 健康探测（2026-09-29，已部署，待浏览器人工验收）

- 目标：区分 Mesh Agent 控制在线与本机 OpenCode 可用；保留原设备、身份及业务传输，不因探测失败重放请求或关闭控制连接。
- Agent 独立探测 `/api/info`，完成后等待 5 秒，单次总期限 2 秒；连续两次失败后标记不可用，一次成功恢复。结果变化与心跳均携带健康状态及单调时钟年龄，Gateway 以 30 秒新鲜度计算 `upstream_health`、`available`；`online` 仍指控制连接。健康数据不持久化。
- 菜单和顶部状态定期刷新；离线页区分 Agent offline、OpenCode unavailable/authentication failed、unknown。页面入口遇到新鲜明确失败时返回原目标恢复页；首页手动 handoff 选择页面来源，API/POST 转发保持原语义。
- 回归覆盖探测认证、错误分类、两失败一恢复、总超时、健康字段类型、age、持久化隔离、控制 ping/hello 清理、HTML 入口与跨设备 handoff。修正了旧测试把第一次超时当成最终不可用的错误预期，以及测试协程尚未启动便取消时误要求执行 finally 的问题。
- 浏览器人工验收待用户操作：先停止/恢复 OpenCode（Agent 保持运行），再停止/恢复对应 Agent，分开观察状态与恢复。本轮尚未主动停服进行故障复现；Android APK 不重打包。
- 验证：全套 pytest **352 passed, 1 skipped**；跳过项为 Android API-35 `android.jar` 缺失。JavaScript 语法与 `git diff --check` 通过；部署后再记录服务和健康 API 证据，单元测试不等于浏览器停服验收。
- 部署：运行提交 `6fb3d702e16dde14321c998a1515c0c984c10f30` 已推送并部署至 Gateway、远程 Agent 及本机两个 Agent；revision 与 active 独立核对通过。三个已更新 Agent 的公开健康为 healthy/available，业务 `/api/info` 均 200 JSON；未更新 Agent 的控制在线但健康 unknown/available=false，符合旧 Agent 契约。未执行真实停服测试，等待用户浏览器配合；版本仍为 0.3.2，未移动发布标签。

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
| `tests/test_mesh_reliability.py` | 代理、分片及生命周期回归 |
| `tests/test_v2_*.py` | 实际 Node 浏览器接口行为及 V2 错误、启动、上传、WS 边界 |
| `tests/test_v2_offline_page.py` | 统一在线判据（列表/路由/P2P/浏览器 ws gate）、离线页行为与轮询失败/永不返回恢复（ASGI 直驱 websocket + Node 真实页面脚本） |

原生 OpenCode 管理 Server 名称、项目、会话和终端 UI；Mesh 仍注入适配脚本与状态栏。当前活动设备使用一条 P2P 通道，其他 Server 请求按明确地址走 Relay。

### 关键决定与已排除的错误方向

- Relay 空 400 曾由解码后保留 `Transfer-Encoding`、同时生成 `Content-Length` 引起。解决点是 HTTP 分帧头，不是补 `{}` 或改写模型字段。
- 绝对 `/api/...` URL 会丢失路径型 Server 基址；在 URL 构造时保留设备作用域，不能用当前页面选择重路由所有后台请求。
- Gateway origin 不作为额外业务 Server；设备显示名、默认选择和稳定身份是不同概念。
- 默认设备短时不可达时保持旧项目的 canonical 归属；首次默认选择可以选择在线设备，显式用户选择保持。
- 上传超限回 Relay 要保留已读前缀和剩余字节；不依赖全量 `arrayBuffer()` 探测，也不自动重放已发 mutation。

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

浏览器自动化不是当前 pytest 的一部分。正式回归测试受 Git 管理；临时探针、日志、截图及 manifest 可保存在忽略的 `data/diagnostics/`，长期证据另行备份。归档脚本可能有旧版本假设和环境路径，先审阅再运行。公开记录只写脱敏方法、结果及限制，不复制真实身份或认证头、ticket、会话正文。

### 尚存边界

- P2P offer 调用方取消后，Agent 仍可能完成无人接收的协商并暂留 peer；原子槽位预留仅限制并发总量，不解决取消关联。此项已受控复现，尚未实施新的 offer 取消或未连接 peer 期限策略。
- 用户报告的偶发可见双提示符未稳定复现；已有采样包含 ANSI 清行重绘，不能文本去重或宣称根治。
- 浏览器上传有界不代表端到端流式上传：Gateway 仍全量缓冲请求体；单个超大源块仍可能产生内存峰值。
- Python/JS 的部分稳定错误 reason 双端维护，需要同步核对。
- 上游入口契约升级须重新验收；VPS 同机独立 Agent 目前是部署设计，不能据此声称已实际安装验证。
- PWA、不同移动浏览器及实际移动网络不是完整自动验收覆盖；模型供应商限流、额度错误与 Mesh 传输故障分别归因。

## 5. 发布与部署

依据任务授权更新版本、CHANGELOG、完成测试和审阅后创建新 tag；不要移动旧 tag。部署命令及回滚流程以 [README](../README.md#更新已有部署推荐) 为准。文档整理通常无需递增运行版本或重启服务。

服务 `active` 只是进程存活，交付还需核对 `.mesh-revision`、设备在线状态及受影响链路。若维护会话运行于被管理的 OpenCode 服务内，重启它可能中断执行，操作前核实进程依赖。

## 6. 文档脱敏与更新规则

- 虚构设备使用 `Device A`/`Device B`、`device-a`/`device-b`；Gateway 使用 `https://mesh.example.com`。
- 示例公网 IP 使用 RFC 5737 地址段，如 `192.0.2.10`；个人路径使用 `/home/example/`。回环地址与项目默认端口属于技术配置，不是开发机器身份。
- 官方文档、项目公开仓库地址、公开提交哈希与版本可保留；真实部署映射留在忽略的本地配置中。
- 脱敏当前文件不会擦除 Git 历史；若历史含真正的密钥，另行安排凭据撤销与历史处理，不擅自重写共享历史。
- 改变架构、协议或维护边界时同步更新对应当前文档；历史记录加状态说明，不把当时的失败改写成成功。

### 注释整理候选（独立于功能修改）

精简重复代码字面的说明；将会话式排查叙述迁入记录；保留通道绑定、取消顺序、预算和身份归属等设计原因。单独审阅注释 diff，避免大范围语言替换掩盖逻辑修改。

## 7. 压缩或结束会话前的交接模板

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
