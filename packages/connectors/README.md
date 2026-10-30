# Connectors

已实现 `agentforge_connectors.skill_hub.HttpSkillHub`：通过固定部署地址浏览 Hub 技能列表、读取版本详情。接口契约位于 core，来源转换和 HTTP 访问留在本包；运行时不访问 Hub。

产品知识、智能体广场及外部业务 API 连接器仍待建设。协议及边界见 [Skill 与广场安装](../../docs/skills.md)。
