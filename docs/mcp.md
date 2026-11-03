# MCP 远程只读工具

AgentForge 使用官方 MCP Python SDK（当前锁定 2.3.0）通过 Streamable HTTP 连接独立 MCP 服务，API / Worker / Harness 保持 Python。已有工具发现、Agent 绑定、运行时核对、调用与审计；默认关闭。未接入原版 MCP 配置页面；原版聊天能执行通过 REST 绑定的工具。

## 配置

复制 `infra/mcp/servers.example.json` 为根目录下被 Git 忽略的 `mcp.local.json`，根据实际身份范围修改。文件由部署管理员维护，不能由模型、Skill 或页面指定 URL。

```json
{
  "servers": [{
    "server_id": "products",
    "tenant_id": "local",
    "space_id": "development",
    "users": ["developer"],
    "url": "http://127.0.0.1:8765/mcp",
    "allow_insecure_http": true,
    "token_env": "",
    "read_tools": {"product_lookup": 1},
    "timeout_seconds": 15
  }]
}
```

在本机 .env 填入绝对路径：

```dotenv
AGENTFORGE_MCP_SERVERS_FILE=/absolute/path/to/AgentForge/mcp.local.json
```

HTTPS 为默认要求；本地演示显式开启 HTTP。真实服务通常将 token_env 设为 AGENTFORGE_MCP_PRODUCTS_TOKEN，并在 API / Worker 的环境中配置此变量；不要在 JSON 中写密钥。服务 URL 不允许内嵌凭据、查询参数或重定向，客户端不继承环境代理。HTTP 响应压缩暂不支持。

users 必须是显式用户 ID，不支持通配符。只有租户、空间和用户全部匹配才能发现 / 使用服务。read_tools 是管理员审定的只读、幂等工具及批准版本；远端还需声明 readOnlyHint=true、idempotentHint=true。这些声明不能证明工具实现没有副作用，不能作为自动授权依据。服务账号应在上游限制数据和操作范围；平台入口授权不替代服务端的数据行级权限。

API 和 Worker 使用同一代码版本、配置与身份范围；部署时需同步升级，旧代码不识别新增的快照字段。部署配置在启动时读取，调整授权或允许列表后需重启；不是动态授权中心。共享 Agent 不分享 MCP 服务授权。服务 ID、地址 / 凭据引用或批准版本变更后旧绑定可能被拒绝，需要重新发现并保存 Agent 新版本。

## 发现与绑定

带现有 REST Bearer 认证调用：

- GET /api/v1/mcp/servers：列出当前身份获准访问的服务与工具名称。
- GET /api/v1/mcp/servers/products/tools：通过 MCP 获取批准工具的定义，返回 server_id、tool_name、reference、input_schema、output_schema、sha256 等，不返回 URL 或密钥。
- POST /api/v1/agents 或 PUT /api/v1/agents/{agent_id}：使用 mcp_tools 绑定本次发现的定义。

创建请求示例（sha256 必须替换为发现接口的实际值）：

```json
{
  "name": "产品查询助手",
  "instructions": "根据查询工具结果回答，说明没有查到的内容。",
  "tools": [],
  "skills": [],
  "mcp_tools": [{
    "server_id": "products",
    "tool_name": "product_lookup",
    "sha256": "从工具发现结果复制完整的 sha256"
  }]
}
```

Agent 必须使用 Chat Completions 模型配置；mock 不能绑定 MCP，避免模拟模式意外访问远端。更新使用完整配置并附 expected_version；省略 mcp_tools 会清除新版本的绑定，已有 Run 不变。原版 bots.update 只改其他设置时保留 MCP 绑定。

API 保存无凭据的完整工具定义和路由指纹，Run 沿用版本快照。Worker 每次工具执行前重新检查当前范围和批准版本，并在独立 MCP 连接中重新发现、比对定义；定义变化返回 mcp_definition_changed，不静默升级。使用发现接口的新摘要保存新 Agent 版本后才采用变化。远端实现自身是否变化无法仅凭 Schema 检测。

