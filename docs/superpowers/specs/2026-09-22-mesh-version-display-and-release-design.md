# OpenCode Mesh 版本展示与版本控制设计

> **历史状态声明：** 本文是特定时期的历史记录，不是当前实施指令；当前以 [`../../../README.md`](../../../README.md)、[`../../architecture.md`](../../architecture.md)、[`../../protocol.md`](../../protocol.md) 和 [`../../maintenance.md`](../../maintenance.md) 为准。

## 背景

OpenCode Mesh 当前在 `pyproject.toml` 中声明了 `0.1.0`，但运行时代码没有统一版本入口，Git 也没有发布 tag，安装脚本固定从 `main` 分支下载。Gateway 注入浏览器的 Mesh 状态栏目前只显示传输状态和设备信息，无法判断浏览器实际加载的是哪个 Mesh 版本。

## 目标

1. 在注入到 OpenCode 网页顶部的 Mesh 状态栏显示软件版本。
2. 建立单一版本来源，避免 `pyproject.toml`、运行时代码、注入脚本和发布 tag 不一致。
3. 让安装脚本支持稳定版本 tag，同时保留开发分支安装能力。
4. 增加版本一致性和状态栏注入测试，并用 CI 在 push/PR 时验证。

## 版本来源与显示

- 单一事实来源为 `src/__init__.py` 的 `__version__ = "0.1.0"`。
- `pyproject.toml` 使用 setuptools 动态版本读取该值，使 Python 包元数据和运行时代码保持一致。
- 状态栏显示 `OpenCode Mesh v0.1.0`。
- 状态栏只显示正式 SemVer 版本，不依赖运行环境中的 Git 信息。
- `src/main.py` 中已有的 `transport-manifest.version = 1` 保持不变，它表示协议版本，不表示软件版本。

## 注入链路

`src/main.py` 从 `src.static_adapter.TRANSPORT_ADAPTER` 获取浏览器适配器，并在 Gateway HTML 响应中注入。版本值由 Python 侧生成并替换适配器中的版本占位符，浏览器端只负责显示，不自行读取 Git 或包元数据。

状态栏标题保留 `OpenCode Mesh`，在同一标题区域增加版本文本，避免改变现有 P2P/Relay 指示逻辑。

## 安装与发布策略

- Git tag 使用 SemVer 风格：`v0.1.0`、`v0.1.1`。
- `scripts/install.sh` 支持 `MESH_VERSION`：
  - 设置为 `v0.1.0` 时从对应 tag 下载；
  - 未设置时保持从 `main` 分支下载，适合开发环境。
- 安装完成后打印实际安装版本。
- 首次落地创建 `v0.1.0` tag，与当前 `pyproject.toml` 版本对齐。
- 后续发布流程为：修改版本号 → 运行测试 → 提交 → 创建对应 tag → push 分支和 tag。
- 增加 GitHub Actions：在 push/PR 时运行测试；推送 `v*` tag 时额外校验 tag 与 Python/pyproject 版本一致。

## 测试方案

- 测试 `src.__version__` 与 `pyproject.toml` 的版本一致。
- 测试 HTML 注入结果包含当前版本，并且不会留下版本占位符。
- 测试适配器状态栏的版本文本存在。
- 运行现有 Mesh 可靠性回归测试。

## 非目标

- 本次不修改 OpenCode 自身 bundle。
- 不把协议版本字段改成软件版本字段。
- 不为离线页单独设计一套版本显示；离线页如需展示版本，后续复用同一版本解析函数扩展。
