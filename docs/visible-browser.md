# Python 可见浏览器

对应 Rakazo `d165de43` 的 `browser-tools.ts` / `ComputerBrowserProvider`：工具应控制 Computer 上实际显示的 Chromium，而不是后台抓取后假装电脑已经操作。Rakazo 提供 navigate / snapshot / act（click、fill、type），工具入口未写死财经域名。AgentForge 以 Python 实现公开网页的导航、分段读取、查找、滚动和链接点击，尚未完整复刻这些动作。

## 已实现的链路

原版 `threads.send` → 固定当前用户的浏览器授权到 Run → Python Worker / ChatHarness → ToolExecutor / ToolJournal → 内部 API 解析当前 Run 身份并预约所属桌面 → Supervisor → Computer 内固定 Python CDP 驱动 → 原版电脑画面。API 只做授权和转发，不运行模型循环；Docker socket 仍只在 Supervisor。

- `browser_open(url)`：自动启动当前 Bot 的电脑，在可见 Chromium 的访客窗口打开 HTTPS HTML。
- `browser_observe()`：返回当前页 URL、标题、最多 4000 字正文及 12 条链接，含本次快照标识。
- `browser_read(snapshot, offset=0, links_offset=0)`：按位置继续读取正文和链接，返回 `next_offset` / `next_link_offset`；每次仍最多 4000 字和 12 个链接。位置按 Unicode 码点计数，中文和 emoji 不会把后续位置错开。
- `browser_scroll(snapshot, direction)`：在可见 Chromium 中向上／下滚动一屏，或滚到顶部／底部，返回实际滚动位置。滚动不自动翻正文分段，继续阅读需使用 `browser_read`。
- `browser_find(snapshot, text)`：查找第一个区分大小写的精确原文匹配，实际选中并滚动定位，返回匹配附近正文及 `match_offset`。未找到时返回 null；如果 `truncated=true`，只能说明已索引范围内未找到。
- `browser_follow(snapshot, link)`：使用刚返回的快照和链接编号，在真实页面滚动并点击链接。过期快照、跨 Run / Attempt 和电脑重建后的状态均拒绝。
- 使用既有 Run / Attempt、工具调用记录、Activity 和失败事件。原版 Web 的 Show tool activity 可由用户自行开启，默认值不改。
- Team 共用同空间电脑，Private 使用所属用户和 Bot 的电脑。不同任务不会读取上一任务的访客页面；Team 切换执行者会使旧页面快照失效。

## 显式配置

默认关闭；历史 Run 不自动增加工具。新 RPC Run 在已有浏览器授权下固定 `browser_reading=true`，才注册 read / scroll / find；缺失字段的历史快照默认 false，API 也会拒绝这些新动作。无需数据库迁移，不改旧快照。浏览器开关不是模型决定的，也不是用户输入一串 URL 就能授权。创建一个部署配置文件，例如：

```json
{
  "grants": [
    {
      "tenant_id": "local",
      "space_id": "development",
      "users": ["admin"],
      "origins": ["*"]
    }
  ]
}
```

`origins: ["*"]` 表示可访问公开 HTTPS 网站，避免把产品固定在财经网站名单。也可改为精确来源，如 `["https://www.python.org", "https://docs.python.org"]`；子域名与重定向目标必须分别允许。`users` 只能为显式用户 ID，不允许通配。以上身份仅为示例，替换为实际部署身份，不自动给新注册用户授权。

本地 API 的配置：

```dotenv
AGENTFORGE_BROWSER_POLICY_FILE=/absolute/path/to/browser.json
AGENTFORGE_BROWSER_API_ORIGIN=http://127.0.0.1:8000
AGENTFORGE_BROWSER_TOKEN=<用 openssl rand -hex 32 单独生成的值>
```

Worker、桌面池 Supervisor 使用相同的 `AGENTFORGE_BROWSER_TOKEN` 和可访问的 `AGENTFORGE_BROWSER_API_ORIGIN`，不需要模型接触该令牌。API 需要动态桌面池 `AGENTFORGE_DESKTOP_POOL`；旧静态单桌面配置不支持这条自动关联链路。令牌不能与 `SCREEN_PROXY_SECRET` 复用。`uv run --env-file .env` 显式加载环境；修改环境变量后需重启对应服务。

Docker 交付：将 JSON 放到交付目录的 `config/browser.json`；在 `.env.backend` 配置 `AGENTFORGE_BROWSER_POLICY_FILE=/etc/agentforge/browser.json`，在 `.env` 填写专用 `AGENTFORGE_BROWSER_TOKEN`。Compose 已传入三个后台服务并设置内部地址 `http://api:8000`，无需 Node.js 后台组件。

