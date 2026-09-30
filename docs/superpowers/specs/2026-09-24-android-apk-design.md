# Android 内置前端试用版

## 目标与授权

用户授权交付可安装 APK，使用 OpenCode 开源前端并内置资源，复用 Mesh 设备路由和 P2P/Relay；希望后台并行推进，有安装包后通知。此授权包含按本文边界自主完成实现与验证。

## 第一版边界

- 固定 OpenCode v2.0.15 源码版本，记录完整 revision 与许可证。
- HTML、JavaScript、CSS、字体等界面资源随 APK 分发，不通过远程首页冒充内置前端。
- 用户配置 HTTPS Gateway；不预置真实部署地址或凭据。
- 保留原生多 Server 界面，使用现有 Mesh 传输适配，不实现双设备 P2P 复用。
- 配置入口、返回导航、上传选择、错误恢复属于 Android 容器。
- 不升级、重启或修改现有生产 Gateway/Agent。
- 不承诺后台永不断线，不实现推送或资源热更新。

## 接入决策

采用 Android WebView 本地资源 HTTPS-origin 映射。仅对已配置 Gateway 的明确静态资源与页面导航返回 APK 内置字节，API、WebSocket、SSE 与 Mesh 控制请求保持真实网络语义；不得把失败的 API 当作 HTML fallback。Capacitor 的独立 origin 会要求调整现有同源传输和服务端 CORS，本版不采用。

在上游源码入口等待 Mesh bootstrap，并将原生 Server 基址指向明确设备；编译后资源不再使用 Gateway 的 JavaScript 正则改写。静态资源缺失返回本地 404，不自动加载服务端的另一版前端。

认证前只允许准确的 Gateway 设备发现 GET，暂时禁用页面 JavaScript；HTTP Basic 挑战完成后使用 WebView 的认证缓存。后续挑战不自动提供原生保存的密码。凭据使用 Android Keystore AES-GCM 加密保存，切换 Gateway 后销毁旧 WebView，旧回调不得影响新实例。WebSocket 对认证缓存的实际支持仍须真机复验。

## 验证与交付

先验证源码构建与最小容器，再验证设备发现、聊天、PTY、上传与网络恢复。安装包生成必须有构建、签名检查、资源清单和 SHA-256 证据。模拟器/真机未验证项分别说明，不把生成 APK 等同于完整功能验收。公开文档和样例使用虚构地址。

## 0.1.1 预览 UI 调整（2026-09-25，已获用户批准）

- 用户已实际安装首版并认可整体效果；本轮解决原生顶部按钮占用高度、与网页主题不一致、系统手势区底色不一致的问题。
- 移除独占高度的原生 Gateway / Reload 工具栏，在现有 Mesh 状态栏中加入主题一致的 `⋮` 菜单，保留设置与重新加载功能。
- 同步网页实际计算后的背景色与明暗到原生设置界面及系统栏区域，保持输入框、键盘和系统手势的安全区。系统手势指示条本身只调整明暗，不承诺任意着色。
- 网页未加载或认证失败时，仍提供可见的连接状态、重试和修改 Gateway 入口，不能把恢复能力藏进尚未加载的页面。
- APK 专用交互不得改变生产网页适配器或暴露通用 JavaScript 原生桥；保留认证与旧 WebView 回调隔离。
- 使用固定上游 OpenCode Web 的真实 logo 作为 App 图标，增加 Android launcher/adaptive icon 资源。沿用私有预览签名，提升 versionCode 以支持覆盖安装。
