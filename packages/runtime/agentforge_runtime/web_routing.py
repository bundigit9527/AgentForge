"""Require the first search only for explicit, current-turn public-web requests."""

import re


def requests_public_search(prompt):
    text = prompt.strip().lower()
    # Never infer authorization from history, quotations or a supplied document.
    # Ambiguous requests stay with normal model tool selection.
    if re.search(
        r"(?:不要|不用|无需|禁止|不必|别).{0,12}(?:联网|检索|搜索|上网)"
        r"|(?:do not|don't|without|never).{0,16}(?:search|brows|internet|web)"
        r"|(?:仅根据|仅使用|只用|只根据|only use|provided sources)"
        r"|(?:知识库|本地|附件|私有|内网|仓库|项目代码|上传|文中|资料中|local files|codebase)",
        text,
    ):
        return False
    chinese = re.match(
        r"^(?:(?:请你|请|帮我|帮忙|麻烦你|麻烦|能否|可以)\s*)*"
        r"(?:(?:联网|上网)(?:帮我)?(?:搜索|检索|查询|查找|查)?|搜索|检索|查询|查找)",
        text,
    )
    english = re.match(
        r"^(?:(?:please|can you|could you|help me)\s+)*(?:search|look up|find)\b", text
    )
    direct_search = re.match(
        r"^(?:(?:请你|请|帮我|帮忙|麻烦你|麻烦|能否|可以)\s*)*(?:搜索|检索)", text
    )
    return bool(
        (chinese or english)
        and (
            direct_search
            or re.search(
                r"联网|上网|网上|互联网|公开|今天|今日|最新|实时|新闻|天气|官网|官方"
                r"|\b(?:web|online|internet|today|latest|current|news|official)\b",
                text,
            )
        )
    )
