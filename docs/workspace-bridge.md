# 电脑文件与聊天附件：Rakazo 对标

更新时间：2026-11-08。后端继续使用 Python Harness；Rakazo 源码不改动。本轮为文件链路增量，不代表通用 Computer 已全部对齐。

## 实现路径

普通聊天 → Worker 的同一 Harness → `list_files` / `attach_file` / `workspace_save` → API 桌面控制层按 Run / Attempt 派生身份 → Supervisor 选定 Team / Private 电脑 → 固定文件操作程序。

API 不运行生成脚本；Worker 不访问 Docker socket。文件操作复用既有电脑分配、控制互斥、工作区快照和恢复；没有第二套任务循环。Python 生成仍在独立离线临时容器完成，文件通过显式工具交接。

- `list_files(path="")`：列举当前 Bot 的目录，最多50项并受12KB元数据预算限制；超过时标记 truncated。内容属于资料，不能充当指令。
- `workspace_save(artifact_id, computer_mode)`：把本 Run 可读取的确定附件版本复制到 `artifacts/<artifact_id>/<原文件名>`。进入原版审批卡片；Team 卡片明确同空间成员可见。仅接受本 Run 冻结文件或本 Run 产物，不能据任意ID读取其他用户附件。匹配运行时固定的电脑模式。
- `attach_file(path)`：把当前 Bot 目录内的文件保存为新的私有聊天附件，返回 artifact_id。随后可作为 `python_execute` 的 inputs 继续编辑；桌面原文件保留。文件字节不进入模型上下文，工具、附件和消息在同一事务发布。

保存只使用固定目标目录、简单文件名和明确的文件ID；同ID、同字节重复保存可核对成功，如果电脑副本已被修改则拒绝覆盖。文件可通过原版电脑文件列表读取、下载，也可从聊天附件下载。没有自动弹出 PowerPoint 或宣称完成视觉审阅。

## 与本地 Rakazo 源码逐项对照

核查本地源码 HEAD `9218c401329b8626bc4d4b9ffde0034ea97a37f0`，原版 Web 兼容基线 `d165de43`。依据 `packages/adapters/src/builtin-tools.ts`、`executor.ts`、`computer-support.ts`。

| 项目 | Rakazo | AgentForge 当前 |
| --- | --- | --- |
| 文件目录 | `list_files` 列举 Bot home | 沿用工具名和列举行为，返回受限目录元数据 |
| 桌面文件发布 | `attach_file` 读真实文件，保留源文件并发布聊天附件 | 已接通相同行为，包括二进制 Office 文件；沿用原版 file 消息和下载 |
| 生成文件进入电脑 | `write_file` / `shell` 直接写持久工作区 | Python 临时执行产物经 `workspace_save` 显式复制，经过审批 |
| Team / Private | Team 默认 Bot home，还可用 shared/、bots/；Private 根目录 | 复用现有 Team 同空间电脑 / Private 用户与Bot隔离；Agent 文件入口限当前Bot目录，不开放跨Bot或全局shared/ |
| 物理路径 | Team workspace 子目录由 computer-support 解析 | 保留现有 `/workspace/<botId>`（Team）和 `/workspace`（Private），不改写已保存工作区 |
| 文件版本 | 命名 attach_file 按同名归入同一文件族 | Python修改按明确输入版本建立新版本；当前 attach_file(path) 每次创建独立附件，name/description 及同名归族仍待对齐 |
| 审批和恢复 | 既有审核 / 执行机制 | `workspace_save` 复用原版 ask 与既有操作台账，批准参数固定，未知写入不自动重发 |
| 电脑重启 | 持久工作目录和 checkpoint | 保存完成后同步 checkpoint；停止、重启后恢复文件，已做真实 Docker 验证 |
| 尚未对齐 | `read_file`、`write_file`、`shell`、`open_path`、图形应用 | 通用工具仍待推进；目前通过附件输入 Python 处理文件，没有渲染或桌面打开文件能力 |

## 权限和失败行为

新 Run 默认不开启。需要 Python 文件能力以及独立 workspace 授权，API 在提交去重后固定 workspace_enabled 和实际电脑模式，旧 Run 不获得新权限。现有工具已占满16项预算时不追加这3项工具，保留原有任务能力。执行前后及 Supervisor 回调检查成员、当前工具记录、权限、租约、取消和电脑代次。保存额外核对相同参数的 approved → dispatched 操作台账，不能拿服务令牌直接绕过审批。

Agent 操作与人工鼠标键盘接管互斥；接管期间返回错误。用户／空间隔离、相对路径、拒绝符号链接、输入输出每文件10MiB限制均保留。电脑文件并非私人附件库：Team电脑内的副本属于共享运行环境，未批准前不复制。

文件保存是外部副作用。若复制后连接断开、取消或持久快照失败，任务不会伪造成功，也不会盲目重放；磁盘上可能已有副本，需查看电脑文件核对。旧版 `workspace_save@1` 的审批记录复用不能自动恢复未知结果。已导入聊天的附件是独立不可变字节；电脑后续编辑不改变该附件。

## 配置与操作

保持原有 Python 镜像、用户授权、动态桌面池、AGENTFORGE_BROWSER_TOKEN 和 AGENTFORGE_BROWSER_API_ORIGIN。API 与 Worker 增加：

```dotenv
AGENTFORGE_WORKSPACE_POLICY_FILE=/absolute/private/workspace-policy.json
```

```json
{"grants":[{"tenant_id":"实际租户","space_id":"实际空间","users":["实际用户ID"]}]}
```

策略文件不得提交。API 与 Worker 读取；Supervisor 只通过已配置的内部服务令牌回查授权，不接收数据库或模型凭据。Compose 可放在已有只读 config 挂载内，`.env.backend` 指向 `/etc/agentforge/workspace-policy.json`。本轮无新迁移，数据库保持0018；升级 API、Worker、桌面 Supervisor，Python计算镜像和 Rakazo 前端无需重新构建。

手工用例：

1. 在已有 PPT 对话输入：`把刚才的 PPT 保存到电脑工作区，保留原附件。`
2. 核对审批卡片的 Team / Private 提示后允许；回复应包含 `artifacts/<id>/文件名.pptx`。
3. 进入电脑的文件列表检查文件；重启电脑后文件仍在。
4. 输入：`列出电脑上 artifacts 目录，把其中的 PPT 作为附件发回聊天。`
5. 再要求修改其中一页，验证仍能通过已有 Python 文件连续编辑链路交付新版本。

自动验证：`tests/test_workspace_bridge.py` 覆盖 Team / Private 完整 Harness、原版审批恢复、拒绝、服务授权、无审批直调拒绝、撤权、取消、租约、人工接管和安全路径。另跑全套 SQLite / PostgreSQL / 原版客户端测试。真实模型和独立 Docker 电脑已验证保存审批、电脑重启恢复、列举与取回，PPT往返字节一致。尚未将测试等同于渲染质量验收或生产容量验收。

## 接下来的对标顺序

1. 补 `read_file` / `write_file`，明确修改前版本和覆盖审批；补 attach_file 的命名与文件族语义。
2. 补 `open_path`、已安装应用打开文件和隔离渲染验收，再讨论通用 Shell 的审批与执行边界。
3. 将 Team shared/、bots/ 可见范围作为独立授权能力推进，不能借相对路径绕过当前 Bot 范围。
