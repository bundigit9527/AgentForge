from agentforge_core.skills import SkillManifest, SkillSnapshot

# Published versions are immutable. Change the version when changing a recipe.
_RECIPES = (
    SkillManifest(
        skill_id="document_summary",
        version=1,
        name="资料摘要",
        description="从用户提供的资料提取主题、事实、结论和待核实事项。",
        content=(
            "# 资料摘要\n"
            "仅依据用户提供或已授权获取的资料。先识别主题，再提取主要事实和结论。\n"
            "区分原文事实与分析推断；资料未说明的内容标记为待核实，不编造来源。\n"
            "按摘要、关键依据、待核实事项组织结果，保留资料中已有的来源标识。\n"
            "验收：结论有依据、数字与原文一致、不遗漏关键限制。"
        ),
    ),
    SkillManifest(
        skill_id="meeting_notes",
        version=1,
        name="会议纪要",
        description="将会议材料整理为议题、决议、行动项与待确认事项。",
        content=(
            "# 会议纪要\n"
            "阅读用户提供的记录，区分讨论意见、明确决议和未决问题。\n"
            "行动项列出事项、负责人、截止日期；没有明确记录的字段标记待确认。\n"
            "验收：不把建议写成决议，不编造负责人或时间，不自动发送或创建工单。"
        ),
    ),
    SkillManifest(
        skill_id="text_statistics",
        version=1,
        name="文本统计",
        description="使用文本统计工具计算字符、行数和空白分隔片段。",
        required_tools=("text_stats@1",),
        content=(
            "# 文本统计\n"
            "将用户指定的完整文本传给 text_stats，保留原始换行和空白。\n"
            "按工具结果报告字符数、非空白字符数、行数和空白分隔片段数。\n"
            "验收：数值来自工具，明确片段数不等于中文词数或模型 token 数。"
        ),
    ),
)

BUILTIN_SKILLS = tuple(
    SkillSnapshot(
        reference=f"builtin:{manifest.skill_id}@{manifest.version}",
        source="builtin",
        source_id="builtin",
        manifest=manifest,
        sha256=manifest.digest(),
    )
    for manifest in _RECIPES
)
