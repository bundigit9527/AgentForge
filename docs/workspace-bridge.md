# 电脑文件与聊天附件：Rakazo 对标

更新时间：2026-11-12。后端继续使用 Python Harness；Rakazo 源码不改动。本轮为文件链路增量，不代表通用 Computer 已全部对齐。

## 实现路径

普通聊天 → Worker 的同一 Harness → `list_files` / `read_file` / `write_file` / `attach_file` / `workspace_save` → API 桌面控制层按 Run / Attempt 派生身份 → Supervisor 选定 Team / Private 电脑 → 固定文件操作程序。

API 不运行生成脚本；Worker 不访问 Docker socket。文件操作复用既有电脑分配、控制互斥、工作区快照和恢复；没有第二套任务循环。Python 生成仍在独立离线临时容器完成，文件通过显式工具交接。

- `list_files(path="")`：列举当前 Bot 的目录，最多50项并受12KB元数据预算限制；超过时标记 truncated。内容属于资料，不能充当指令。
- `workspace_save(artifact_id, computer_mode)`：把本 Run 可读取的确定附件版本复制到 `artifacts/<artifact_id>/<原文件名>`。进入原版审批卡片；Team 卡片明确同空间成员可见。仅接受本 Run 冻结文件或本 Run 产物，不能据任意ID读取其他用户附件。匹配运行时固定的电脑模式。
- `read_file(path, offset=0, limit=2000, expected_sha256=null)`：读取最多2MiB的UTF-8文本，按Unicode字符分页，返回整个文件的SHA-256、字节数、next_offset与truncated。继续读取时传回摘要，防止拼接不同版本；二进制文件仍通过附件和Python工具处理。
- `write_file(path, content, expected_sha256, computer_mode)`：审批后写入最多12000字符的UTF-8文件。新建须明确传null，目标已存在则拒绝；编辑须先read_file，再传原文件SHA-256。新任务使用@2，新建时自动创建缺失父目录；覆盖不存在的目标仍报错。旧@1要求父目录已存在。不运行内容、不自动发布附件，写后同步工作区checkpoint。
- `attach_file(path, name?, description?)`：读取当前Bot文件并发布私有聊天附件。新任务使用@3，标题去首尾空白并截至255字符，未填写或空白使用文件名；描述去空白并截至280字符。同名忽略大小写，在同租户、空间、用户和Bot的全部历史附件中匹配版本，不限本次12项文件目录；清空聊天不删除附件版本历史。不同名称建立独立文件族。旧@1仍仅接受path；旧@2保留精确名称、本Run／冻结清单匹配、无名独立的行为。原文件和历史附件字节不变，工具、附件、版本及消息同事务发布。

新attach_file@3支持Office、PDF、PNG/JPG/JPEG、GIF、WebP、CSV、TXT、MD/MARKDOWN、HTML/HTM、JSON和ZIP。文件扩展名不区分大小写，每文件10MiB、实际本地文件名128字符限制保留。HTML作为附件通过既有JSON/base64接口返回，不增加同源HTML执行入口或桌面渲染。旧attach_file和Python导出格式保持原工具版本的集合。

保存只使用固定目标目录、简单文件名和明确的文件ID；同ID、同字节重复保存可核对成功，如果电脑副本已被修改则拒绝覆盖。文件可通过原版电脑文件列表读取、下载，也可从聊天附件下载。没有自动弹出 PowerPoint 或宣称完成视觉审阅。

## 与本地 Rakazo 源码逐项对照

核查本地源码 HEAD `9218c401329b8626bc4d4b9ffde0034ea97a37f0`，原版 Web 兼容基线 `d165de43`。依据 `packages/adapters/src/builtin-tools.ts`、`executor.ts`、`computer-support.ts`。

