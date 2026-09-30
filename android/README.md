# Android 内置前端预览 APK

本目录把 OpenCode 开源前端（固定 v2.0.15）的界面资源直接打包进 Android APK，
通过 Mesh 传输适配器连接用户配置的 HTTPS Gateway。容器是 Android 系统提供的
WebView，不依赖 Gradle、不引入第三方 Android 库；界面资源随 APK 分发，不通过远程
首页冒充内置前端。

本包是**本地预览用途**，不是正式发布产物：业务请求始终属于用户配置的明确 Gateway
设备，Gateway origin 不另行充当业务 Server。

## 边界与行为

- 内置资源仅在本 APK 内部使用，服务端不参与分发；`index.html` 注入
  `mesh-transport.js` 与 `mesh-ui.js`，API、WebSocket、SSE 与 Mesh 控制请求保持真实网络语义。
- `mesh-transport.js` 的状态栏（`#ocm-mesh-bar`）右侧由 `mesh-ui.js` 追加 ⋮ 菜单
  （Gateway settings / Reload），样式只使用网页 v2 主题 token（`var(--v2-*)`），
  不硬编码表面颜色。
- 页面与容器之间不使用 `addJavascriptInterface`：`mesh-ui.js` 只通过受限的
  `ocm-app://` 主帧导航与容器通信（settings / reload / theme），Java 侧校验
  “当前 WebView + 主帧 + priming 已完成（本地 UI 就绪）+ 页面仍为配置的同一
  Gateway origin”后才执行动作；任何 frame 的自定义 scheme 导航一律拦截，
  不会启动外部应用或进入错误页。
- 主题回传只含 `r/g/b/a/dark` 整数，绝不携带凭据；颜色由浏览器计算
  （内嵌 var()/oklch 链经 canvas `getImageData` 归一化），不把原始 token
  当作 hex。容器据此同步窗口底色、系统状态/导航栏与明暗图标，并让原生
  Gateway 对话框跟随主题；Android 15（target 35）边缘到边缘下系统栏透明，
  由窗口背景色承接页面颜色，手势导航 pill 颜色属系统行为，不由应用控制。
- 首次配置、连接探测（priming）或失败时，界面顶部有原生的可见连接状态视图
  （含 Retry 与 Gateway settings），不依赖尚未加载的网页菜单。
- 打包构建禁用上游 service worker 与 sourcemap；本地静态资源按 SHA-256 清单
  （`bundle-manifest.json`）记录。
- 认证按 origin 隔离，凭据只用于首屏业务发现（`/_mesh/devices`）的一次性播种，
  后续挑战不会自动携带存储凭据；凭据经 `CredentialStore` 本地保存，不写入公开配置。
- 外部链接交给系统浏览器；`asset` 缺失返回 404，不回落到网络冒充 HTML。
- 选择器、返回导航、上传选择与错误提示属于 Android 容器（见 `MainActivity.java`）。

## 环境要求

| 组件 | 版本/说明 |
|------|-----------|
| Android 系统 | Android 8.0+（`minSdkVersion 26`，`targetSdkVersion 35`），使用系统最新 WebView |
| JDK | 11（`javac` 以 `--release 8` 编译容器字节码） |
| Bun | 1.3.14（构建内置前端用，`bun run build`） |
| Android SDK | platform 35（`android.jar`）+ build-tools（含 `d8`/`aapt2`/`zipalign`/`apksigner`） |
| Python | 3.11+（构建脚本） |

构建链路不经过 Gradle：`javac` → `d8`（`--min-api 26`）→ `aapt2 compile`/`link`（含
launcher icon 资源）→ `zipalign` → `apksigner`。

## 构建步骤

### 1. 准备固定版本的 OpenCode 源码

源码固定提交为 `6f3639d82ed0760091792189b78f8eeb44f699b1`（v2.0.15），
`build_frontend.py` 校验版本和 5 个入口文件的完整 SHA-256；只接受原始内容或本脚本适配后的内容。
克隆上游 OpenCode 公开仓库后检出该提交，再安装依赖（固定锁文件、跳过安装脚本）：

