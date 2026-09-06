"""Minimal async client for the ComfyUI HTTP API.

Only the endpoints this plugin needs are implemented:

- ``POST /prompt`` — submit a workflow.
- ``GET /history/{prompt_id}`` — poll a submitted workflow until it finishes.
- ``GET /view`` — download an output image.

ComfyUI must be running with the Anima custom nodes
(``AnimaMultiLoraLoader`` / ``AnimaDAVE``) and the "easy" node pack
(``easy cleanGpuUsed``) installed for the shipped ``anima.json`` workflow.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import httpx


class ComfyUIError(RuntimeError):
    """Raised when ComfyUI reports a workflow error or unexpected output."""


class ComfyUIClient:
    """Minimal async client for the ComfyUI HTTP API.

    Args:
        server_url: ComfyUI base URL, e.g. ``"http://127.0.0.1:8188"``.
    """

    def __init__(self, server_url: str) -> None:
        self.server_url = server_url.rstrip("/")
        self.client_id = str(uuid.uuid4())

    async def submit_workflow(self, workflow: dict) -> str:
        """Submit a workflow via ``POST /prompt`` and return the prompt id.

        Args:
            workflow: The ComfyUI API-format workflow dict (``class_type`` +
                ``inputs`` per node).

        Returns:
            The generated ``prompt_id``.

        Raises:
            ComfyUIError: If ComfyUI rejects the submission.
        """
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{self.server_url}/prompt",
                json={"prompt": workflow, "client_id": self.client_id},
            )
            resp.raise_for_status()
            data = resp.json()
        if not data.get("prompt_id"):
            raise ComfyUIError(f"ComfyUI 提交失败: {data}")
        return data["prompt_id"]

    async def wait_for_completion(
        self,
        prompt_id: str,
        timeout: int = 300,
        poll_interval: float = 2.0,
    ) -> list[dict]:
        """Poll ``/history/{prompt_id}`` until the workflow finishes.

        Args:
            prompt_id: The prompt id returned by :meth:`submit_workflow`.
            timeout: Maximum time in seconds to wait.
            poll_interval: Seconds between history polls.

        Returns:
            A list of output image infos, each ``{"filename", "subfolder",
            "type"}``.

        Raises:
            ComfyUIError: When the workflow errors out or produces no images.
            TimeoutError: When generation exceeds ``timeout`` seconds.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                resp = await client.get(f"{self.server_url}/history/{prompt_id}")
                resp.raise_for_status()
                entry = resp.json().get(prompt_id)
                if entry:
                    status = entry.get("status", {})
                    if status.get("status_str") == "error":
                        raise ComfyUIError(
                            f"ComfyUI 执行出错: {self._error_message(entry)}"
                        )
                    if status.get("completed"):
                        images: list[dict] = []
                        for node_output in entry.get("outputs", {}).values():
                            images.extend(node_output.get("images", []))
                        if images:
                            return images
                        raise ComfyUIError("ComfyUI 工作流完成，但未产出图片。")
                if loop.time() >= deadline:
                    raise TimeoutError(f"ComfyUI 生成超时（{timeout} 秒）。")
                await asyncio.sleep(poll_interval)

    @staticmethod
    def _error_message(entry: dict) -> str:
        """Extract a human-readable message from a failed history entry."""
        for msg in entry.get("status", {}).get("messages", []):
            if msg and msg[0] == "execution_error":
                data = msg[1] or {}
                return str(data.get("exception_message", "unknown error"))
        return "unknown error"

    async def download_image(self, image_info: dict, save_dir: Path) -> Path:
        """Download an output image to ``save_dir`` and return its local path.

        Args:
            image_info: An image info dict from :meth:`wait_for_completion`.
            save_dir: Directory to save the image into.

        Returns:
            The local path of the downloaded image.
        """
        params = {
            "filename": image_info["filename"],
            "type": image_info.get("type", "output"),
        }
        if subfolder := image_info.get("subfolder"):
            params["subfolder"] = subfolder
        save_dir.mkdir(parents=True, exist_ok=True)
        save_path = save_dir / image_info["filename"]
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.get(f"{self.server_url}/view", params=params)
            resp.raise_for_status()
            save_path.write_bytes(resp.content)
        return save_path


class RunningHubError(RuntimeError):
    """Raised when a RunningHub API call fails or reports an error."""


# 轮询 /task/openapi/outputs 时，处于排队/运行中的任务会以非零 code + 这些
# 关键字的 msg 返回；据此与真正的错误区分开。
_RUNNINGHUB_PENDING_TOKENS = ("QUEUED", "RUNNING", "排队", "运行中")


