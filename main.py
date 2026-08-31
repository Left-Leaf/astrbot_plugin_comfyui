"""AstrBot ComfyUI 文生图插件（Anima3）。

完整流程：

1. 用户在 QQ 等平台发送 ``/anima <描述>``。
2. 插件加载 ``skills/anima3-prompt`` 技能，交给 LLM 生成 Anima3 正向提示词。
3. 将提示词注入当前激活的工作流（存于 ``data/plugin_data/<plugin_name>/workflows/``），
   提交 ComfyUI。
4. 轮询等待生成完成，下载图片并发送回聊天。
"""

from __future__ import annotations

import json
import random
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import Reply
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .comfy_client import ComfyUIClient
from .prompt_engine import AnimaPromptGenerator

# self.name 在 AstrBot v4.9.2+ 可用；更低版本使用该兜底名称。
PLUGIN_NAME = "astrbot_plugin_comfyui"

# 前置质量提示词，注入到工作流正向提示词之前，保证出图质量。
QUALITY_PREFIX = (
    "masterpiece, best quality, score_7, score_9, very aesthetic, ultra detailed"
)

# 生图任务历史最多保留的条数。
PROMPT_HISTORY_MAX = 30


@dataclass
class ImageGenTask:
    """One image generation task (both /comfyui and /改图 create one).

    A task is created when the user invokes a command, records the triggering
    message, then is progressively filled in as generation proceeds:

    - ``trigger_message_id`` / ``trigger_text`` — set at task creation time
      (the user's command message).
    - ``prompt`` / ``full_prompt`` — written once the LLM finishes building the
      positive prompt.
    - ``result_path`` — written after the generated image is saved and sent.

    Tasks are keyed by ``trigger_message_id`` and capped at 30 entries.
    """

    trigger_message_id: str
    trigger_text: str = ""
    prompt: str = ""
    full_prompt: str = ""
    result_path: str = ""
    ts: float = field(default_factory=time.time)