当前授权在原版 RPC 新建 Run 时加入快照。REST 创建的普通 Agent / 会话不会自动加入该能力。策略撤销逐操作生效；已有 Run 不追随新增的网站权限。模拟模型保持无浏览器工具。

## 执行边界和当前差异

- 仅匿名、无登录凭据的 HTML GET。Supervisor 逐次解析 DNS，拒绝私网、回环和链路本地地址，固定已解析 IP 并校验原网站 TLS；不使用环境代理，不转发 Cookie / Authorization / Set-Cookie。重定向仍检查范围，最多处理 5 次文档请求。
- Computer 仍使用 `--network=none`。HTML 由 Supervisor 受控传回浏览器；页面脚本、外部样式、子框架和外部图片被禁止，因此有的网站显示简化布局，依赖 JavaScript 的搜索页不能正常使用。不能把本版称为完整通用浏览器。
- 每份 HTML 最多 1 MiB；单页最多索引 200000 个 Unicode 码点和 480 个链接，超出时返回 `truncated=true`。达到末尾后下一段位置为 null。链接位置按页面原始列表计数，被授权策略过滤的链接可能使某一段不足 12 条。浏览器步骤有时间上限；冷启动电脑可能需要更久。网页文字是不可信资料，不授予额外权限。
- 每个步骤核对成员权限、Run 取消状态、当前 Attempt、有效租约、电脑 generation 和部署授权。用户接管时工具报 `browser_busy`；Agent 操作期间接管/停机请求明确报冲突，操作完成后即可接管。尚不支持强制抢占正在执行的 CDP 动作。
- 每台电脑同一时间只有一个 Agent 浏览器步骤。Supervisor 当前串行处理桌面命令；其他桌面请求可能排队。本次电脑画面授权使用该步骤的缓存绑定，避免被同一操作阻塞。
- 访客页面状态保存在 Supervisor 内存中；重启或挂起后需重新打开。无登录态持久化、表单输入、下载、发送消息或交易动作；这些需要后续实现操作策略和未知结果恢复。

真实模型联调与原版画面、接管、隔离验收见 [P0 记录](p0-acceptance.md)。启用 browser_reading 的 Run 遇到 browser_page_changed 时，Harness 保留失败工具记录并把固定错误提示返回模型，由新的 observe 调用恢复；不会自动重放原动作。授权拒绝、占用、取消与轮次限制不会被这项恢复绕过。

错误码包括 `browser_not_allowed`、`browser_busy`、`browser_page_changed`、`browser_unavailable`、`browser_failed`。不向模型或 UI 返回底层凭据或原始异常。

新动作必须携带最新快照，旧快照不能继续驱动操作。正文或链接变化会报 `browser_page_changed`；可用 `browser_observe` 重新观察当前页后重试，无需假装旧内容仍然有效。

## 手工验证

1. 配置真实 Chat Completions 模型、上述授权及动态桌面池，重启 API、Worker、Supervisor。
2. 在原版 Rakazo Web 新建任务：“请用电脑浏览器打开 https://www.python.org/，读取页面，然后点击 PSF 链接。给出最终页面标题和来源链接。”
3. 在电脑面板核对真实页面变化；开启 Show tool activity 后核对工具记录，刷新会话仍保留记录。
4. 用户接管电脑后发起同类任务，应返回占用错误；释放控制后发起新任务可运行。取消中的任务不能继续授权新操作。
5. 用另一用户、Private Bot 或新 Attempt 验证不能读取前一任务的快照。修改配置撤销授权后，旧 Run 后续步骤必须拒绝。

离线行为测试见 `tests/test_visible_browser.py` 和 `tests/test_browser_reading.py`。Docker 冒烟入口见 `tests/interop/visible_browser_smoke.py`；它只使用临时桌面、两个公开测试网站与模拟租约回调，不连接开发数据库或真实大模型。它验证真实 Chromium 的导航/读取/点击与回调撤权，API 身份链路和模型循环由离线测试独立覆盖。长页面验收入口 `tests/interop/browser_reading_smoke.py` 使用同一真实 Computer，但由测试代码提供确定的本地 HTML，不访问外网。覆盖中文与 emoji、第二段正文、第 61 个链接、可见查找与滚动、页面变化后的重新观察和截断提示。示例：

```sh
DOCKER_CONTEXT=colima uv run python tests/interop/browser_reading_smoke.py --image rakazo/computer:local --output /tmp/agentforge-reading-check
```

目标 Ubuntu 与真实模型选择工具的效果仍需部署验收。