class RunningHubClient:
    """Minimal async client for the RunningHub (cloud ComfyUI) HTTP API.

    Runs workflows that already exist in a user's RunningHub account,
    identified by their numeric ``webappId``. Only field overrides are sent;
    the workflow itself lives on RunningHub and must have been run at least
    once on the web before it can be invoked via API.

    Args:
        api_key: The RunningHub API key.
        base_url: RunningHub base URL (default ``https://www.runninghub.cn``).
    """

    def __init__(
        self, api_key: str, base_url: str = "https://www.runninghub.cn"
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    async def get_node_info(self, webapp_id: int) -> list[dict]:
        """Fetch the modifiable nodes of a RunningHub workflow.

        Args:
            webapp_id: The numeric RunningHub workflow (webapp) id.

        Returns:
            A list of node dicts with ``nodeId``/``nodeName``/``fieldName``/
            ``fieldValue``/``fieldType``/``description``.

        Raises:
            RunningHubError: When the API errors or no modifiable nodes exist.
        """
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(
                f"{self.base_url}/api/webapp/apiCallDemo",
                params={"apiKey": self.api_key, "webappId": webapp_id},
            )
            resp.raise_for_status()
            data = resp.json()
        if data.get("code") != 0:
            raise RunningHubError(f"获取工作流节点信息失败: {data.get('msg', data)}")
        nodes = (data.get("data") or {}).get("nodeInfoList", [])
        if not nodes:
            raise RunningHubError(
                "该工作流没有可修改的节点。请先在 RunningHub 网页上成功运行一次，"
                "之后才能通过 API 调用。"
            )
        return nodes

    async def submit(self, webapp_id: int, node_info_list: list[dict]) -> str:
        """Submit a workflow run with field overrides; returns the task id.

        Args:
            webapp_id: The numeric RunningHub workflow (webapp) id.
            node_info_list: Overrides as ``{"nodeId", "fieldName", "fieldValue"}``.

        Returns:
            The generated ``taskId``.

        Raises:
            RunningHubError: When the API rejects the submission.
        """
        payload = {
            "workflowId": int(webapp_id),
            "apiKey": self.api_key,
            "nodeInfoList": node_info_list,
        }
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{self.base_url}/task/openapi/create", json=payload
            )
            resp.raise_for_status()
            data = resp.json()
        if data.get("code") != 0:
            raise RunningHubError(f"提交工作流失败: {data.get('msg', data)}")
        task_id = (data.get("data") or {}).get("taskId")
        if not task_id:
            raise RunningHubError(f"提交成功但未返回 taskId: {data}")
        return str(task_id)

    async def wait_for_completion(
        self, task_id: str, timeout: int = 300, poll_interval: float = 5.0
    ) -> list[str]:
        """Poll ``/task/openapi/outputs`` until the task finishes.

        Args:
            task_id: The task id returned by :meth:`submit`.
            timeout: Maximum time in seconds to wait.
            poll_interval: Seconds between polls.

        Returns:
            A list of output image URLs (directly downloadable).

        Raises:
            RunningHubError: When the workflow errors out or produces no images.
            TimeoutError: When generation exceeds ``timeout`` seconds.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        async with httpx.AsyncClient(timeout=60) as client:
            while True:
                resp = await client.post(
                    f"{self.base_url}/task/openapi/outputs",
                    json={"taskId": task_id, "apiKey": self.api_key},
                )
                resp.raise_for_status()
                data = resp.json()
                if data.get("code") == 0:
                    outputs = data.get("data") or []
                    urls = [o["fileUrl"] for o in outputs if o.get("fileUrl")]
                    if urls:
                        return urls
                    raise RunningHubError("工作流完成，但未产出图片。")
                msg = str(data.get("msg", ""))
                is_pending = any(
                    token in msg or token.upper() in msg.upper()
                    for token in _RUNNINGHUB_PENDING_TOKENS
                )
                if not is_pending:
                    raise RunningHubError(f"工作流执行出错: {data}")
                if loop.time() >= deadline:
                    raise TimeoutError(f"RunningHub 生成超时（{timeout} 秒）。")
                await asyncio.sleep(poll_interval)

    async def download_image(self, url: str, save_dir: Path) -> Path:
        """Download an image URL to ``save_dir`` and return its local path.

        Args:
            url: The direct image URL from :meth:`wait_for_completion`.
            save_dir: Directory to save the image into.

        Returns:
            The local path of the downloaded image.
        """
        name = url.split("?")[0].rsplit("/", 1)[-1] or "image.png"
        if "." not in name:
            name += ".png"
        save_dir.mkdir(parents=True, exist_ok=True)
        save_path = save_dir / name
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            save_path.write_bytes(resp.content)
        return save_path
