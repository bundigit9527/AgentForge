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
    SkillManifest(
        skill_id="presentation",
        version=1,
        name="PPT生成",
        description="根据资料或当前对话自动生成可编辑 PowerPoint 文件并返回下载附件。",
        required_tools=("python_execute@1",),
        content=(
            "# PPT生成\n"
            "结合本轮要求和已有对话选取内容。主题明确时直接执行，默认中文、16:9、6至8页；"
            "只有完全缺少主题或必要资料时才询问，不重复索要已有材料。\n"
            "调用 python_execute，使用已安装的 python-pptx（from pptx import Presentation），"
            "在 /workspace 写入 .pptx 并在 exports 声明文件名。无需安装依赖。"
            "使用可编辑文本框、形状、表格；正文22至28pt，标题30至36pt，浅底深字，"
            "保持页边距，拆分过密内容。默认包括封面、主要内容、结论和已有来源。"
            "不编造数据、日期或来源；生成最新资讯前先调用已授权联网工具。\n"
            "从 pptx.util 导入 Inches、Pt；使用 Inches(13.333)、Inches(7.5) 设置画布。"
            "避免将英文长段或中文密集内容塞入固定文本框。"
            "保存后用 Presentation(文件名) 再次打开，检查页数、非空标题和形状边界，"
            "print 简短检查结果。遇到 exit_code 非零，根据 stderr 修正后重新执行；"
            "每次执行是空目录，须重新生成完整文件。\n"
            "仅在工具返回成功且 files 包含产物时告知已生成。附件自动出现在聊天中，"
            "不要提供虚构本地下载路径。结构检查不等于视觉检查，未渲染时不得声称已逐页审图。"
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

# Keep the published v1 recipe resolvable; only the newest recipe is discoverable.
LEGACY_BUILTIN_SKILLS = BUILTIN_SKILLS[-1:]
_continuation = SkillManifest(
    skill_id="presentation",
    version=2,
    name="PPT生成",
    description="根据资料生成可编辑 PPT，或读取已有 PPT 按要求修改并交付新版本。",
    required_tools=("python_execute@2",),
    content=(
        "# PPT生成与修改\n"
        "结合本轮要求和已有对话；主题明确时直接执行，默认中文、16:9、6至8页。"
        "只有缺少必要主题或源文件时才询问，不重复索要已有资料。\n"
        "新建：调用 python_execute，使用 python-pptx（from pptx import Presentation）"
        "生成可编辑文本、形状和表格。"
        "浅底深字，标题30至36pt，正文22至28pt，留足边距，拆分过密内容。"
        "用 Inches(13.333)、Inches(7.5) 设置画布。保留已有来源，不编造数字或日期。\n"
        "修改：从 available files 选择目标 artifact_id；通过 inputs 指定该 ID 和本地文件名，"
        "用 Presentation(文件名) 读取真实 PPT，检查页数、文本和形状，再只修改用户要求的页面或元素。"
        "保留其余页面、布局、图片和样式，勿凭对话重新生成整份文件。"
        "保存为相同 input 文件名并在 exports 声明，系统保存新版本，原文件保留。"
        "如用户要求另存独立文件，使用新的 exports 文件名。\n"
        "保存后重新打开，检查页数、目标内容及形状边界；修改时对比未要求修改的内容，"
        "print 简短验收结果。"
        "工具报错时根据 stderr 修正，下一次调用须再次提供 inputs；"
        "中间产物可用返回的 artifact_id 继续读取。"
        "单次输入和输出分别最多4个、合计10MiB；无网络、无需安装依赖。\n"
        "仅在 exit_code 为0且 files 返回产物时告知完成，附件自动发布，不虚构下载路径。"
        "结构检查不等于视觉检查，未渲染不得声称逐页审图。"
    ),
)
BUILTIN_SKILLS = BUILTIN_SKILLS[:-1] + (
    SkillSnapshot(
        reference="builtin:presentation@2",
        source="builtin",
        source_id="builtin",
        manifest=_continuation,
        sha256=_continuation.digest(),
    ),
)
