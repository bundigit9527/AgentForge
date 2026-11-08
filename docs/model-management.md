# 原版 Web 模型管理

本轮接通 DeepSeek 官方接口和 OpenAI Compatible（Chat Completions）。沿用原版 Rakazo `d165de43` 页面，Python API 保存连接，Python Worker / Harness 执行聊天。没有引入 Node.js 执行引擎。

## 启用

1. 更新依赖：`uv sync --locked`。
2. 在本机 `.env` 或生产 `.env.backend` 配置 `AGENTFORGE_MODEL_ENCRYPTION_KEY`。使用 `uv run python -c "import secrets; print(secrets.token_urlsafe(32))"` 生成一次，保存到受控配置中。API 与 Worker 必须使用同一个值，不传给 Web、Supervisor 或 Computer。不要提交、随意重新生成或丢弃此值；数据库备份还需要独立保管对应的加密密钥。
3. 使用 `database` 身份模式，执行 `uv run --env-file .env --locked alembic upgrade head`，升级至 `0017`。迁移只新增模型连接、默认选择和备用顺序三张表，保留原有账号、聊天与文件。含连接记录时阻止降级，避免误删凭据。
4. 重启 API 与 Worker，启动原版 Web，打开 **Settings → Models**。未配置加密密钥时，连接操作返回明确的配置错误。

本机统一启动方式见 [P0 启动说明](p0-acceptance.md)。已有服务端托管路由仍可使用，个人连接可与托管路由并存。后端默认 mock 模式无需管理员配置模型 API Key，也可以由用户在页面连接个人模型；纯个人部署要将 `AGENTFORGE_MODEL_PROVIDER=mock`，避免 Worker 要求缺失的托管路由密钥。

## 页面操作

- **DeepSeek**：输入个人 API Key，发现可用模型，选择模型后连接。固定访问 `https://api.deepseek.com`，使用 `max_tokens` 并关闭 thinking。密钥、模型有效性在保存前通过一次小型 Chat Completions 请求验证；这会产生供应商侧请求与可能的费用。
- **OpenAI Compatible**：填写 Base URL（例如 `https://gateway.example/v1`，不含 `/chat/completions`）、模型 ID、可选 API Key、最大输出和上下文窗口。发送 `max_tokens`。可先读取 `/models`；供应商没有此接口时仍可手填模型并连接。只接收文本和工具调用，思考/图片选项启用时明确报错。
- **更换模型或限额**：地址不变时可不重新输入 Key；更换地址必须重新输入 Key，避免把原密钥发送到另一服务。新连接无 Key 时可接入明确配置的免认证网关。
- **默认模型**：按用户、空间保存，用于新建 Bot；已有 Bot 可在其设置中另选模型。连接成功会把该模型设为当前空间默认。
- **备用模型**：按顺序保存最多 10 个已连接模型。新 RPC Run 固定这一顺序；不会改写已提交的任务。
- **断开**：删除可用密钥并清理该用户在各空间的相关默认与备用选择。其他用户不受影响；绑定原模型的已有 Bot 需要重新选择模型。

每个用户在同一租户中每种 provider 保存一条连接。OpenAI Compatible 暂为单地址；同地址可先后验证并保存多个模型 ID。模型限制随该连接统一保存，后续变更产生新 revision。OAuth、其他原生供应商协议、多连接命名管理、思考与图片尚未实现。

## 执行与隔离

- AES-256-GCM 加密密钥，认证数据绑定租户、用户、连接、版本、provider 和配置。页面、快照、事件及错误都不返回密钥。管理员也不会通过模型接口读取其他用户的个人连接。
- 新 RPC Run 使用当前用户的连接并固定 revision、模型、请求预算与备用顺序。共享 Agent 不分享凭据。REST 会话仍遵守原有版本固定语义；本轮未新增 REST 模型管理接口。
- 更新或断开连接后，使用旧 revision 的任务会在下一次模型请求检查时以 `model_configuration_mismatch` 停止，应重新提交任务。不会将旧任务静默发送至新地址或新密钥。同步上游请求无法保证立刻中断；返回结果写入前再次检查。
- 备用只处理尚未输出文字的鉴权、限流、超时或服务失败；沿用同一 Harness 和工具记录，不重跑已完成的工具。本轮中切换后继续使用该备用。取消、撤权、连接变更、无效响应、拒答、预算超限及已经输出部分文字的失败不会自动切换。
- `model_completed` 记录实际成功响应的模型名；失败请求可能已计费但没有有效用量，不能据此作为完整计费账单。恢复进程可能再次请求模型，原有重复计费边界不变。
- 上下文窗口暂按“窗口减最大输出”的 UTF-8 JSON 字节预算保守限制，同时保留原字符上限。这不是供应商精确 tokenizer；按完整历史轮次裁剪，不截断本轮工具状态。较小备用模型无法容纳当前输入时明确失败。

## 地址与部署策略

个人地址默认只允许公网 HTTPS。API 的探测、连接测试以及 Worker 的请求均检查 DNS 并固定实际连接 IP，保留原 Host / TLS SNI，不跟随重定向，不使用环境代理。

内网或本地 HTTP 路由由部署方在 **API 与 Worker** 同时设置明确的 origin 白名单，例如：

```dotenv
AGENTFORGE_MODEL_ALLOWED_ORIGINS=["http://127.0.0.1:8900","https://gateway.internal:8443"]
```

origin 不含路径。白名单意味着允许用户把个人模型请求发至该 origin，应只列模型网关；Docker 部署中的 localhost 指当前容器。原有管理员托管路由继续按部署配置访问。

## 验证

原版备用列表在加载和保存前调用 `me({context: {spaceId}})`；后端兼容该输入，仅将它作为已认证空间的前置条件。请求体不能切换用户或授权空间；空间不一致时返回冲突，保留原版重试入口。

`tests/test_model_management.py` 覆盖原版客户端/Zod 契约、连接探测、默认和 Bot 选择、备用执行、加密、跨用户隔离、更新失败回滚、断开、旧 Run 和部分流式失败。`tests/test_model_endpoint.py` 覆盖内网阻断、DNS 固定、重定向及目录大小限制。模型响应使用受控 HTTP fixture；真实供应商连通性需要有效个人 Key，在页面连接时验证。
