"""AstrBot ComfyUI 文生图插件（Anima3）。

完整流程：

1. 用户在 QQ 等平台发送 ``/comfyui run <描述>``。
2. 插件加载 ``skills/anima3-prompt`` 技能，交给 LLM 生成 Anima3 正向提示词。
3. 将提示词注入当前激活的工作流（工作流目录由配置 ``workflow_dir`` 指定），
   提交 ComfyUI。
4. 轮询等待生成完成，下载图片并发送回聊天。

工作流管理：

- 工作流目录完全由配置文件 ``workflow_dir`` 决定；留空则使用插件数据目录下
  的 ``workflows/`` 子目录。
- 启动时扫描目录内所有 ``*.json`` 工作流，识别「受支持」的工作流：必须存在
  ``_meta.title == "Prompts"`` 且 ``class_type == "CLIPTextEncode"`` 的节点。
- 不支持删除/修改指令；本地工作流的增删改由用户手动操作目录完成。

执行后端（按激活工作流的来源自动路由）：

- 本地工作流 → 提交到本地 ComfyUI（``comfyui_server_url``）。
- RunningHub 工作流 → 通过 ``runninghub_api_key`` / ``runninghub_base_url``
  调用 RunningHub 云端 API。在配置 ``runninghub_workflows`` 中以「别名 →
  webappId」键值对列表登记你在 RunningHub 上传的工作流；这些工作流会
  出现在 ``/comfyui workflow list`` 中并带「（RunningHub）」后缀，可用
  ``/comfyui workflow use <名称>`` 激活。
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

from .comfy_client import (
    ComfyUIClient,
    RunningHubClient,
    RunningHubError,
)
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


@register(PLUGIN_NAME, "Left-Leaf", "ComfyUI 文生图插件（Anima3）", "2.1.0")
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
        # 工作流目录由配置 workflow_dir 指定；留空则使用插件数据目录下 workflows/。
        configured_dir = str(self._cfg("workflow_dir", "") or "").strip()
        if configured_dir:
            self.workflows_dir = Path(configured_dir).expanduser()
        else:
            self.workflows_dir = self.plugin_data_path / "workflows"
        # 激活工作流状态文件。
        self.active_workflow_path = self.plugin_data_path / "active_workflow.json"
        self.skill_dir = Path(__file__).parent / "skills" / "anima3-prompt"
        self.prompt_gen = AnimaPromptGenerator(context, self.skill_dir)
        # 工作流支持性状态：{文件名: {"supported": bool, "prompt_node_id": str|None}}。
        self._workflow_status: dict[str, dict] = {}
        # 生图任务历史：trigger_message_id -> ImageGenTask。用于「回复改图」——
        # 用户回复某条生图结果时，通过回复链回溯到触发消息 ID 找到对应任务。
        self._task_history: OrderedDict[str, ImageGenTask] = OrderedDict()

    def _cfg(self, key: str, default):
        """Read a plugin config value, tolerating both dict and AstrBotConfig."""
        if isinstance(self.config, dict):
            return self.config.get(key, default)
        return getattr(self.config, key, default)

    async def initialize(self) -> None:
        """初始化工作流目录、扫描并校验所有工作流，设置激活状态。"""
        self.workflows_dir.mkdir(parents=True, exist_ok=True)

        # 将插件自带的默认工作流复制到目标目录（仅当不存在时）。
        src = Path(__file__).parent / "anima.json"
        default_workflow = self.workflows_dir / "anima.json"
        if src.exists() and not default_workflow.exists():
            default_workflow.write_text(
                src.read_text(encoding="utf-8"), encoding="utf-8"
            )
            logger.info(f"默认工作流已初始化: {default_workflow}")

        # 扫描并校验所有工作流，记录支持性。
        self._workflow_status = self._scan_workflows()

        # 若尚无激活状态文件，自动选择第一个受支持的本地工作流。
        if not self.active_workflow_path.exists():
            supported = [n for n, s in self._workflow_status.items() if s["supported"]]
            if supported:
                self._set_active(supported[0], "local")
                logger.info(f"已自动激活工作流: {supported[0]}")
            else:
                logger.warning(
                    f"工作流目录 {self.workflows_dir} 中没有受支持的工作流。"
                    "需要存在 _meta.title == 'Prompts' 且 class_type == 'CLIPTextEncode' 的节点。"
                )

        # 配置了 RunningHub 工作流但未提供 API Key 时给出提示。
        if (
            self._rh_workflows()
            and not str(self._cfg("runninghub_api_key", "") or "").strip()
        ):
            logger.warning(
                "已配置 RunningHub 工作流，但 runninghub_api_key 为空；"
                "RunningHub 工作流将无法执行。"
            )

    def _scan_workflows(self) -> dict[str, dict]:
        """扫描工作流目录，返回 {文件名: {"supported": bool, "prompt_node_id": str|None}}。"""
        status: dict[str, dict] = {}
        if not self.workflows_dir.is_dir():
            return status
        for path in sorted(self.workflows_dir.glob("*.json")):
            supported, node_id = self._check_workflow_support(path)
            status[path.name] = {"supported": supported, "prompt_node_id": node_id}
        return status

    @staticmethod
    def _check_workflow_support(path: Path) -> tuple[bool, str | None]:
        """检查工作流是否受支持：存在 title 为 Prompts 的 CLIPTextEncode 节点。"""
        try:
            workflow = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False, None
        for node_id, node in workflow.items():
            if not isinstance(node, dict):
                continue
            meta = node.get("_meta") or {}
            title = str(meta.get("title", "")).strip()
            if title == "Prompts" and node.get("class_type") == "CLIPTextEncode":
                return True, str(node_id)
        return False, None

    def _rh_workflows(self) -> dict[str, str]:
        """Parse configured RunningHub workflows into ``{alias: webapp_id}``.

        The config value is a list of ``{"alias": ..., "workflow_id": ...}``
        objects. A legacy comma-separated ``name=webappId`` string is also
        accepted for backward compatibility with older configs.
        """
        raw = self._cfg("runninghub_workflows", []) or []
        result: dict[str, str] = {}

        def _add(alias: object, wid: object) -> None:
            alias_s = str(alias).strip() if alias is not None else ""
            wid_s = str(wid).strip() if wid is not None else ""
            if alias_s and wid_s:
                result[alias_s] = wid_s

        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    _add(item.get("alias"), item.get("workflow_id"))
        elif isinstance(raw, str) and raw.strip():
            # Legacy format: comma-separated "name=webappId" pairs.
            for part in raw.split(","):
                part = part.strip()
                if not part or "=" not in part:
                    continue
                name, _, wid = part.partition("=")
                _add(name, wid)
        return result

    def _unified_workflows(self) -> list[dict]:
        """Return all workflows (local + RunningHub) as registry entries.

        Each entry is a dict with keys ``key`` (unique id), ``display`` (name
        shown in the list command), ``source`` ("local" or "runninghub"),
        ``supported`` (bool), and ``webapp_id`` (str, set for RH only).
        """
        entries: list[dict] = []
        for name, status in self._workflow_status.items():
            entries.append(
                {
                    "key": name,
                    "display": name,
                    "source": "local",
                    "supported": status["supported"],
                    "webapp_id": "",
                }
            )
        for name, wid in self._rh_workflows().items():
            entries.append(
                {
                    "key": name,
                    "display": f"{name}（RunningHub）",
                    "source": "runninghub",
                    "supported": True,
                    "webapp_id": wid,
                }
            )
        return entries

    def _read_active(self) -> tuple[str, str]:
        """Read the active workflow as ``(key, source)``; ``("", "")`` if unset."""
        try:
            data = json.loads(self.active_workflow_path.read_text(encoding="utf-8"))
            key = str(data.get("workflow", "")).strip()
            source = str(data.get("source", "local")).strip() or "local"
            if key:
                return (key, source)
        except (OSError, json.JSONDecodeError):
            pass
        # 回退：取第一个受支持的本地工作流。
        for name, status in self._workflow_status.items():
            if status["supported"]:
                return (name, "local")
        return ("", "")

    def _set_active(self, key: str, source: str) -> None:
        """Set the active workflow (key + source) into active_workflow.json."""
        self.active_workflow_path.parent.mkdir(parents=True, exist_ok=True)
        self.active_workflow_path.write_text(
            json.dumps(
                {"workflow": key, "source": source}, ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )

    def _get_active_entry(self) -> dict | None:
        """Return the active workflow registry entry, or None if unset/missing."""
        key, source = self._read_active()
        if not key:
            return None
        for entry in self._unified_workflows():
            if entry["key"] == key and entry["source"] == source:
                return entry
        return None

    def _resolve_local_path(self, name: str) -> Path:
        """Resolve a local workflow filename to its path (must exist)."""
        path = self.workflows_dir / name
        if not path.exists():
            raise FileNotFoundError(
                f"工作流文件不存在: {path}。\n"
                f"请将工作流 JSON 放入 {self.workflows_dir} 目录。"
            )
        return path

    def _find_prompt_node(self, workflow: dict) -> str:
        """定位正向提示词节点：_meta.title == 'Prompts' 的 CLIPTextEncode 节点。"""
        for node_id, node in workflow.items():
            if not isinstance(node, dict):
                continue
            meta = node.get("_meta") or {}
            title = str(meta.get("title", "")).strip()
            if title == "Prompts" and node.get("class_type") == "CLIPTextEncode":
                return str(node_id)
        raise RuntimeError(
            "工作流中找不到 _meta.title 为 'Prompts' 的 CLIPTextEncode 节点，"
            "该工作流不受支持。"
        )

    @filter.command_group("comfyui")
    def comfyui_group(self):
        """ComfyUI 文生图指令组：/comfyui run <描述> | /comfyui workflow list"""
        pass

    @comfyui_group.command("run", alias={"生图"})
    async def comfyui_run(self, event: AstrMessageEvent, prompt: GreedyStr):
        """生成 Anima3 图片：/comfyui run <图片描述>"""
        user_request = str(prompt).strip()
        if not user_request:
            yield event.plain_result(
                "用法：/comfyui run <图片描述>\n"
                "例如：/comfyui run 一位穿着白色连衣裙的少女站在樱花树下\n"
                "工作流管理：/comfyui workflow list 查看可用工作流"
            )
            return

        active = self._get_active_entry()
        if not active:
            yield event.plain_result(
                f"没有可用的受支持工作流。\n"
                f"请将工作流 JSON 放入 {self.workflows_dir} 目录，"
                "并确保其中存在 _meta.title 为 'Prompts' 的 CLIPTextEncode 节点。"
            )
            return

        yield event.plain_result(
            f"正在使用工作流 {active['display']} 生成图片，请稍候..."
        )

        async for res in self._generate_and_reply(event, user_request):
            yield res

    @comfyui_group.group("workflow")
    def comfyui_workflow_group(self):
        """工作流子指令组：/comfyui workflow list"""
        pass

    @comfyui_workflow_group.command("list", alias={"-l"})
    async def workflow_list(self, event: AstrMessageEvent):
        """列出可用工作流（本地 + RunningHub）：/comfyui workflow list 或 /comfyui -l"""
        entries = self._unified_workflows()
        if not entries:
            yield event.plain_result(
                f"没有可用的工作流。\n"
                f"请将工作流 JSON 放入 {self.workflows_dir} 目录，"
                "或在配置 runninghub_workflows 中登记 RunningHub 工作流。"
            )
            return
        active = self._get_active_entry()
        active_key = active["key"] if active else ""
        lines: list[str] = []
        for entry in entries:
            mark = ""
            if not entry["supported"]:
                mark = "（不受支持）"
            elif entry["key"] == active_key:
                mark = "（当前）"
            lines.append(f"- {entry['display']}{mark}")
        yield event.plain_result(
            f"工作流目录：{self.workflows_dir}\n" + "\n".join(lines)
        )

    @comfyui_workflow_group.command("use", alias={"-u"})
    async def workflow_use(self, event: AstrMessageEvent, name: GreedyStr):
        """切换激活的工作流（本地或 RunningHub）：/comfyui workflow use <名称>"""
        wanted = str(name).strip()
        if not wanted:
            yield event.plain_result(
                "用法：/comfyui workflow use <工作流名称>\n"
                "用 /comfyui workflow list 查看可用名称。"
            )
            return
        # 按显示名或 key 匹配；RunningHub 条目允许省略「（RunningHub）」后缀。
        matched = None
        for entry in self._unified_workflows():
            if wanted == entry["key"] or wanted == entry["display"]:
                matched = entry
                break
        if matched is None:
            yield event.plain_result(
                f"未找到名为「{wanted}」的工作流。\n"
                "用 /comfyui workflow list 查看可用名称。"
            )
            return
        self._set_active(matched["key"], matched["source"])
        backend = (
            "RunningHub（云端）"
            if matched["source"] == "runninghub"
            else "本地 ComfyUI"
        )
        yield event.plain_result(
            f"已激活工作流：{matched['display']}\n执行后端：{backend}"
        )

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

            # 2. 按激活工作流的来源选择执行后端（本地 ComfyUI 或 RunningHub）。
            active = self._get_active_entry()
            if not active:
                raise RuntimeError("没有可用的受支持工作流，无法生成图片。")

            task.prompt = positive_prompt
            save_dir = Path(get_astrbot_data_path()) / "temp" / self.plugin_name

            if active["source"] == "runninghub":
                client = RunningHubClient(
                    str(self._cfg("runninghub_api_key", "") or ""),
                    str(self._cfg("runninghub_base_url", "https://www.runninghub.cn")),
                )
                node_info_list, full_prompt = await self._build_runninghub_overrides(
                    active["webapp_id"], positive_prompt
                )
                task.full_prompt = full_prompt
                prompt_id = await client.submit(
                    int(active["webapp_id"]), node_info_list
                )
            else:
                workflow = self._build_workflow(positive_prompt)
                # 记录注入质量前缀后的完整提示词，便于核对出图质量配置。
                prompt_node_id = self._find_prompt_node(workflow)
                full_prompt = workflow[prompt_node_id]["inputs"]["text"]
                task.full_prompt = full_prompt
                client = ComfyUIClient(
                    str(self._cfg("comfyui_server_url", "http://127.0.0.1:8188"))
                )
                prompt_id = await client.submit_workflow(workflow)

            self.logger.info(f"Anima3 提交完整提示词: {full_prompt}")
            images = await client.wait_for_completion(
                prompt_id,
                timeout=int(self._cfg("timeout", 300)),
            )
            for image_info in images:
                path = await client.download_image(image_info, save_dir)
                # 3. 结果图片保存后写入任务。
                if not task.result_path:
                    task.result_path = str(path)
                # 作为对触发消息的回复发出：前置 Reply 组件引用原消息，并 @ 发送者。
                result = event.make_result()
                try:
                    result.chain.append(Reply(id=event.message_obj.message_id))
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

    def _create_task(
        self, trigger_message_id: str, trigger_text: str = ""
    ) -> ImageGenTask:
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

    def _build_workflow(self, positive_prompt: str) -> dict:
        """载入当前激活的工作流，注入正向提示词并为 KSampler 随机化种子。

        Args:
            positive_prompt: The content prompt to inject.

        Returns:
            The API-format workflow dict.

        Raises:
            FileNotFoundError: When the active workflow file does not exist.
            RuntimeError: When no supported prompt node is found in the workflow.
        """
        active = self._get_active_entry()
        if not active or active["source"] != "local":
            raise RuntimeError("当前激活的工作流不是本地工作流。")
        workflow_path = self._resolve_local_path(active["key"])
        workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
        prompt_node_id = self._find_prompt_node(workflow)
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

    async def _build_runninghub_overrides(
        self, webapp_id: str, positive_prompt: str
    ) -> tuple[list[dict], str]:
        """Build RunningHub field overrides for a text-to-image run.

        Fetches the workflow's modifiable nodes and produces ``nodeInfoList``
        entries that (a) set the positive prompt on the first matching text
        node, (b) randomize any KSampler seed, and (c) apply the configured
        sampler steps. The negative-prompt node is left untouched.

        Args:
            webapp_id: The numeric RunningHub workflow id.
            positive_prompt: The LLM-generated content prompt.

        Returns:
            A tuple of ``(node_info_list, full_prompt)`` where ``full_prompt``
            is the quality-prefixed prompt actually sent to the workflow.

        Raises:
            RunningHubError: When node info cannot be fetched or no text node
                matches for the positive prompt.
        """
        client = RunningHubClient(
            str(self._cfg("runninghub_api_key", "") or ""),
            str(self._cfg("runninghub_base_url", "https://www.runninghub.cn")),
        )
        nodes = await client.get_node_info(int(webapp_id))
        full_prompt = f"{QUALITY_PREFIX}, {positive_prompt}"

        field_name = (
            str(self._cfg("runninghub_prompt_field", "text") or "text").strip()
            or "text"
        )
        steps = int(self._cfg("sampler_steps", 12))
        seed = random.randrange(1 << 63)

        node_info_list: list[dict] = []
        prompt_set = False
        for node in nodes:
            name = str(node.get("nodeName", ""))
            field = str(node.get("fieldName", ""))
            if not prompt_set and field == field_name:
                # 第一个匹配的文本节点视为正向提示词；其余（如负向）保持不变。
                node_info_list.append(
                    {
                        "nodeId": node["nodeId"],
                        "fieldName": field,
                        "fieldValue": full_prompt,
                    }
                )
                prompt_set = True
            elif name == "KSampler" and field in ("seed", "steps"):
                value = seed if field == "seed" else steps
                node_info_list.append(
                    {
                        "nodeId": node["nodeId"],
                        "fieldName": field,
                        "fieldValue": value,
                    }
                )
        if not prompt_set:
            raise RunningHubError(
                f"工作流中没有名为「{field_name}」的可修改文本节点，无法注入提示词。"
            )
        return node_info_list, full_prompt

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
