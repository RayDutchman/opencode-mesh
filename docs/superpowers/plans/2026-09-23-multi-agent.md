# 同机多 Agent Implementation Plan

> 执行方式：主 agent 集成；安装生命周期与共享目录发布脚本由限定文件范围的子 agent 并行实现，最后统一审阅。

**Goal:** 正式支持独立身份的具名实例，并完成第二实例的未启动安装准备。

**Architecture:** 源码、虚拟环境和一份人工配置按安装目录共享；内部身份状态和 systemd unit 按实例隔离。升级是目录级操作，卸载是实例级操作。

**Tech Stack:** 现有 Python、Shell、systemd、pytest。

**Spec:** ../specs/2026-09-23-multi-agent-design.md

## Global Constraints

- 保留默认实例与现有身份；最初仅安装第二实例，随后用户明确授权升级后的第二实例启用验证；不修改上游服务。
- 公开示例虚构；不复制 device_id/agent_token，不输出认证信息。
- 不增加运行依赖；用临时目录和模拟命令进行破坏性路径测试。

## Review Focus

- disabled 但正在运行的实例也必须纳入共享升级。
- 其他安装目录的同名类型服务不可被重启。
- 新实例仅安装不能覆盖运行中的共享源码或改变其他实例配置。
- 单实例卸载不能删除共享代码及其他 inactive 实例。
- 多实例注册身份必须独立，重复启动不能重新生成身份。

## 任务与验证

- [x] 注册显示名：`src/main.py` 使用配置 `device_name`，缺省回退 hostname；`tests/test_agent_instances.py` 捕获真实注册载荷，验证两个状态文件和重启身份。RED→GREEN，全套 141 passed。
- [x] 统一配置运行时：`resolve_agent_config(path, cfg, instance)` 合并公共字段与实例字段，派生内部身份路径；`--instance` 选择实例，Gateway 拒绝该参数。旧单实例调用兼容，禁止具名实例复用旧单实例身份。新增 9 项 RED→GREEN，全套 150 passed。
- [ ] 安装/卸载：修改 `scripts/install.sh`、`scripts/deploy-agent.sh`、`scripts/uninstall.sh`；增加 `tests/test_instance_install.py`。统一实例参数、路径、安全名称与仅安装模式。临时目录 mock systemctl 验证独立单元、非法参数和卸载隔离。
- [ ] 共享升级：修改 `scripts/deploy-release.sh`；增加 `tests/test_instance_release.py`。枚举实际关联目录的单元，记录原运行集合，成功与回滚均只恢复该集合。通过 mock 执行 `--apply` 验证失败回滚及跨目录隔离。
- [ ] 集成审阅：检查两任务路径契约一致，README/maintenance/architecture 与脚本实际参数同步；全套 pytest、Shell 语法、compileall、diff 检查。
- [ ] 本机安装：迁移为统一人工配置并保留现有身份，使用正式安装接口增加 `windows` 实例；程序自动派生独立身份路径，不启用、不启动。确认现有默认实例 active、身份未变，第二实例 disabled/inactive，无注册副作用。

## 当前执行记录

用户后续实测确认 Windows PTY 终端正常。独立 connect-token 探针的 403 不代表真实浏览器终端故障；终端状态以用户实测通过记录。不同 V2 版本深链接懒加载资源 404 是独立、尚未修复的问题。本次用户授权提交、推送并部署到远端 Agent 安装目录，PVE 暂不操作。

后续启用验收：用户确认第二上游已升级到 OpenCode 2.0.15 并明确授权启用。认证信息从现有上游启动配置读取，仅写入被忽略的统一配置。两个 Agent 已 enabled/active，默认实例 PID 未变，内部 device_id 不同；通过公网 Gateway 分别读取两实例 `/api/info` 与 `/api/session` 均返回 200 JSON，版本分别为 2.0.6 与 2.0.15，两个前端入口资源均通过现有启动契约改写。

真实浏览器验收发现版本混用缺口：第二实例 P2P 通道可 open，但深链接页面的 Vite 懒加载依赖表包含 `_assets/...`，运行时产生裸 `/_assets/...` 请求，Gateway 默认上游缺少这些新版 hash 资源，出现 404 和应用错误页。只检查 pageerror 或 textbox 数量不能证明页面健康。浏览器临时改写探针因资源获取超时失败，尚不能证明修复有效；未部署猜测性产品修复。上游直接创建 PTY 成功，但独立 connect-token 请求在上游直连和 Relay 都返回 403 Invalid PTY connect token request，尚需核对 2.0.15 的实际浏览器请求契约，不能认定为 Mesh 转发故障。结论：双实例配置、注册和基础 API 隔离通过；2.0.15 完整页面、消息、终端验收尚未通过。

本轮集成：两名实现子 agent 均因供应商限流中断，主 agent 接管残留脚本与测试。安装/卸载、共享升级、迁移工具已实现，全套 178 项通过。独立审阅尚未完成，不将子 agent 的残留工作视作审阅通过。

最初安装阶段：本机使用正式迁移工具生成统一配置并通过正式安装脚本准备第二实例（当时 disabled/inactive）。默认服务单元指向统一配置，daemon-reload 前后 PID 相同，旧、新解析后配置完全相等；没有重启默认服务。当时第二身份文件未生成、上游仍为 V1。后续启用状态见本节开头；旧配置与服务备份保留。

Ruling: 新增实例不得改变已有公共 Gateway 配置；冲突时拒绝，而非覆盖公共字段 — 避免改变其他实例的归属 — 代价是管理员需先显式调整统一配置。

Ruling: 移除最后一个实例后仍保留空 agents 映射及公共配置 — 保留人工设置和身份备份 — 代价是完整卸载需明确使用 all。

开发基线 `7e72297`，产品版本 `0.2.1`。最初默认实例使用自定义 `config/agent.local.json`，后已保身份迁移至统一配置；不能以安装默认值覆盖现有配置。

Ruling: 具名服务可生成具体的 `@name.service` unit，不强制共用 `%i` 模板 — 避免不同安装目录互相覆盖同一个模板 — 代价是每实例一个很小的 unit 文件。

Ruling: 对已有共享安装目录，新增实例不更新源码和依赖，升级必须走目录级发布接口 — 避免活跃实例加载新旧混合代码 — 代价是安装新实例前需确保共享版本支持多实例。

Ruling: 用户进一步明确配置独一份，不要求消除程序身份持久化 — 改用 `config/agents.json`，不把身份写回人工配置，也不要求填写 state_file — 代价是旧单实例需一次保身份迁移。
