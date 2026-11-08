# Python Harness 联网搜索与网页读取

对齐 Rakazo `d165de43` 的内置 `web_search` / `web_fetch`：模型可以自行选择关键词和来源，在同一次工具循环中搜索、读取正文并给出引用。无需新增前端页面、Node.js 后台、Computer 或搜索 API Key。真实推理仍使用部署或用户配置的模型。

这与 [可见浏览器](visible-browser.md) 是两条链路：联网工具不会在电脑面板打开 Chromium；需要展示页面加载、点击或由用户接管时，仍调用可见浏览器工具。

## 调用与记录

原版 `threads.send` / REST 提交 → API 将当前用户授权与搜索源固定到 Run → Worker 注册只读工具 → Python Harness → ToolExecutor / ToolJournal → 返回正文与来源 → 模型回答。Activity 沿用原版工具记录，参数、正文和凭据不进入 UI 活动摘要。

- `web_search(query, maxResults=5)`：关键词检索，最多 10 条标题、HTTPS 链接和摘要。
- `web_fetch(url, maxChars=8000)`：读取公开 HTTPS HTML、纯文本、JSON 或 XML，返回最终 URL、标题、正文、是否截断及抓取时间；最多 12000 字符，另有序列化结果预算。
- `retrievedAt` 是抓取时间，不是来源发布日期。即时排名、ETF 规模等必须核对正文的日期、范围和指标；搜索摘要不等于完整、实时的数据集。
- 超时、上游拒绝、内容不支持及大小超限保留失败工具记录，并将固定错误返回模型，由模型决定换来源或说明限制。取消、撤权和租约失效直接停止，不伪造成功。
- 对当前消息中明确的公开检索请求（例如“请检索今天的财经新闻”“帮我检索下今天ETF规模最大的一支”），且本次确有搜索授权时，第一轮使用命名 `tool_choice` 请求 `web_search`，后续恢复自动工具选择。避免旧会话反复声称“无法联网”时，模型仍无工具调用就结束。本规则保守匹配中英文请求，不是通用意图识别；含“不联网”、仅使用给定来源、本地／知识库请求、显式 Skill 或可见浏览器操作时不强制搜索。供应商忽略命名调用时明确报 `model_invalid_response`，不把无搜索答复保存为成功结果。

## 配置

API 与 Worker 使用相同配置。默认关闭；只给明确授权的用户启用，模拟模型不注册这些工具。

```dotenv
AGENTFORGE_WEB_ENABLED=true
AGENTFORGE_WEB_SEARCH_PROVIDER=duckduckgo
AGENTFORGE_WEB_POLICY_FILE=/absolute/path/to/web.json
AGENTFORGE_WEB_PROXY_URL=
```

`web.json` 格式沿用浏览器授权，但不需要浏览器令牌或桌面池：

```json
{
  "grants": [
    {
      "tenant_id": "your-tenant",
      "space_id": "your-space",
      "users": ["actual-user-id"],
      "origins": ["*"]
    }
  ]
}
```

替换为服务端真实身份。`*` 仅表示公开 HTTPS 来源，不包括私网或登录凭据；用户 ID 不支持通配。也可按精确来源授权，搜索站点、重定向站点和被读取来源均须允许。不自动为新注册用户授权。

`AGENTFORGE_WEB_POLICY_FILE` 留空时复用已有 `AGENTFORGE_BROWSER_POLICY_FILE`，不改变该文件中的用户范围。新 Run 固定来源权限；当前策略可以收紧，但不能扩展旧 Run 的权限。重复提交返回原 Run，不重新生成工具配置。历史任务不自动获得新工具，修改配置后重启 API 与 Worker，再发送一条新消息即可。

搜索源选项：

| 配置 | 上游 | 说明 |
| --- | --- | --- |
| `duckduckgo` | `https://html.duckduckgo.com/html/` | 默认，与 Rakazo 的免 Key 方向一致；网络或验证码可能导致失败。 |
| `bing_rss` | `https://www.bing.com/search?format=rss` | 可选免 Key RSS 搜索；可能重定向到 `cn.bing.com`，精确授权时须纳入。 |

本机 2026-11-06 验收使用 Bing RSS 直连；当前网络下 DuckDuckGo 直连超时，因此本地开发配置采用 `bing_rss`。搜索源是部署配置，不由模型临时指定，不自动绕过验证码或访问控制。