本地工具与 MCP 合计最多 16 个，MCP 最多 8 个。Skill 的 required_tools 可以引用发现结果的 reference，但 Skill 本身不授予访问权限。工具参数和返回值作为普通数据处理，不执行安装命令或加载代码。

## 执行和错误

模型请求 MCP 工具 → ToolExecutor 校验参数 → 写 tool_started → 复核授权 / 定义 → tools/call → 验证结果 → 写 tool_completed → 把结果交回模型。沿用 /api/v1/runs/{id}/tool-calls 和 Run 事件查询，调用者只能访问自己的授权任务。

支持文本与 structuredContent；首版拒绝图片、音频、嵌入资源和资源链接，不自动抓取 URI。输入 / 输出 Schema 必须是 object；支持常用 Draft 2020-12 校验，但暂不支持引用、递归引用和正则表达式。每个 Schema 最多 16000 字符，JSON 深度 / 节点有限制，调用参数与结果受 20000 字符等现有限制。发现最多 8 页、256 个工具；单次 HTTP 响应最多 512000 字节，单次连接操作累计最多 2000000 字节。超限拒绝，不静默截断。

| 错误码 | 含义 |
| --- | --- |
| mcp_not_allowed | 当前范围、批准版本或只读 / 幂等条件不满足 |
| mcp_definition_changed | 已绑定路由或工具定义变化 |
| mcp_timeout | 连接、发现和调用的总等待超过配置时限 |
| mcp_unavailable | 连接、认证、协议或服务不可用 |
| mcp_invalid_response | 不支持或不合法的 Schema、内容类型、大小等 |
| mcp_tool_failed | MCP 工具明确返回 isError 等失败结果 |

HTTP 管理入口分别以 404、409、504、503 或 502 返回安全错误码；执行失败进入既有 Run 及工具记录。错误和 SDK 日志不回显远端正文、端点或凭据。正常工具结果可能包含业务数据，仍按原有权限保存在审计记录中。

调用期间检查取消、租约与成员权限；失效会停止本地等待、关闭连接并禁止写回结果，但不能保证远端停止查询或计费。不自动重发 tools/call，不自动执行输入补充 / 采样 / roots 请求。SDK 可以恢复 SSE 读取；Worker 崩溃重试仍可能重复查询、结果变化和计费，因此首版仅支持管理员审定的只读幂等工具。

## 手动测试

1. 在项目根目录执行 uv sync --locked 安装新增依赖。离线测试无需服务：uv run --locked --offline pytest tests/test_mcp.py -q。
2. 手动运行 uv run --locked python examples/mcp_catalog.py。演示只监听 127.0.0.1:8765，提供虚构产品 DEMO-001，不访问外部数据。
3. 配置 mcp.local.json 和 .env；确认真实模型配置已经可用，按原有开发命令重启 API / Worker。不要同时启动多个手动调试 Worker。
4. 用现有 Bearer 认证调用服务列表、工具发现接口，把返回的摘要绑定到 Agent。已有 Bot 可使用 REST 更新其 Agent ID，注意保存其他完整设置。
5. 创建会话并提问“查询 DEMO-001，按工具结果回答”，或在原版 Rakazo 页面选择已绑定的 Bot 提问。检查 tool_calls 中有 mcp_products_ 前缀的 succeeded 记录，结果包含“示例产品”。
6. 将允许用户或批准版本改掉并重启 Worker，验证旧绑定调用失败；普通聊天权限、历史记录和其他用户的任务仍保持隔离。

PyCharm 断点：McpService.resolve / check、exchange 的调用前核对、mcp_tools.definition 的处理函数，以及 ToolExecutor.execute。

本轮验证为离线 HTTP 响应与官方 SDK 的进程内 ASGI 服务（JSON 和 SSE），包含传统协议初始化与新版能力协商。尚未接入真实企业 MCP 服务或运行生产容量测试；未重启现有服务、未修改 .env 或真实数据库。

参考：[官方 Python SDK](https://github.com/modelcontextprotocol/python-sdk)、[Streamable HTTP](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)。
