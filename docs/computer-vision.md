# 桌面视觉与受控操作

2026-11-20。保持原版 Rakazo，Python Harness 在同一模型/工具/审批循环中执行；不是旁路视觉服务，也没有加入 Node.js 引擎。

## 与 Rakazo 对齐

核查 `packages/adapters/src/model-vision.ts` 和 `executor.ts`：Rakazo 按模型图片输入能力开放 computer_observe、computer_act、launch_app，并将截图交给执行模型。AgentForge 本轮支持这三个版本化工具；保留已有 open_path 的离线预览语义，预览后通过 computer_observe 看图，不能把 previewed 当作视觉审核。

| 能力 | 本轮范围 |
| --- | --- |
| 图片输入 | 部署路由通过 AGENTFORGE_MODEL_IMAGE_MODELS 显式列出模型；个人 OpenAI Compatible 连接使用原版 supportsImages 字段，按模型保存，不根据名称猜测。旧快照默认关闭。 |
| 观察 | 仅当前获授权 Bot/Team 桌面，固定截图程序运行在无网络容器。PNG 单图上限 2 MiB。当前桌面固定 1280×800，截取顶部 1280×776 应用区域，排除底部动态时钟；坐标原点不变，不支持点击被排除的面板。 |
| 动作 | 每批最多 8 个点击、受限按键、文字输入或滚动。先校验完整批次及可见画面摘要，再发送任何输入。画面变化返回 computer_screen_changed，需重新观察和新审批。动态页面可能频繁触发此保护。 |
| 应用 | 仅 browser / calculator 固定启动参数；镜像未安装时明确失败。无任意可执行路径、URL 或 Shell 参数。 |
| 审批与恢复 | computer_act / launch_app 复用原版 Allow / Deny。失效租约、取消、撤权或人工接管不能发布成功结果。确定的执行前拒绝记为 failed；发送输入后的未知结果保留 dispatched，禁止自动重放。 |
| 存储与上下文 | 图片保存在当前 Run 工具记录；聊天与审批检查点只保存元数据。发给模型前按活动租约读取该 Run 的最近两张成功截图，更早截图不进入请求。图片 Base64 仍保守计入请求字符/字节预算，不等价于模型视觉 token 数；超限明确失败。 |

部署需要同时启用 AGENTFORGE_COMPUTER_TOOLS_ENABLED=true、已有 workspace/Python 用户授权和视觉模型。工具总预算需容纳额外三个工具。含文字专用备用模型时不启用电脑工具，防止视觉任务切换到无法看图的模型。此开关默认关闭，本轮未自动替换本机 DeepSeek 路由。

## 独立 Qwen 验收

本机 `.env.vision` 被 Git 忽略，填写 AGENTFORGE_VISION_BASE_URL、AGENTFORGE_VISION_MODEL、AGENTFORGE_VISION_API_KEY。测试使用独立模型配置，不覆盖 `.env`，密钥不进入桌面与 Web。

```bash
uv run --env-file .env.vision --locked --offline python tests/interop/vision_probe.py
uv run --env-file .env --env-file .env.vision --locked --offline python tests/interop/workspace_model_smoke.py --vision --output /tmp/agentforge-vision-acceptance
```

第二条命令使用一次性 SQLite、账号、API/Worker/Supervisor 和隔离桌面；只批准该合成测试任务的操作，退出清理服务。它不代表原版 Web 的视觉工具交互已验收。首条已通过北京地域 qwen3-vl-plus 图片识别及 Function Calling 验证。

后续仍需原版 Web 视觉任务/审批/接管组合验收，以及目标 Ubuntu 验收。当前不包含示教录制、任意应用、拖拽、联网桌面、登录态或所有文档页面的自动质量审核。

## 工具参数纠正

新运行配置 AGENTFORGE_MODEL_MAX_TOOL_REPAIRS 默认 2，范围 0–2，旧快照默认 0。仅整批工具执行前的参数校验失败可回传模型修正；该批次中合法的兄弟调用也不执行。纠正消耗轮次，次数跨审批保留。不得自动修正已批准参数、权限拒绝、工具执行失败或未知副作用。

## 本版验收记录（2026-11-20）

- 最终离线全套 908 通过、5 项 PostgreSQL 专用测试跳过。
- PostgreSQL 全套 912 通过；随后截图保留增加一项回归，电脑专项 17 通过。
- Qwen 真实图片/Function Calling 探测通过。真实 API/Worker/Docker 桌面：launch_app → workspace_save → open_path → computer_observe → computer_act 全部成功，4 次审批，3 张截图；模型正确指出红色面板右侧裁切及翻页后的绿色面板。证据目录 /tmp/af-qwen-desktop-20261120-b。
- 初次桌面运行因测试附件未绑定被拒绝；后一次画面变化拦截，去除时钟和编码时间后动作通过；仅保留一张截图时最终比较断言失败，保留最近两张不同截图后通过。保留失败记录，不将其计为成功。
- P0-1 保留历史的 Team/Private 原版 Web 共 11 项检查通过。视觉增量本轮通过后端真实链路，尚未做原版 Web 的完整组合验收。