默认不读取系统 `HTTP_PROXY` / `HTTPS_PROXY`。确需代理时，将 `AGENTFORGE_WEB_PROXY_URL` 设置为显式 HTTP CONNECT 代理，例如 `http://127.0.0.1:7897`；不支持代理凭据。目标仍由 Worker 解析并验证为公网 IP，CONNECT 使用该 IP，TLS 验证原站点。Docker 内的 `127.0.0.1` 指当前容器，不能直接沿用宿主机代理地址。

Docker 交付时，将授权文件放在 `infra/delivery/config/web.json`，并在 `.env.backend` 设置 `AGENTFORGE_WEB_POLICY_FILE=/etc/agentforge/web.json`。现有 Compose 已向 API 与 Worker 挂载该目录；无需更改 Web、Supervisor 或 Computer 的凭据和网络配置。

## 当前边界

仅匿名 HTTPS GET，443 端口；不支持明文 HTTP、登录、表单提交、JavaScript 渲染、PDF、下载或专用金融行情接口。不能以这次改动宣称完整浏览器自动化已对齐 Rakazo。

每次解析全部 DNS 地址，拒绝任何私网、回环和链路本地地址；连接固定到验证过的 IP，TLS 验证原域名。每次重定向和读取块均重新检查取消／租约／权限。最多 4 次文档请求、1 MiB 响应，名义网络截止时间 15 秒、连接及读取超时至多 4 秒；系统 DNS 解析时间由运行环境决定。不转发 Cookie、Authorization 或上游 Set-Cookie，不自动加载页面资源。不接受压缩响应。

工具输出按 UTF-8 序列化字节数计算，避免中文被当作单字节低估。普通路线最多 18000 字节；有模型字节预算时，单份网页／搜索结果采用该预算的八分之一（下限 1024，上限 18000），给工具定义和多个来源保留空间。正文缩短或搜索条目减少时标记 `truncated`；连一个搜索条目都放不下时报告超限，不冒充“没有结果”。这是单次返回预算，不保证任意多次调用都能装入模型上下文；现有总体预算和轮次上限仍生效。

网页正文是外部不可信资料。隐藏脚本和样式不进入正文，页面中的指令不能改变授权。HTML 解析和输出均有大小限制；本版使用轻量正文提取，不等同于 Rakazo 的 Readability。工具描述要求查询中不包含密钥、私人身份信息或保密文档；这不是独立的内容防泄漏审查能力。

## 验证与手工测试

2026-11-06：联网专项 24 项包含正常工具循环、用户隔离、旧快照、权限收紧、取消、私网与重定向拒绝、输出限制、搜索页异常和失败恢复。全套 SQLite 689 项通过、12 项跳过（7 项原版客户端互操作、5 项 PostgreSQL 专项）；独立 PostgreSQL 701 项全部通过，包含原版客户端互操作。Ruff 检查通过。

真实 DeepSeek V4 Pro 在一次性 SQLite 和测试账号上自主调用 `web_search` → `web_fetch`，成功读取 Python 官方文档，返回来源链接；原版会话 Activity 投影通过。验收未写入开发账号、消息或数据库，也未启动 Computer。

同日旧会话回归修复：初次验收仅覆盖空会话、部署模型，未覆盖用户个人模型及多轮“无法联网”历史。真实问题在这条路径复现，即使收到工具列表和加强提示，模型仍零调用结束。修复后只读重放原有历史、使用同一个人 DeepSeek 连接，实际完成 1 次搜索和 2 次网页读取并返回结果；没有清空或改写开发会话。`tests/test_web_routing.py` 固定覆盖个人模型、旧历史、命名首轮、中文网页预算及禁止联网等反例。修复后全套回归：SQLite 704 项通过、12 项跳过，PostgreSQL 716 项全部通过（包含原版客户端互操作）。

可复用入口：

```sh
# 仅检查公开网络，不用模型或数据库。
AGENTFORGE_WEB_SEARCH_PROVIDER=bing_rss uv run --locked --offline python -m tests.interop.web_smoke

# 显式真实模型验收，会使用 .env 中的模型配置并产生模型费用。
# 验收内部清除开发库、桌面和工具授权，创建一次性数据库与独立用户。
uv run --env-file .env --locked --offline python -m tests.interop.web_model_smoke \
  --provider bing_rss --output /tmp/agentforge-web-acceptance.json
```

启用配置后，在原版 Web 发送：“请联网检索今天的财经新闻，读取来源正文，列出发布日期和链接。”检查 Activity 中实际出现搜索与读取工具；再刷新确认记录保留。也可用 Python 官方文档作为稳定样例。复杂金融数据的完整性仍需实际数据源验收；遇到网站限制时应报告失败或换可用来源，不能编造实时结果。