```bash
git clone https://github.com/anomalyco/opencode.git <source-dir>
cd <source-dir>
git checkout 6f3639d82ed0760091792189b78f8eeb44f699b1
bun install --frozen-lockfile --ignore-scripts
```

### 2. 构建内置前端资源

```bash
python3 android/build_frontend.py --source <source-dir> --output <assets-dir>
```

- `--source`：上一步的源码目录。
- `--output`：内置资源输出目录，**必须是全新目录**（已存在会报错）。
- 过程：在源码边界做最小 bootstrap 适配（`platform.web.ts` 的 serverUrl、`entry.tsx`
  等待 bootstrap、关闭 service worker/sourcemap、`index.html` 注入
   `mesh-transport.js` 与 `mesh-ui.js`），执行 `bun run build`，然后打包 `dist` 为
  `web/`、生成 `web-assets.txt`、`bundle-manifest.json`，并随包复制上游
  `LICENSE`（`LICENSE-OpenCode.txt`）。
- 可选 `--prepare-only`：只做源码适配，不执行构建与打包。
- 如果使用固定提交的 codeload 压缩包，可加 `--source-tar <archive.tar.gz>` 校验原包 SHA-256。该检查验证传入压缩包，不能代替检查源码目录中其余文件是否被手工修改。
- 包中同时携带 OpenCode、KaTeX、IBM Plex 许可和 `android/notices/` 中的字体声明。

### 3. 构建并签名 APK

```bash
python3 android/build_apk.py \
  --tools <sdk>/build-tools/<版本> \
  --platform <sdk>/platforms/android-35 \
  --assets <assets-dir> \
  --output <apk-out-dir> \
  --keystore <私有持久路径>/mesh-preview.jks
```

- `--tools`：Android SDK build-tools 目录（含 `d8`、`aapt2`、`zipalign`、`apksigner`）。
- `--platform`：Android SDK platform 35 目录（含 `android.jar`）。
- `--assets`：第 2 步的输出目录。
- `--output`：APK 输出目录，**必须是全新目录**。
- `--keystore`：**私有持久路径**下的预览签名 key。不存在时自动用 `keytool` 生成
  （别名 `mesh-preview`，有效期 3650 天，权限收紧为 600）。

### 4. 输出产物

APK 文件名从 `AndroidManifest.xml` 的 `versionName` 派生（当前为
`0.1.1-preview`），构建脚本不重复维护版本号；`versionCode` 同样只存在于
Manifest，升级采用同一预览 key 时递增以便覆盖安装。

- `opencode-mesh-<versionName>.apk`：最终 APK（如 `opencode-mesh-0.1.1-preview.apk`）。
- `opencode-mesh-<versionName>.apk.sha256`：SHA-256 校验文件。
- `unsigned.apk` / `aligned.apk` / `classes/` / `dex/` / `res.zip`：中间产物（fresh 目录内）。

构建脚本会以 `apksigner verify --verbose` 与 `aapt2 dump badging` 打印签名与
包信息作为证据；输出中包含 SHA-256 摘要。

## 安装

把 APK 拷贝到 Android 8+ 设备（允许"安装未知来源应用"），或使用 ADB：

```bash
adb install -r opencode-mesh-0.1.1-preview.apk
```

同一预览 key 的后续构建可直接覆盖安装，无需先卸载。

## 首次使用：配置 HTTPS Gateway

1. 打开 App，界面顶部显示连接状态；点 **Gateway settings**（或首次自动弹出的配置对话框）。
2. 输入 HTTPS Gateway 地址（纯 origin，不带路径，如 `https://mesh.example.com`）、
   用户名与密码。
3. 应用先做一次业务发现（`/_mesh/devices`）探测连通性与认证；连接期间顶部状态视图显示
   “Connecting to …”，失败或超时会给出原因并提供 **Retry** 与 **Gateway settings**。
   成功后加载 `https://mesh.example.com/`，界面资源全部来自 APK 内置 `web/`。

