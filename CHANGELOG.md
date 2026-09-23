# Changelog

## [Unreleased]

- 记录后续未发布的变更。

## [0.3.1] - 2026-09-23

- 无参数升级自动发现本机 systemd 安装，按目录选择并显示版本、提交及服务摘要，再确认操作。
- 相同已安装提交不重启；本机位置参数支持回环主机别名、相对路径及 `~`，非法参数给出明确原因。
- 升级成功或完整回滚后移除临时恢复资料，仅恢复失败时保留；不再产生长期源码备份。

## [0.3.0] - 2026-09-23

- 同机多个 Agent 共用统一配置，独立维护身份和服务单元，支持设备显示名。
- 共享目录升级保留原运行集合，失败回滚；按实例卸载保留其他实例和身份。
- 运维入口收敛为 install.sh、uninstall.sh、upgrade.sh，补充无参数交互式升级与卸载确认。
- 移除重复部署模板和辅助脚本，认证边界检查纳入 pytest。

## [0.2.1] - 2026-09-23

- Gateway 不再作为原生业务 Server；在 V2 启动前发现明确设备，定向迁移旧 origin 别名，保留用户名称与外部 Server。
- 为已验证的 OpenCode 2.0.6 入口加入集中启动适配及独立静态资源命名空间，设备发现不再延迟刷新已打开的页面。
- 大上传按限额增量探测，超限保留全部原始字节并按 Relay 消费速度继续读取；读取期间取消及时生效。
- 修复 WebSocket 在 CONNECTING 阶段关闭时悬挂，以及迟到消息重复清理的问题。
- Mesh 自行生成的错误返回 JSON 类型；流式请求首帧前错误保留 HTTP 状态，首帧后保持流中断语义。
- 补充 VPS 同机 Gateway 与独立 Agent 的部署边界及弱网终端调查记录。

## [0.2.0] - 2026-09-23

- 收敛到 OpenCode V2 原生多 Server，保留用户名称和外部 Server；设备发现只补充 V2。
- 保持浏览器 WebRTC 直连优先、Gateway Relay 兜底，无需客户端 VPN。
- 修复 Relay 重新编码后仍转发 Transfer-Encoding 导致空 400 和 UnsupportedContentType。
- 删除空 JSON/模型字段改写、V1 页面跳转及 XMLHttpRequest/EventSource 模拟。
- 修复 WSS 设备前缀、设备切换期间的通道绑定、取消与大 Request 回退。
- 在 V2 SDK 构造绝对 API URL 时保留明确 Server 基址，裸 origin 固定属于默认 Server，避免首页切换污染后台请求。
- 增加实际 Node 浏览器接口行为测试和 Agent HTTP 请求字节透传测试。

## [0.1.0] - 2026-09-22

- 增加 OpenCode Mesh 浏览器状态栏版本显示。
- 建立 `src.__version__` 单一版本来源和 SemVer 发布 tag 约定。
- 增加按发布 tag 安装、版本一致性校验和基础 CI 流程。
- 修复 P2P WebSocket 连续输入、终端关闭顺序和 RTT 误报问题。
