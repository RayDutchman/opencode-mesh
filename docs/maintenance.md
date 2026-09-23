# 维护、验证与会话交接

本文是新会话和维护者的工作入口。长期规则见根目录 [AGENTS.md](../AGENTS.md)，用户操作见 [README](../README.md)。

## 1. 先确认事实来源

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
