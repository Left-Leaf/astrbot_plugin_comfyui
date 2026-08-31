"""Generate Anima3 positive prompts by driving the ``anima3-prompt`` skill.

The skill is designed for *progressive loading*: only ``SKILL.md`` (the hard
rules) plus the reference tag libraries that are actually needed for the
current request should reach the LLM. Reading all ~120 KB of references at
once would blow up the context window.

To honor that, generation is split into two LLM calls:

1. **Routing** — a lightweight call with ``SKILL.md`` decides which
   ``references/`` files apply to the user's request (output is a JSON array
   of filenames).
2. **Generation** — ``SKILL.md`` plus only the selected references are used
   as the system prompt to produce the final positive prompt.

If routing fails (bad JSON / unknown filenames / LLM error), a sane default
reference set is used instead so the pipeline never breaks.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

# Catalog of available reference files, mirroring SKILL.md §0.
REFERENCE_CATALOG: dict[str, str] = {
    "count-identity.md": "人数/身份",
    "appearance.md": "外貌（发/眼/体/肤色/部位/标记）",
    "appearance-special.md": "外貌（非人/扶她/男娘）",
    "clothing-types.md": "服装（类型/材质/状态）",
    "clothing-modify.md": "服装（改造引擎 + 职业实例）",
    "clothing-combos.md": "服装（反差公式/道具玩具）",
    "pose-solo.md": "体位动作（单人）",
    "pose-foreplay.md": "体位动作（双人前戏）",
    "pose-sex-core.md": "体位动作（双人正戏核心）",
    "pose-sex-ext.md": "体位动作（双人正戏扩展）",
    "pose-multi.md": "体位动作（多人/百合）",
    "expression-reaction.md": "表情反应",
    "camera-shot.md": "镜头景别/POV",
    "scene-environment.md": "场景环境",
    "detail-mood.md": "质感氛围",
    "themes-a.md": "特殊主题 14.1-14.6",
    "themes-b.md": "特殊主题 14.7-14.12",
    "examples.md": "完整跑图案例",
}

# Fallback reference set when routing fails. Covers the most common single
# character showcase scenario described in SKILL.md §5.1.
DEFAULT_REFERENCES: list[str] = [
    "count-identity.md",
    "appearance.md",
    "clothing-types.md",
    "pose-solo.md",
    "expression-reaction.md",
    "camera-shot.md",
    "scene-environment.md",
    "detail-mood.md",
]

# Map AstrBot websearch_provider values to builtin search tool *names*.
# Tools are looked up by name at call time so the plugin keeps working even on
# AstrBot releases that do not ship every search provider's tool class.
WEBSEARCH_PROVIDER_TOOL_NAME: dict[str, str] = {
    "tavily": "web_search_tavily",
    "bocha": "web_search_bocha",
    "brave": "web_search_brave",
    "firecrawl": "web_search_firecrawl",
    "baidu_ai_search": "web_search_baidu",
    "exa": "web_search_exa",
    "anysearch": "web_search_anysearch",
}


class _AgentRunContext:
    """Minimal stand-in for AstrBot's ``AstrAgentContext``/``AgentContextWrapper``.

    The builtin search tools only read ``context.context`` (the plugin Context)
    and ``context.event`` (the message event) from their run context, so a tiny
    structural stand-in is enough and avoids importing AstrBot-internal classes
    that differ across versions.
    """

    __slots__ = ("context", "event")

    def __init__(self, context, event=None) -> None:
        self.context = context
        self.event = event


class AnimaPromptGenerator:
    """Loads the anima3-prompt skill and turns a request into an Anima3 prompt.

    Args:
        context: The AstrBot plugin ``Context`` used to call the LLM.
        skill_dir: Path to the ``anima3-prompt`` skill directory.
    """

    def __init__(self, context, skill_dir: Path) -> None:
        self.context = context
        self.skill_dir = skill_dir
        self.skill_md = self._read(self.skill_dir / "SKILL.md")

    @staticmethod
    def _read(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    async def generate(
        self,
        provider_id: str,
        user_request: str,
        event: AstrMessageEvent | None = None,
        enable_character_search: bool = True,
    ) -> str:
        """Generate the Anima3 positive prompt for ``user_request``.

        When ``event`` is provided and the request references a well-known IP
        character (e.g. a specific anime/game character), a web search is
        performed via AstrBot's builtin search tool to verify the character's
        appearance, and the snippets are injected into the generation context
        so the rendered image matches the character's canonical look.

        Args:
            provider_id: The LLM provider id to use.
            user_request: The user's image description.
            event: The message event, used to look up the current web search
                configuration. When None, character verification is skipped.
            enable_character_search: Whether to attempt the character
                verification web search at all.

        Returns:
            The generated positive prompt (single line of English tags).
        """
        character_context = ""
        if enable_character_search and event is not None:
            character_context = await self._gather_character_context(
                provider_id, user_request, event
            )

        if not self.skill_md:
            # Skill missing: fall back to a plain, generic instruction so the
            # plugin still works.
            system_prompt = (
                "You are an expert anime illustration prompt engineer. Convert "
                "the user's Chinese scene description into a single-line English "
                "prompt for the Anima3 image model. Output only the prompt text, "
                "no explanations, no markdown."
            )
            if character_context:
                system_prompt += (
                    "\n\n"
                    "Reference material about the character's appearance (use it "
                    "to keep the prompt faithful to the canonical design):\n\n"
                    f"{character_context}"
                )
            return await self._call_llm(provider_id, system_prompt, user_request)

        references = await self._route_references(provider_id, user_request)
        references_text = self._load_references(references)
        system_prompt = (
            f"{self.skill_md}\n\n"
            f"# 本次已加载的参考标签库\n\n{references_text}"
        )
        if character_context:
            system_prompt += (
                "\n\n"
                "# 角色形象查证资料（来自联网搜索）\n\n"
                "用户请求中涉及知名 IP 角色。以下为联网搜索到的该角色形象资料，"
                "请务必据此忠实刻画角色的外貌特征（发型、发色、瞳色、服装、标志性"
                "元素等），不要凭空臆造与角色形象不符的细节：\n\n"
                f"{character_context}"
            )
        raw = await self._call_llm(provider_id, system_prompt, user_request)
        return self._clean_prompt(raw)

    async def _call_llm(self, provider_id: str, system_prompt: str, prompt: str) -> str:
        resp = await self.context.llm_generate(
            chat_provider_id=provider_id,
            prompt=prompt,
            system_prompt=system_prompt,
        )
        return (resp.completion_text or "").strip()

    async def _gather_character_context(
        self,
        provider_id: str,
        user_request: str,
        event: AstrMessageEvent,
    ) -> str:
        """Detect a well-known IP character and collect its appearance info.

        First asks the LLM whether ``user_request`` references a specific known
        character (e.g. an anime/game character). If yes, runs a web search via
        AstrBot's configured search provider and returns the formatted snippets
        for injection into the generation context. Any failure degrades to an
        empty string so the pipeline never breaks.

        Args:
            provider_id: The LLM provider id to use for the detection call.
            user_request: The user's image description.
            event: The message event used to resolve the search configuration.

        Returns:
            A formatted block of character appearance notes, or "" when no
            character is referenced or the search could not be completed.
        """
        character = await self._detect_character(provider_id, user_request)
        if not character:
            return ""
        snippets = await self._search_character(character, event)
        if not snippets:
            return ""
        return f"角色：{character}\n{snippets}"

    async def _detect_character(self, provider_id: str, user_request: str) -> str:
        """Ask the LLM if the request references a specific known character.

        Args:
            provider_id: The LLM provider id to use.
            user_request: The user's image description.

        Returns:
            The detected character name, or "" when none is referenced.
        """
        system_prompt = (
            "判断用户的生图请求是否明确指向某个具体的知名 IP 角色（动漫、游戏、"
            "影视等作品中的角色，例如「雷电将军」「初音未来」「白起」）。\n"
            "若明确指向某知名角色，仅输出该角色名称（保持原文，不加引号、不加"
            "其他内容）；\n"
            "若只是泛指的人物、职业、路人，或无法确定具体角色，输出空字符串。\n"
            "只允许输出角色名或空字符串，不要输出任何解释。"
        )
        try:
            name = await self._call_llm(provider_id, system_prompt, user_request)
            return name.strip().strip('"')
        except Exception as e:
            logger.warning(f"Anima 角色识别失败: {e}")
            return ""

    async def _search_character(self, character: str, event: AstrMessageEvent) -> str:
        """Search the character's appearance via AstrBot's builtin search tool.

        Resolves the user's configured ``websearch_provider`` and calls the
        matching builtin search tool with a query targeting the character's
        appearance. Returns a flat list of result titles + snippets.

        Args:
            character: The character name to search for.
            event: The message event used to look up provider settings.

        Returns:
            Formatted search snippets, or "" when search is disabled/unavailable.
        """
        try:
            cfg = self.context.get_config(umo=event.unified_msg_origin)
            provider_settings = cfg.get("provider_settings", {}) or {}
        except Exception as e:
            logger.warning(f"Anima 读取联网搜索配置失败: {e}")
            return ""

        if not provider_settings.get("web_search", False):
            return ""

        provider = provider_settings.get("websearch_provider", "tavily")
        tool_name = WEBSEARCH_PROVIDER_TOOL_NAME.get(provider)
        if tool_name is None:
            logger.warning(f"Anima 不支持的联网搜索服务: {provider}")
            return ""

        try:
            tool = self.context.get_llm_tool_manager().get_builtin_tool(tool_name)
            agent_ctx = _AgentRunContext(context=self.context, event=event)
            run_ctx = _AgentRunContext(context=agent_ctx)
            query = (
                f"{character} 角色 外貌 形象 发型 发色 瞳色 服装 设定 "
                f"{character} character appearance design"
            )
            result = await tool.call(
                run_ctx, query=query, max_results=5, count=5, top_k=5, limit=5
            )
            return self._parse_search_payload(result)
        except Exception as e:
            logger.warning(f"Anima 角色形象联网搜索失败: {e}")
            return ""

    @staticmethod
    def _parse_search_payload(result: object) -> str:
        """Extract title/snippet lines from a search tool's JSON payload.

        Args:
            result: The ``ToolExecResult`` returned by a builtin search tool,
                normally a JSON string of ``{"results": [{title, snippet, ...}]}``.

        Returns:
            A newline-joined list of "title: snippet" lines, or "" when empty.
        """
        text = result if isinstance(result, str) else str(result or "")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return ""
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            return ""
        lines = []
        for item in results:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            snippet = str(item.get("snippet") or "").strip()
            if title or snippet:
                lines.append(f"- {title}: {snippet}")
        return "\n".join(lines)

    async def _route_references(self, provider_id: str, user_request: str) -> list[str]:
        """Ask the LLM which reference files the request needs."""
        catalog = "\n".join(
            f"- {name}: {purpose}" for name, purpose in REFERENCE_CATALOG.items()
        )
        system_prompt = (
            f"{self.skill_md}\n\n"
            "# 加载规划任务\n\n"
            "根据用户的生图需求，结合上方 §0 加载指引表，判断本次需要加载哪些 "
            "references/ 参考文件。\n\n"
            "可用参考文件（只能从中选择）：\n"
            f"{catalog}\n\n"
            '输出格式：仅一行 JSON 数组，例如 ["appearance.md","clothing-types.md",'
            '"pose-solo.md"]。不要输出任何其他内容。'
        )
        try:
            raw = await self._call_llm(provider_id, system_prompt, user_request)
            return self._parse_reference_list(raw)
        except Exception as e:
            logger.warning(f"Anima 参考文件路由失败，使用默认集合: {e}")
            return list(DEFAULT_REFERENCES)

    def _load_references(self, names: list[str]) -> str:
        """Concatenate the given reference files into one markdown block."""
        parts = []
        for name in names:
            content = self._read(self.skill_dir / "references" / name)
            if content:
                parts.append(f"## {name}\n\n{content}")
        return "\n\n".join(parts)

    @staticmethod
    def _clean_prompt(raw: str) -> str:
        """Normalize the final prompt: drop code fences and collapse whitespace.

        The skill mandates a single comma-separated line of tags; a few extra
        guards keep a slightly-off LLM reply from breaking the workflow.
        """
        text = re.sub(r"```(?:text|txt)?\s*|\s*```", "", raw.strip())
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _parse_reference_list(raw: str) -> list[str]:
        """Robustly parse the routing output into a list of valid filenames.

        Tolerates markdown code fences and surrounding prose, and drops any
        filename that is not in the catalog.
        """
        text = re.sub(r"```(?:json)?\s*|\s*```", "", raw.strip())
        match = re.search(r"\[.*\]", text, flags=re.DOTALL)
        if not match:
            return list(DEFAULT_REFERENCES)
        try:
            names = json.loads(match.group(0))
        except (json.JSONDecodeError, TypeError):
            return list(DEFAULT_REFERENCES)
        if not isinstance(names, list):
            return list(DEFAULT_REFERENCES)
        valid = [n for n in names if isinstance(n, str) and n in REFERENCE_CATALOG]
        return valid or list(DEFAULT_REFERENCES)
