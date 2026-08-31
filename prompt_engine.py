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

    async def generate(self, provider_id: str, user_request: str) -> str:
        """Generate the Anima3 positive prompt for ``user_request``.

        Args:
            provider_id: The LLM provider id to use.
            user_request: The user's image description.

        Returns:
            The generated positive prompt (single line of English tags).
        """
        if not self.skill_md:
            # Skill missing: fall back to a plain, generic instruction so the
            # plugin still works.
            system_prompt = (
                "You are an expert anime illustration prompt engineer. Convert "
                "the user's Chinese scene description into a single-line English "
                "prompt for the Anima3 image model. Output only the prompt text, "
                "no explanations, no markdown."
            )
            return await self._call_llm(provider_id, system_prompt, user_request)

        references = await self._route_references(provider_id, user_request)
        references_text = self._load_references(references)
        system_prompt = (
            f"{self.skill_md}\n\n"
            f"# 本次已加载的参考标签库\n\n{references_text}"
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
