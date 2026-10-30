# 与 Rakazo 对齐的架构基线

更新日期：2026-10-30。参考已核查的 Rakazo 本地版本 `d165de43`（2026-10-15）；不代表远程最新版本。

AgentForge 以 Rakazo 的服务职责、执行链路、资源边界和恢复语义作为参考，以 Python 实现后端。现有企业系统接入、资产版本快照和 MIT 项目方向保留；React / Electron 客户端仍需适配新接口。

## 模块映射与现状

| Rakazo 概念 | AgentForge 对应 | 当前状态 |
| --- | --- | --- |
| API / 身份与资源服务 | FastAPI / contracts / storage | 已实现开发身份与资源接口；企业成员授权待补 |
| Worker / Run Executor | Python Worker / 任务存储 | 已有领取、续租、失效保护、Attempt；恢复语义待补齐 |
| Pi AgentRuntime | Python Harness / ChatModel | 已有模型和工具循环；流式输出、压缩、检查点待补 |
| Bot / Thread / Message | Agent / AgentVersion / Thread / Message | 已实现；版本固定为企业扩展 |
| Task / Run / Attempt | Task / Run / Attempt | Run、Attempt 已实现；独立 Task 待工作流阶段增加 |
| Builtin / AgentSkill / skill_read | 内置 Skill / 安装副本 / skill_read | 已实现目录、按需读取；Hub 安装是企业扩展 |
| ConnectorProvider / adapters | core 中的接口 / connectors 中的协议适配 | 已有 SkillHub 接口与 HTTP 适配；业务连接器待补 |
| ExternalEffect / approval checkpoint | 外部操作记录 / 审批 / 运行检查点 | 待实现；现有工具记录不可替代 |
| SandboxProvider / Supervisor / Computer | Python 环境接口 / Supervisor / Docker Computer | 目标边界一致，尚未实现 |
| MarkdownMemoryStore / MemoryProvider | Memory 模块与企业知识连接器 | 尚未实现，现有消息历史不等于长期记忆 |
| ArtifactStore / AgentHome | 文件产物与工作目录存储 | 尚未实现 |

Rakazo 代码参考：`apps/worker/src/index.ts`、`packages/adapters/src/executor.ts`、`packages/core/src/agent-skill.ts`、`packages/adapters/src/skill-tools.ts`、`packages/adapters/src/approval-effect.ts`、`packages/adapters/src/sandbox-factory.ts` 和 `packages/db/prisma/schema.prisma`。

## 必须保持的边界

- API 负责身份、资源、提交和事件；耗时 Agent 决策位于 Worker 中。
- Worker 承担执行协调；Harness 负责模型、上下文和工具循环。模型及 Skill 正文不决定访问权限。
- Skill 是操作方法和验收要求，Tool 是具体动作。先提供可用目录，再由 skill_read 读取选定技能；内置与安装技能共用读取路径。
- 上游广场是资产来源，本地保存明确版本。Hub 通信限于 API 的连接器；Worker 执行安装副本，不在运行中追随远程 latest。
- 工具请求统一经过权限、校验和执行记录；未来外部写操作须补审批、独立幂等键与不确定结果处理，不从模型请求重放推断成功。
- Computer 只执行脚本、文件和浏览器动作；只有 Supervisor 管理 Docker。API、Worker 和 Computer 不持有 Docker socket。
- 记忆、知识与文件独立持久化，并按资源权限访问。共享 Worker 或同一 Bot 目录树不构成强隔离。

## 明确保留的差异

- Python 技术栈；首版数据库任务表兼任队列，不为复刻 Graphile 引入另一套组件。
- Agent 和运行配置固定版本；Hub 安装固定来源、版本和内容摘要。升级技能需要新 Agent 版本，旧会话不静默变更。
- 当前先由 Agent 显式选择可见技能。Rakazo 还提供用户级目录、快捷指令、创建和编辑技能等体验，本轮不声称已覆盖。
- 当前使用 Markdown 正文加元数据保存技能，skill_read 输出 SKILL.md；完整 YAML 导入、插件资源包、自建技能编辑界面仍待建设。
- 不复制 Rakazo 的所有产品模块；移动端、语音、外部聊天渠道等未列为当前交付范围。

## 后续验收顺序

1. 企业身份、空间成员与资源共享；以两个身份验证资源授权。完成企业模型路由联调。
2. 审批暂停/恢复、外部操作记录与检查点；验证崩溃后不盲目重复业务操作。
3. Supervisor / Docker Computer / 文件产物；验证授权工作目录、资源限制、取消与回收。
4. 接入已有广场和知识系统，并增加 Task 与简单工作流步骤；完成“取资料 → 分析 → 报告 → 人工确认”。工作流在执行核心之上编排，不替换 Harness。
5. 最小 Web / 桌面交互及 Ubuntu 容器化交付，完成监控、备份与恢复演练。

此处是实施基线和差距清单，不表示所有 Rakazo 能力已经完成。
