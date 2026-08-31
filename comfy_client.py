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