@register(PLUGIN_NAME, "Left-Leaf", "ComfyUI 文生图插件（Anima3）", "1.1.0")
class ComfyUIPlugin(Star):
    """基于 anima3-prompt skill 的 ComfyUI Anima3 文生图插件。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}

        self.plugin_name = getattr(self, "name", None) or PLUGIN_NAME
        # 运行时数据存放于 data/plugin_data/<plugin_name>/
        self.plugin_data_path = (
            Path(get_astrbot_data_path()) / "plugin_data" / self.plugin_name
        )
        # 所有工作流放在 workflows/ 子目录，用 active_workflow.json 指定当前激活的工作流。
        self.workflows_dir = self.plugin_data_path / "workflows"
        self.active_workflow_path = self.plugin_data_path / "active_workflow.json"
        self.skill_dir = Path(__file__).parent / "skills" / "anima3-prompt"
        self.prompt_gen = AnimaPromptGenerator(context, self.skill_dir)
        # 生图任务历史：trigger_message_id -> ImageGenTask。用于「回复改图」——
        # 用户回复某条生图结果时，通过回复链回溯到触发消息 ID 找到对应任务。
        self._task_history: OrderedDict[str, ImageGenTask] = OrderedDict()

    def _cfg(self, key: str, default):
        """Read a plugin config value, tolerating both dict and AstrBotConfig."""
        if isinstance(self.config, dict):
            return self.config.get(key, default)
        return getattr(self.config, key, default)

    async def initialize(self) -> None:
        """初始化工作流目录、默认工作流与激活状态。"""
        self.workflows_dir.mkdir(parents=True, exist_ok=True)
        src = Path(__file__).parent / "anima.json"
        default_workflow = self.workflows_dir / "anima.json"
        if src.exists() and not default_workflow.exists():
            default_workflow.write_text(
                src.read_text(encoding="utf-8"), encoding="utf-8"
            )
            logger.info(f"默认工作流已初始化: {default_workflow}")
        if not self.active_workflow_path.exists():
            self._set_active_workflow("anima.json")
            logger.info(f"已创建激活工作流状态: {self.active_workflow_path}")

    def _set_active_workflow(self, filename: str) -> None:
        """将某个工作流设为当前激活（写入 active_workflow.json）。"""
        self.active_workflow_path.write_text(
            json.dumps({"workflow": filename}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _get_active_workflow(self) -> str:
        """读取当前激活的工作流文件名，缺省为 anima.json。"""
        try:
            data = json.loads(self.active_workflow_path.read_text(encoding="utf-8"))
            name = str(data.get("workflow", "")).strip()
            if name:
                return name
        except (OSError, json.JSONDecodeError):
            pass
        return "anima.json"

    def _list_workflows(self) -> list[str]:
        """列出 workflows/ 目录下所有工作流文件。"""
        if not self.workflows_dir.is_dir():
            return []
        return sorted(p.name for p in self.workflows_dir.glob("*.json"))

    @staticmethod
    def _sanitize_workflow_name(name: str) -> str:
        """校验工作流文件名，防止路径穿越，返回空串表示非法。"""
        name = name.strip().strip("/\\")
        if not name or ".." in name or "/" in name or "\\" in name:
            return ""
        if not name.endswith(".json"):
            return ""
        return name

    def _resolve_workflow_path(self) -> Path:
        """解析当前激活的工作流路径；被删除时回退到默认 anima.json。"""
        name = self._get_active_workflow()
        path = self.workflows_dir / name
        if path.exists():
            return path
        if name != "anima.json":
            fallback = self.workflows_dir / "anima.json"
            if fallback.exists():
                self.logger.warning(f"激活工作流 {name} 不存在，回退到 anima.json。")
                return fallback
        return path

    def _find_prompt_node(self, workflow: dict) -> str:
        """定位正向提示词节点。

        优先使用工作流中 ``_meta.is_positive_prompt`` 标记的节点；
        否则回退到配置 ``prompt_node_id``（默认 6）。
        """
        for node_id, node in workflow.items():
            meta = node.get("_meta") or {}
            if meta.get("is_positive_prompt"):
                return str(node_id)
        return str(self._cfg("prompt_node_id", "6"))

    @filter.command("comfyui", alias={"生图"})
    async def comfyui(self, event: AstrMessageEvent, prompt: GreedyStr):
        """生成 Anima3 图片：/comfyui <图片描述>"""
        user_request = str(prompt).strip()
        if not user_request:
            yield event.plain_result(
                "用法：/comfyui <图片描述>\n"
                "例如：/comfyui 一位穿着白色连衣裙的少女站在樱花树下\n"
                "工作流管理：/workflow list 查看，/workflow use <文件> 切换"
            )
            return

        yield event.plain_result(
            f"正在使用工作流 {self._get_active_workflow()} 生成图片，请稍候..."
        )

        async for res in self._generate_and_reply(event, user_request):
            yield res

    @filter.command("改图")
    async def modify_image(self, event: AstrMessageEvent, prompt: GreedyStr):
        """回复改图：回复机器人发过的生图结果，并发送 /改图 <描述> 来修改图片。

        仅当被回复的消息是本插件之前生成并保存过提示词的结果时才会触发。
        """
        reply = self._find_reply_component(event)
        if reply is None:
            yield event.plain_result(
                "用法：请先回复要修改的生图结果，再发送 /改图 <修改描述>\n"
                "例如：回复某张图后发送 /改图 换成红色背景"
            )
            return
        modify_desc = str(prompt).strip()
        if not modify_desc:
            yield event.plain_result(
                "用法：/改图 <修改描述>\n例如：/改图 换成红色背景，添加雨天"
            )
            return

        # 按被回复消息找到对应的原生图任务；改图本身会创建一次新任务。
        source = self._find_task(reply)
        if source is None or not source.prompt:
            yield event.plain_result(
                "只能修改本机器人最近生成的图片（超出最近 30 张的历史无法修改）。"
            )
            return

        yield event.plain_result("正在根据原提示词和你新的要求修改，请稍候...")
        async for res in self._generate_and_reply(
            event,
            modify_desc,
            base_prompt=source.prompt,
        ):
            yield res

    async def _generate_and_reply(
        self,
        event: AstrMessageEvent,
        user_request: str,
        base_prompt: str = "",
    ):
        """Create an image task, generate, and send the image as a reply.

        This is used by both ``/comfyui`` (fresh task) and ``/改图`` (new task
        that reuses an earlier prompt as its base). The task is created up
        front with the trigger-message info, then progressively filled: prompt
        once the LLM output is ready, and the result path after the image is
        saved.

        Args:
            event: The triggering message event; the image is sent as a reply
                to this message.
            user_request: The user's image description.
            base_prompt: Optional existing positive prompt to modify (用于改图)。
                When given, the LLM rewrites this prompt instead of building a
                fresh one.

        Yields:
            The reply result carrying the generated image.
        """
        # 1. 创建任务封装，写入触发消息信息。
        task = self._create_task(
            str(event.message_obj.message_id),
            trigger_text=user_request,
        )
        try:
            provider_id = await self._resolve_provider_id(event)
            positive_prompt = await self.prompt_gen.generate(
                provider_id,
                user_request,
                event=event,
                enable_character_search=bool(
                    self._cfg("enable_character_search", True)
                ),
                base_prompt=base_prompt,
            )
            self.logger.info(f"Anima3 内容提示词: {positive_prompt}")

            workflow = self._build_workflow(positive_prompt)
            # 记录注入质量前缀后的完整提示词，便于核对出图质量配置。
            prompt_node_id = self._find_prompt_node(workflow)
            full_prompt = workflow[prompt_node_id]["inputs"]["text"]
            self.logger.info(f"Anima3 提交 ComfyUI 的完整提示词: {full_prompt}")

            # 2. 提示词生成完成，写入任务。
            task.prompt = positive_prompt
            task.full_prompt = full_prompt

            client = ComfyUIClient(
                str(self._cfg("comfyui_server_url", "http://127.0.0.1:8188"))
            )
            prompt_id = await client.submit_workflow(workflow)
            images = await client.wait_for_completion(
                prompt_id,
                timeout=int(self._cfg("timeout", 300)),
            )

            save_dir = Path(get_astrbot_data_path()) / "temp" / self.plugin_name
            for image_info in images:
                path = await client.download_image(image_info, save_dir)
                # 3. 结果图片保存后写入任务。
                if not task.result_path:
                    task.result_path = str(path)
                # 作为对触发消息的回复发出：前置 Reply 组件引用原消息，并 @ 发送者。
                result = event.make_result()
                try:
                    result.chain.append(
                        Reply(id=event.message_obj.message_id)
                    )
                except Exception:
                    pass
                try:
                    result.at(
                        name=event.get_sender_name(),
                        qq=event.get_sender_id(),
                    )
                except Exception:
                    pass
                result.file_image(str(path))
                yield result
        except Exception as e:
            self.logger.error(f"Anima3 生图失败: {e}", exc_info=True)
            yield event.plain_result(f"生图失败：{e}")

    def _create_task(self, trigger_message_id: str, trigger_text: str = "") -> ImageGenTask:
        """Create a new task, store it, and evict the oldest when over the cap.

        Args:
            trigger_message_id: The id of the user's command message.
            trigger_text: The user's request text (optional).

        Returns:
            The newly created and stored task.
        """
        task = ImageGenTask(
            trigger_message_id=trigger_message_id,
            trigger_text=trigger_text,
        )
        self._task_history[trigger_message_id] = task
        self._task_history.move_to_end(trigger_message_id)
        while len(self._task_history) > PROMPT_HISTORY_MAX:
            self._task_history.popitem(last=False)
        return task

    def _find_task(self, reply: Reply) -> ImageGenTask | None:
        """Look up the task referenced by a Reply component.

        The user replies to the *image* message, whose id differs from the
        *command* message id we key the history by. The image message itself
        carries a nested ``Reply`` back to the command message, so we search
        both the direct reply id and any nested reply ids inside its chain.

        Args:
            reply: The Reply component from the user's modify request.

        Returns:
            The matching task, or None when not found.
        """
        if reply is None:
            return None
        # 先查用户直接回复的消息 id（图片消息），再查其内部嵌套引用的
        # 命令消息 id（机器人发的图片消息本身回复了命令消息）。
        candidate_ids: list[str | int] = [reply.id]
        if reply.chain:
            candidate_ids += [
                comp.id for comp in reply.chain if isinstance(comp, Reply)
            ]
        for mid in candidate_ids:
            task = self._task_history.get(str(mid))
            if task is not None:
                return task
        return None

    @staticmethod
    def _find_reply_component(event: AstrMessageEvent) -> Reply | None:
        """Return the first Reply component of the message, if any.

        Args:
            event: The message event to inspect.

        Returns:
            The Reply component, or None when the message is not a reply.
        """
        for comp in event.message_obj.message:
            if isinstance(comp, Reply):
                return comp
        return None

    @filter.command_group("workflow")
    def workflow_group(self):
        """工作流管理指令组：/workflow list | use <文件> | show"""
        pass

    @workflow_group.command("list")
    async def workflow_list(self, event: AstrMessageEvent):
        """列出可用工作流：/workflow list"""
        workflows = self._list_workflows()
        if not workflows:
            yield event.plain_result(
                f"工作流目录为空：{self.workflows_dir}\n"
                "请将工作流 JSON 文件放入该目录。"
            )
            return
        active = self._get_active_workflow()
        lines = [
            f"- {name}" + ("（当前）" if name == active else "")
            for name in workflows
        ]
        yield event.plain_result(
            "可用工作流：\n" + "\n".join(lines) + "\n切换：/workflow use <文件名>"
        )

    @workflow_group.command("use")
    async def workflow_use(self, event: AstrMessageEvent, name: GreedyStr):
        """切换当前使用的工作流：/workflow use <文件名>"""
        filename = self._sanitize_workflow_name(str(name))
        if not filename:
            yield event.plain_result(
                "用法：/workflow use <工作流文件名>\n"
                "例如：/workflow use my_workflow.json"
            )
            return
        path = self.workflows_dir / filename
        if not path.exists():
            yield event.plain_result(
                f"工作流不存在：{filename}，可用 /workflow list 查看。"
            )
            return
        self._set_active_workflow(filename)
        yield event.plain_result(f"已切换到工作流：{filename}")

    @workflow_group.command("show")
    async def workflow_show(self, event: AstrMessageEvent):
        """显示当前工作流配置：/workflow show"""
        active = self._get_active_workflow()
        path = self.workflows_dir / active
        exists = "✓" if path.exists() else "✗（缺失，将回退 anima.json）"
        yield event.plain_result(
            "当前工作流配置：\n"
            f"- 激活文件：{active} {exists}\n"
            f"- 目录：{self.workflows_dir}\n"
            f"- 提示词节点（兜底）：{self._cfg('prompt_node_id', '6')}\n"
            f"- ComfyUI：{self._cfg('comfyui_server_url', 'http://127.0.0.1:8188')}"
        )

    def _build_workflow(self, positive_prompt: str) -> dict:
        """载入当前激活的工作流，注入正向提示词并为 KSampler 随机化种子。

        Args:
            positive_prompt: The content prompt to inject.

        Returns:
            The API-format workflow dict.
        """
        workflow_path = self._resolve_workflow_path()
        if not workflow_path.exists():
            raise FileNotFoundError(
                f"工作流文件不存在: {workflow_path}。\n"
                f"请将工作流 JSON 放入 {self.workflows_dir} 目录。"
            )
        workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
        prompt_node_id = self._find_prompt_node(workflow)
        if prompt_node_id not in workflow:
            raise RuntimeError(
                f"工作流 {workflow_path.name} 中找不到提示词节点 {prompt_node_id}。"
            )
        # 前置质量提示词 + LLM 生成的内容提示词。
        workflow[prompt_node_id]["inputs"]["text"] = (
            f"{QUALITY_PREFIX}, {positive_prompt}"
        )
        # 每次生成使用随机种子，避免固定种子导致图片完全一致；同时应用
        # 可配置的采样步数（默认 12，调高可提升清晰度、减少噪声）。
        for node in workflow.values():
            if node.get("class_type") == "KSampler":
                node["inputs"]["seed"] = random.randrange(1 << 63)
                node["inputs"]["steps"] = int(self._cfg("sampler_steps", 12))
        return workflow

    async def _resolve_provider_id(self, event: AstrMessageEvent) -> str:
        """解析用于生成提示词的 LLM Provider ID。

        优先使用配置中指定的 ``llm_provider_id``；否则使用当前会话的对话模型。
        """
        configured = str(self._cfg("llm_provider_id", "") or "").strip()
        if configured:
            return configured
        try:
            return await self.context.get_current_chat_provider_id(
                event.unified_msg_origin
            )
        except Exception:
            providers = self.context.get_all_providers()
            if not providers:
                raise RuntimeError(
                    "未配置可用的 LLM 模型，请在 AstrBot 中先配置对话模型。"
                )
            return providers[0].meta().id
