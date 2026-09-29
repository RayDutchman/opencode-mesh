# V2 审查修复执行记录

**目标：** 修复全面审查中已复现的生命周期完整性、传输资源预算与并发限制问题。

**依据：** `docs/code-review-2026-09-29.md`。用户已批准按报告建议修复，并授权验证后直接提交、推送及部署；本记录用于跨会话交接。

## 边界

- 基线 `04adb39`，产品版本 0.3.2；只支持 OpenCode V2。
- 原 `feat/android-apk` 工作区继续使用；未提交 Android 源码、文档及 `.gitignore` 改动完整保留，不混入本次提交、不重打包 APK。
- 原审查会话继续实现，模型均为 GPT-5.6 Terra；按文件所有权并行，不互改文件。
- 不引入新依赖、不改变设备身份或业务请求归属，不重放结果未知的 mutation。
- 代码注释、Shell 输出使用英语；不在公开资料记录生产凭据。

## 执行清单

- [x] 生命周期组：三个脚本统一真实目录互斥；中断后回滚或保留恢复资料；修正文档升级、单 Gateway 卸载语义。
  - 所有权：`scripts/`、实例安装/发布及提示测试、README。
  - 红测必须包含源码替换后 SIGTERM、并发实例配置更新；补锁释放、独立目录隔离及恢复失败保留验证。
- [x] 浏览器组：入站单消息/全局字节、条目数、TTL 预算；取消、超时、非法编码/序号及正常结束释放装配。
  - 所有权：`src/static_adapter.py`、浏览器入站回归测试。
  - 红测复用真实适配器，覆盖无 final、未知 ID、解码失败、长流及连续 WS 逻辑消息；不以业务 ID 等同所有装配 ID。
- [x] 服务端组：WS 双向有界队列和单 sender、peer 原子槽位、响应逐跳头、重复 ID single-flight 与有界去重语义。
  - 所有权：`src/main.py`、`src/p2p.py`、可靠性及协议限额测试。
  - 红测包含慢消费者积压、并发协商超过限制、失败/取消释放、Connection 扩展头及重复任务取消所有权。
- [x] 主审：协调两端预算/协议兼容，检查正常大请求、SSE、PTY、锁屏恢复及取消行为；独立完整回归和代码审查。
- [x] 更新架构、协议、CHANGELOG 与维护交接，区分已修复、受限去重窗口及待真机验收项。
- [x] 仅提交本轮修复及记录，推送并部署 VPS、本机两个 Agent、ehang-box；核对实际 revision、服务状态和逐设备 API，保留 Android 工作。

部署完成：运行提交 `ea60ebe370fd8010201a6ad660ab063098c74c47` 已推送至 `origin/main`；VPS Gateway、ehang-box Agent、本机默认及 Windows Agent 均为 active，安装 revision 匹配。设备列表四台在线，各自 `/api/info` 均返回 200 JSON。PVE 未部署，APK 未重打包，Android 未提交工作保持原样。产品版本仍为 0.3.2，已有发布标签未移动。

## 验证命令

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider -W error::DeprecationWarning -rs
git diff --check
bash -n scripts/install.sh
bash -n scripts/upgrade.sh
bash -n scripts/uninstall.sh
```

另对提取的浏览器适配器执行 `node --check`。现有基线为 291 passed、1 skipped（缺少 Android API-35 工具链）；不把模拟测试等同真实 WebRTC/Android 全链路验收。

## 暂缓项目

主审最后完整验证：317 passed、1 skipped；三脚本语法、适配器 JavaScript 语法及 diff 检查通过。此处正常场景验证指既有行为回归，不替代新增真实手机/WebRTC 验收。

修复期间确认另有既存 offer 取消后暂留孤儿 peer 的生命周期问题；本轮原子槽位预留不解决它，待明确取消关联或期限策略。控制连接的重复 ID 处理没有改成结果缓存；single-flight 新修复针对 P2P 同通道活动请求。

Android 全工作树构建证明、完整 assets 输入校验及真机矩阵属于已披露的增强/验收项，随 APK 工作恢复处理。本轮不借修复范围引入登录页、V1 兼容或多设备 P2P 连接池。

## 第二轮：协商取消与控制请求防重（已批准，执行中）

用户批准仅修两项已确认缺陷，不修未经确认的问题，明确不实施双 P2P 连接池。沿用原服务端实现与审查会话，模型 GPT-5.6 Terra；验证后按 Agent → Gateway 顺序部署，保留 Android 工作。

- [x] Offer：真实 HTTP 断连与调用任务取消传播；内部取消关闭 peer，协商 task 已结束仍可按 session 取消，重复 offer 不覆盖原连接。
- [x] Agent：未建立 DataChannel 的有限期限清理；已 open 的空闲通道保留；reset 释放 session、watchdog、槽位。
- [x] 控制 request/stream：active 同 ID 不打断、不重复执行；completed/cancelled 有界 256 条、300 秒防重，不缓存响应正文；WS 生命周期独立。
- [x] 行为验证：直驱 ASGI disconnect、真实控制消息调度、类型冲突、late cancel、取消后重发、watchdog/open 竞态；审查发现已修正。
- [ ] 更新当前文档，选择性提交推送，先更新 Agent 再更新 Gateway；核对部署 revision、服务和逐设备 API。

首轮实现只覆盖 task.cancel 和墓碑容量，未达到交付条件。随后独立测试暴露发送 answer 失败后残留、已 open channel 未通知 ready、重复 offer 覆盖与 watchdog 完成记录残留，均先红后绿修复。真实 ASGI 断连使用消费完 body 后的 receive 监听；清理在 Starlette 取消 scope 中屏蔽重复取消，通知仍受两秒发送期限约束。HTTP/stream 墓碑阻止跨类型 WS 重用，返回 WS 自身的错误信封，不误用 HTTP response。未新增依赖（取消 scope 使用现有 FastAPI/Starlette 依赖 AnyIO），未实施连接池，未重打包 APK。
