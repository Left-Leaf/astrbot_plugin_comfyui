#!/usr/bin/env python3
"""Standalone test: submit the Anima workflow to a running ComfyUI and download the result.

This exercises exactly the same ComfyUI calls the plugin makes (POST /prompt,
GET /history, GET /view), but without needing AstrBot, an LLM, or httpx — it
only uses the Python standard library. Run it from any Python 3.10+:

    python scripts/test_comfyui.py [workflow.json] [options]

Options:
    --server URL    ComfyUI base URL (default: http://127.0.0.1:8188)
    --prompt TEXT   Positive prompt to inject (default: a sample scene)
    --out DIR       Where to save generated images (default: ../test_output)

Examples:
    python scripts/test_comfyui.py                          # run the shipped workflow
    python scripts/test_comfyui.py ..\\..\\plugin_data\\astrbot_plugin_comfyui\\anima.json
    python scripts/test_comfyui.py --server http://192.168.1.10:8188 --prompt "1girl, sakura"
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_PROMPT_NODE_ID = "6"
DEFAULT_PROMPT = (
    "1girl, long white hair, blue eyes, white sundress, standing under a "
    "sakura tree, petals falling, direct eye contact, facing viewer"
)
DEFAULT_TIMEOUT = 300


def http_json(method: str, url: str, payload: dict | None = None) -> dict:
    """Perform an HTTP request and return the JSON response.

    Raises:
        RuntimeError: If the server returns a non-2xx status.
    """
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} from {url}: {body[:500]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"无法连接 ComfyUI（{url}）：{e.reason}") from e


def build_workflow(workflow_path: Path, positive_prompt: str) -> dict:
    """Load a workflow, inject the prompt and randomize the KSampler seed."""
    workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    prompt_node_id = DEFAULT_PROMPT_NODE_ID
    for node_id, node in workflow.items():
        meta = node.get("_meta") or {}
        if meta.get("is_positive_prompt"):
            prompt_node_id = str(node_id)
            break
    if prompt_node_id not in workflow:
        raise RuntimeError(
            f"工作流中找不到提示词节点 {prompt_node_id}。"
        )
    workflow[prompt_node_id]["inputs"]["text"] = positive_prompt
    for node in workflow.values():
        if node.get("class_type") == "KSampler":
            node["inputs"]["seed"] = random.randrange(1 << 63)
    return workflow


def wait_for_images(server: str, prompt_id: str, timeout: int) -> list[dict]:
    """Poll /history until the workflow completes and images are available."""
    deadline = time.monotonic() + timeout
    while True:
        history = http_json("GET", f"{server}/history/{prompt_id}")
        entry = history.get(prompt_id)
        if entry:
            status = entry.get("status", {})
            if status.get("status_str") == "error":
                raise RuntimeError(f"ComfyUI 执行出错：{error_message(entry)}")
            if status.get("completed"):
                images = []
                for node_output in entry.get("outputs", {}).values():
                    images.extend(node_output.get("images", []))
                if images:
                    return images
                raise RuntimeError("ComfyUI 工作流完成，但未产出图片。")
        if time.monotonic() >= deadline:
            raise TimeoutError(f"ComfyUI 生成超时（{timeout} 秒）。")
        time.sleep(2)


def error_message(entry: dict) -> str:
    """Extract a human-readable error from a failed history entry."""
    for msg in entry.get("status", {}).get("messages", []):
        if msg and msg[0] == "execution_error":
            data = msg[1] or {}
            return str(data.get("exception_message", "unknown error"))
    return "unknown error"


def download_image(server: str, image_info: dict, out_dir: Path) -> Path:
    """Download one output image and return its local path."""
    params = {
        "filename": image_info["filename"],
        "type": image_info.get("type", "output"),
    }
    if subfolder := image_info.get("subfolder"):
        params["subfolder"] = subfolder
    url = f"{server}/view?{urllib.parse.urlencode(params)}"
    out_dir.mkdir(parents=True, exist_ok=True)
    save_path = out_dir / image_info["filename"]
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:
            save_path.write_bytes(resp.read())
    except urllib.error.URLError as e:
        raise RuntimeError(f"下载图片失败（{url}）：{e.reason}") from e
    return save_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "workflow",
        nargs="?",
        default=str(Path(__file__).parent.parent / "anima.json"),
        help="API 格式工作流 JSON 路径",
    )
    parser.add_argument("--server", default="http://127.0.0.1:8188")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument(
        "--out",
        default=str(Path(__file__).parent.parent / "test_output"),
    )
    args = parser.parse_args()

    server = args.server.rstrip("/")
    workflow_path = Path(args.workflow)
    if not workflow_path.exists():
        print(f"[错误] 工作流文件不存在：{workflow_path}")
        return 1

    print(f"[1/4] 载入工作流: {workflow_path}")
    workflow = build_workflow(workflow_path, args.prompt)

    print(f"[2/4] 提交到 ComfyUI: {server}")
    resp = http_json(
        "POST",
        f"{server}/prompt",
        {"prompt": workflow, "client_id": "astrbot-comfyui-test"},
    )
    if "prompt_id" not in resp:
        print(f"[错误] 提交失败：{resp}")
        return 1
    prompt_id = resp["prompt_id"]
    print(f"      prompt_id: {prompt_id}")

    print("[3/4] 等待生成完成（最长 300 秒）...")
    images = wait_for_images(server, prompt_id, DEFAULT_TIMEOUT)

    print(f"[4/4] 下载 {len(images)} 张图片")
    out_dir = Path(args.out)
    for info in images:
        path = download_image(server, info, out_dir)
        view_url = f"{server}/view?{urllib.parse.urlencode({'filename': info['filename'], 'type': info.get('type', 'output')})}"
        print(f"      已保存: {path}")
        print(f"      浏览器查看: {view_url}")

    print("\n生成成功！也可在 ComfyUI WebUI 的右侧 History 面板点击该任务查看。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
