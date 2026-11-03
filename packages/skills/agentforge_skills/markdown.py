"""Bounded SKILL.md parsing. Frontmatter is data; it never grants tool permissions."""

import math
import re

import yaml
from agentforge_core.skills import SkillDocument, SkillDocumentError
from yaml.events import AliasEvent


class SkillLoader(yaml.SafeLoader):
    # Keep dates as strings, matching portable recipe metadata rather than Python objects.
    yaml_implicit_resolvers = {
        key: [(tag, pattern) for tag, pattern in values if tag != "tag:yaml.org,2002:timestamp"]
        for key, values in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }

    def __init__(self, stream):
        super().__init__(stream)
        self.depth = 0
        self.nodes = 0

    def compose_node(self, parent, index):
        self.depth += 1
        self.nodes += 1
        try:
            if (
                self.depth > 6
                or self.nodes > 256
                or self.check_event(AliasEvent)
                or getattr(self.peek_event(), "anchor", None) is not None
            ):
                raise SkillDocumentError()
            return super().compose_node(parent, index)
        finally:
            self.depth -= 1

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result or key == "<<":
                raise SkillDocumentError()
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def validate_data(value):
    if isinstance(value, dict):
        for child in value.values():
            validate_data(child)
    elif isinstance(value, list):
        for child in value:
            validate_data(child)
    elif value is not None and type(value) not in (str, int, bool, float):
        raise SkillDocumentError()
    elif isinstance(value, float) and not math.isfinite(value):
        raise SkillDocumentError()


def parse_document(content):
    if not isinstance(content, str) or len(content) > 12000:
        raise SkillDocumentError()
    try:
        content.encode("utf-8")
        match = re.match(r"\A\ufeff?---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", content, re.DOTALL)
        if not match or len(match[1]) > 4000:
            raise SkillDocumentError()
        frontmatter = yaml.load(match[1], Loader=SkillLoader)
        if not isinstance(frontmatter, dict):
            raise SkillDocumentError()
        validate_data(frontmatter)
        return frontmatter, content[match.end() :].removeprefix("\r\n").removeprefix("\n")
    except (yaml.YAMLError, UnicodeError, RecursionError, ValueError, TypeError):
        raise SkillDocumentError() from None


def resolve_document(fields, prior=None):
    if fields.get("content", "").strip():
        metadata, body = parse_document(fields["content"])
    else:
        metadata, body = parse_document(prior) if prior else ({}, "")
        for field in ("name", "description"):
            if field in fields:
                metadata[field] = fields[field]
        if "body" in fields:
            body = fields["body"]
    name, description = metadata.get("name"), metadata.get("description")
    if not isinstance(name, str) or not isinstance(description, str):
        raise SkillDocumentError()
    name, description = name.strip(), description.strip()
    try:
        if (
            not name
            or not description
            or len(name.encode("utf-16-le")) // 2 > 80
            or any(ord(c) < 32 for c in name)
            or name.startswith("/")
            or re.match(r"^(chat\s+settings|settings:)", name, re.IGNORECASE)
            or re.search(r" \[v[0-9]+:[a-f0-9]{8}\]$", name)
        ):
            raise SkillDocumentError()
        metadata = {
            "name": name,
            "description": description,
            **{key: value for key, value in metadata.items() if key not in {"name", "description"}},
        }
        header = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).rstrip()
        content = f"---\n{header}\n---\n\n{body}"
        # Round-trip through the same bounded parser; normalization must not create an
        # unreadable document. Unknown metadata is preserved without becoming authorization.
        parse_document(content)
        return SkillDocument(
            name=name,
            description=description,
            content=content,
            required_tools=metadata.get("required_tools", ()),
        )
    except (ValueError, TypeError, UnicodeError, yaml.YAMLError):
        raise SkillDocumentError() from None
