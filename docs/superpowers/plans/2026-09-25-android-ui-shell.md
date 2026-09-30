# APK UI 层实现报告（2026-09-25）

> 与 logo worker 并行的独立工作项；主 agent 合并两项后统一构建。本报告
> 说明本项改动、验证证据、检查/限制与合并线索。写入均完成后
> `stat` 复核，未受此前 ENOSPC 事故影响。

## 授权范围与约束

- 只改 `android/app/src/main/java/` 下 Java、新增 `android/ui.js`、
  `android/build_frontend.py` 集成与相关行为测试（`tests/test_android_ui.py`
  新增、`tests/test_android_frontend_build.py` 扩展）。
- 不改 `src/static_adapter.py` 生产网页行为；不动 Manifest、`build_apk.py`、
  `res/`（属于 logo worker）。
- 不提交、不推送、不部署、不重新打包；不改私有数据、不派 agent。
- 保留认证 priming 精确 GET、JS disabled、15s deadline、旧 view 守卫/无
  mutation 重放、文件选择器语义。

## 改动清单

| 文件 | 内容 |
|------|------|
| `android/ui.js`（新） | ⋮ 菜单 + 主题回传；只经 `ocm-app://` 主帧导航与容器通信；无 `addJavascriptInterface`、无 prompt/eval 桥；菜单样式只用 `var(--v2-*)` token；颜色经 canvas `getImageData` 浏览器计算 |
| `app/.../AppScheme.java`（新） | 纯 JVM bridge 契约：严格解析 settings/reload/theme、上下文门禁 `gate()`、alpha 合成 `composite()`；载荷只含 r/g/b/a/dark 整数 |
| `app/.../MainActivity.java` | 删除独占高度原生工具栏；新增原生连接状态视图（首次配置/priming/失败 + Retry/Gateway settings）；`shouldOverrideUrlLoading` 拦截 `ocm-app://` 并按门禁执行动作；主题应用到 window/系统栏/明暗图标/对话框；insets 适配安全区（状态栏/刘海）与键盘 |
| `android/build_frontend.py` | `prepare()` 原样打包 `ui.js` → `public/mesh-ui.js`；index.html patch 追加 `<script src="/mesh-ui.js">`，更新 pinned patched hash 为 `12baf8b32efc9f4f07902ccf7f82992bd70d515ff949eaafabd05fd680721a6c`；`package()` 必含 `mesh-ui.js` |
| `tests/test_android_ui.py`（新） | 10 个 Node/VM 行为测试 + AppScheme JVM 测试 + API-35 android.jar 编译门 |
| `tests/test_android_frontend_build.py` | 适配 mesh-ui.js：dist fixture、manifest 文件集、prepare/index 断言 |
| `android/README.md`、`docs/maintenance.md` | 行为文档与交接记录（本文件为独立报告） |

## 验证证据

- 红阶段：`test_android_ui.py` + `test_android_frontend_build.py` 首轮
  `19 failed, 8 passed`（ui.js/AppScheme 缺失、pin 未更新等按预期失败）。
- 绿阶段：目标 27 项全绿；`node --check android/ui.js`、`compileall`、
  `git diff --check` 通过。
- 全套：`.venv/bin/python -m pytest tests/ -q -W error::DeprecationWarning`
  → **251 passed**（含 logo worker 的 `test_android_apk_build.py`）。
- JVM：`AppScheme` 编译并在 JVM 执行（scheme 解析、门禁、合成取整
  `ffff7f7f`）；全套 Java 以 `javac --release 8 -classpath android.jar`
  编译通过（与 `build_apk.py` 同参数）。

## 检查与限制

- 桥接门禁：`view == web`（当前实例）＋ `request.isForMainFrame()` ＋
  `!priming`（本地 UI 就绪）＋ `policy.sameOrigin(view.getUrl())`（固定
  Gateway 同源）；任意 frame 的 `ocm-app://` 一律拦截（返回 true，不会
  进错误页或拉起外部应用）。
- 主题回传不暴露凭据：ui.js 无凭据字段；Java 不把密码注入页面；载荷键
  白名单 r/g/b/a/dark 由测试与 `AppScheme` 双重约束。
- target 35 手势背景：Android 15 边缘到边缘下系统栏透明，`window` 背景色
  承接页面颜色；<35 设备显式 `setStatusBarColor/setNavigationBarColor`；
  图标明暗经 `WindowInsetsController`（R+）/`SYSTEM_UI_FLAG_LIGHT_*`（<R）
  切换；**不承诺任意设置系统手势 pill 颜色**（系统行为）。
- 真实设备未验证：⋮ 菜单渲染、自定义 scheme 拦截（依赖具体 WebView
  版本）、主题同步/insets/键盘、对话框跟随主题均待真机复验。
- 构建衔接：`/tmp/opencode/android-source-v2.0.15` 为旧 pin 产物，
  `prepare()` 对新的 index.html pin 拒绝（reject 发生在任何写入之前）；
  统一构建需从固定 archive 重新 prepare。未执行 bun build、未重新打包
  APK（授权范围内不做）。
- 环境：根分区 100% 满（约 2.9G 可用），临时物放 `/tmp/opencode`
  （tmpfs，11G）。

## 合并线索

- 本项文件与 logo worker 无重叠（对方：Manifest、`build_apk.py`、`res/`、
  `test_android_apk_build.py`、README 部分段落）；README 的“相关文件”表与
  “本地验证状态”两处已按双方内容合并。
- 最终测试数以合并后 `git status` 前后一致为准；README 中 251 的数字须在
  合并后复跑核对。