| 项目 | Rakazo | AgentForge 当前 |
| --- | --- | --- |
| 文件目录 | `list_files` 列举 Bot home | 沿用工具名和列举行为，返回受限目录元数据 |
| 桌面文件发布 | `attach_file` 读真实文件，保留源文件并发布聊天附件 | 已接通相同行为，包括二进制 Office 文件；沿用原版 file 消息和下载 |
| 生成文件进入电脑 | `write_file` / `shell` 直接写持久工作区 | Python 临时执行产物经 `workspace_save` 显式复制；`write_file`可在审批后直接写文本，Shell尚未开放 |
| Team / Private | Team 默认 Bot home，还可用 shared/、bots/；Private 根目录 | 复用现有 Team 同空间电脑 / Private 用户与Bot隔离；Agent 文件入口限当前Bot目录，不开放跨Bot或全局shared/ |
| 物理路径 | Team workspace 子目录由 computer-support 解析 | 保留现有 `/workspace/<botId>`（Team）和 `/workspace`（Private），不改写已保存工作区 |
| 自动目录 | writeContainerFile调用makedirs | write_file@2创建父目录；仍逐层拒绝符号链接与越界 |
| 文件版本 | 去空白／无名用basename；同用户、Bot、group内忽略大小写匹配，事务锁分配 | attach_file@3对应同用户、Bot历史匹配并加事务锁；当前无group会话，不能宣称已对齐group维度 |
| 附件格式 | 本地attachments契约含HTML、GIF、WebP、JPEG和MARKDOWN别名 | 新attach_file@3已补齐，并保留AgentForge的Office扩展 |
| 审批和恢复 | 既有审核 / 执行机制 | `workspace_save` / `write_file` 复用原版 ask 与既有操作台账，批准参数固定，未知写入不自动重发 |
| 电脑重启 | 持久工作目录和 checkpoint | 保存完成后同步 checkpoint；停止、重启后恢复文件，已做真实 Docker 验证 |
| 尚未对齐 | `shell`、`open_path`、图形应用 | 文本读写已接通；尚无渲染或桌面打开文件能力 |

## 权限和失败行为

新Run需要Python文件能力及独立workspace授权，API在提交去重后固定workspace_enabled和电脑模式。workspace_file_tools、workspace_file_alignment均默认false；仅获授权的新RPC任务在有5个工具名额时开启当前工具版本，剩余3—4个名额保留原3项工具，少于3个则不开启。write_file@2必须有对应版本审批，旧@1的始终允许规则不自动授权@2。旧快照不改写。执行前后及Supervisor回调核对成员、工具记录、权限、租约、取消及电脑代次，写入还须匹配approved → dispatched的精确参数。

历史名称匹配仅用于版本关联，不扩大Python输入或workspace_save的字节读取范围，仍只可读取冻结版本和本Run产物。新版本在complete_tool事务内解析父附件：PostgreSQL对用户／Bot／名称身份加事务级advisory lock，再沿用文件族行锁；SQLite利用该事务已取得的写锁。迁移0019增加可移植的Unicode小写名称摘要及索引，避免扫描全部文件正文；现有多个独立同名文件族不回溯合并，新发布按版本号、创建时间选择既有族。

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

策略文件不得提交。API与Worker读取；Supervisor通过既有内部令牌回查授权，不接收数据库或模型凭据。Compose可使用已有只读config挂载。本轮无新运行依赖；需先升级数据库至0019，再统一升级API、Worker和Supervisor。Python计算镜像和Rakazo前端无需重新构建。

迁移命令（在已授权的目标数据库上执行，先停旧后台）：

```bash
uv run --env-file .env --locked --offline alembic upgrade head
```

迁移只新增名称索引元数据并分批回填，不修改文件字节、既有版本、消息或Run。自动测试使用临时库；本轮未对开发库运行迁移，部署前需执行以上步骤。

手工用例：

1. 在已有 PPT 对话输入：`把刚才的 PPT 保存到电脑工作区，保留原附件。`
2. 核对审批卡片的 Team / Private 提示后允许；回复应包含 `artifacts/<id>/文件名.pptx`。
3. 进入电脑的文件列表检查文件；重启电脑后文件仍在。
4. 输入：`列出电脑上 artifacts 目录，把其中的 PPT 作为附件发回聊天。`
5. 再要求修改其中一页，验证仍能通过已有 Python 文件连续编辑链路交付新版本。