地址必须满足：`https` 协议、无路径、无内嵌凭据；错误输入会被拒绝并要求重新输入。
加载完成后，状态栏（`#ocm-mesh-bar`）右侧的 **⋮** 菜单可随时打开 **Gateway settings**
（重新配置并重连）与 **Reload**；首次连接失败时菜单不可用，改由顶部原生状态视图的
Retry 重试。初次设备发现等待上限为 15 秒，超时会在状态视图提示检查地址和网络。

WebView 会缓存 HTTP 认证。同一 Gateway 修改账号或密码后若仍使用旧身份，可强制停止 App 再打开。Relay 终端的 WebSocket 是否使用该认证缓存，需要在实际手机的 WebView 上验证；桌面浏览器检查不能替代此项。

## 签名说明

`--keystore` 指向私有持久路径，同一 key 用于本机所有预览构建，后续版本可覆盖安装。
此 key 仅为**本地预览签名**，不是正式发布签名身份；正式对外分发前需更换为受控的
发布签名 key 并重新签署。

## 本地验证状态

- APK 本地构建成功，`apksigner verify` 签名校验通过，`aapt2 dump badging` 包信息正确，
  SHA-256 已生成。
- 桌面 Chromium 以本地资源拦截模拟本容器行为 + 真实 Gateway 首页，73 个资源本地命中、
  0 缺失、0 page errors。以记录器替代原生 scheme 导航，验证了菜单设置/重载动作以及
  深浅背景变化后的主题回传；此项不等同于 Android WebView 原生导航回调验收。
- APK UI 层在本仓库以 Node（DOM/Canvas shim 驱动 `ui.js`）与 JVM（`AppScheme`
  契约 + API-35 android.jar 编译门）行为测试覆盖：菜单、受限 scheme bridge、
  v2 token 样式、浏览器计算的主题回传、主题切换去重、无凭据载荷。
- 用户已安装并认可 0.1.0 预览版的整体效果。**本次 0.1.1 未由开发侧在真实 Android
  设备/模拟器安装验收**：⋮ 菜单、主题同步、安全区与键盘等待真机复验；设备发现、
  聊天、PTY、上传与网络恢复也未逐项完成 Android 验收。生成 APK 不等于完整功能验收。
- 仓库全套回归当前为 251 项通过（包含 JVM 资源/认证入口边界与源码锁定、构建调用、
  许可打包、APK UI 层行为测试）；该结果来自合并工作区后的独立完整复跑。

系统栏处理：API 30+ 显式启用 edge-to-edge 后统一消费系统栏、刘海和键盘 insets；
API 26–29 使用旧版 decor 与 `fitsSystemWindows`/`adjustResize`，避免引用新版本的
`WindowInsets.CONSUMED`。系统手势区背景跟随网页，指示条明暗仍由系统控制。

## 相关文件

| 文件 | 作用 |
|------|------|
| `build_frontend.py` | 固定版本源码适配 + 前端构建 + 资源清单打包（注入 `mesh-transport.js` 与 `mesh-ui.js`） |
| `ui.js` | APK 专用 UI：`#ocm-mesh-bar` 右侧 ⋮ 菜单 + 主题回传（打包为 `web/mesh-ui.js`） |
| `build_apk.py` | 无 Gradle 的 APK 组装、zipalign 与签名 |
| `app/src/main/AndroidManifest.xml` | 清单：minSdk 26 / targetSdk 35、网络权限、无明文流量、launcher icon 与版本号 |
| `app/src/main/res/` | launcher/adaptive icon（API 26+），前景取自固定上游 `favicon-v3.svg`，随 `LICENSE-OpenCode.txt` 同源许可分发 |
| `app/src/main/java/dev/opencodemesh/app/` | WebView 容器、`AppScheme` 受限 bridge 契约、资源拦截策略、凭据存储 |

设计边界与待验证决策见 `docs/superpowers/specs/2026-09-24-android-apk-design.md`。