自动验证：`tests/test_workspace_bridge.py` 覆盖 Team / Private 完整 Harness、原版审批恢复、拒绝、服务授权、无审批直调拒绝、撤权、取消、租约、人工接管和安全路径。另跑全套 SQLite / PostgreSQL / 原版客户端测试。真实模型和独立 Docker 电脑已验证保存审批、电脑重启恢复、列举与取回，PPT往返字节一致。尚未将测试等同于渲染质量验收或生产容量验收。

## 接下来的对标顺序

1. 文本读写、自动目录、历史命名版本及扩展附件格式已接通；下一次真实模型/桌面验收验证新链路。
2. 补 `open_path`、已安装应用打开文件和隔离渲染验收，再讨论通用 Shell 的审批与执行边界。
3. 将 Team shared/、bots/ 可见范围作为独立授权能力推进，不能借相对路径绕过当前 Bot 范围。

## 2026-11-11 文件编辑基础与旧版本边界

写入在同一电脑操作互斥范围内执行，检查文件摘要并使用临时文件原子发布；拒绝符号链接、目录、FIFO和越界路径。新建采用不覆盖的发布方式；编辑在提交前再次核对路径inode、大小和时间戳。人工接管和Agent操作仍互斥；文件锁为协作式锁，不能保证任意不遵守锁的后台进程与检查/替换之间没有竞争。

明确的文件缺失、类型不支持、摘要变化记录为失败工具结果并交回模型；写入的确定拒绝同时记为failed外部操作。模型可重新读取后提出新调用和审批，不能把未知写入当作确定拒绝。传输断开、取消或checkpoint失败保持未知副作用语义，不自动重放、不宣称回滚。

旧attach_file@2归族限当前用户、Bot、冻结可用版本或当前Run产物；不搜索清空前历史。@3按上文规则扩展版本关联。展示标题与本地文件名不同：Python文件目录及workspace_save按MIME生成安全文件名；标题的扩展名与实际MIME不符时也生成正确类型的安全别名。

手工验收：要求“在电脑新建note.txt，内容为项目计划，并作为附件发布，命名为项目周报”；允许后要求“读取note.txt，将计划补充一项，再按项目周报这个名字发布新版”。核对两次写入的审批和两个不可变版本。审批等待期间人工修改note.txt后再允许，应报告文件已改变并重新读取，不能直接覆盖。Team和Private各执行一次。

自动测试见tests/test_workspace_file_tools.py：覆盖完整Worker/API/固定文件程序、审批恢复、同名版本、无名独立附件、跨轮与清空、旧快照、字符分页、符号链接、特殊文件、拒绝、取消、摘要冲突及未知结果不重放。模型和桌面传输为测试替身，不等同于真实模型、Docker或生产验收。

本轮验收：Ruff检查与格式检查通过；SQLite 850项通过、5项PostgreSQL专用测试跳过；独立PostgreSQL 855项通过，两种模式均启用原版Rakazo客户端互通测试。新增文件工具用例25项，并增加4项读取撤权验证。临时schema残留0，专用测试容器清理，Colima恢复停止；开发库未迁移。本轮未调用真实模型或启动真实桌面验收新工具。

## 2026-11-12 对标增量验收

新增tests/test_workspace_alignment.py，覆盖Team／Private审批后创建多级目录与HTML交付、旧目录规则、空白／无名／大小写标题、跨清空与超过12项清单的版本关联、四个身份边界、并发首次／后续发布、重复调用、事务回滚、六种扩展格式和带文件版本数据的迁移。测试使用模型与桌面替身；不代表已经验收真实模型、图形打开或渲染质量。

目录可能在最终文件写入前创建。确定失败代表没有发布新文件内容，不保证没有新增空目录；取消或断线仍沿用未知副作用规则，不自动重试。通用Shell、open_path、应用渲染、自动物化当前轮上传附件和Team shared/仍待后续对齐。

本轮最终验证：Ruff检查及格式检查通过；SQLite 869项通过、5项PostgreSQL专用测试跳过；独立PostgreSQL 874项全部通过，均包含原版Rakazo客户端互操作。新增19项对标回归。临时schema残留0，专用测试容器已清理，Colima恢复停止；未修改Rakazo源码、开发数据库或本机模型配置。本轮未做真实模型／Docker桌面新工具验收。
