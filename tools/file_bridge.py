"""
tools/file_bridge.py — Lark ↔ VPS 文件收发桥
大模型不可用时仍然工作（纯 HTTP，无 AI 依赖）。
支持：上传到 VPS / 从 VPS 下载到 Lark / 文件列表卡片
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import os
import time
from pathlib import Path

import httpx
from core.log import get_logger

log = get_logger()


class FileBridge:
    """Lark 文件 API 封装 + 本地存储。"""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        storage_dir: str,
        max_size_mb: int = 50,
        domain: str = "https://open.larksuite.com",
    ) -> None:
        self.app_id      = app_id
        self.app_secret  = app_secret
        self.storage     = Path(storage_dir)
        self.max_size_mb = max_size_mb
        self.api_base    = domain.rstrip("/") + "/open-apis"
        self.storage.mkdir(parents=True, exist_ok=True)
        self._token: str       = ""
        self._token_exp: float = 0

    # ── Lark Token ──────────────────────────────────────────────────
    async def _get_token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        async with httpx.AsyncClient() as c:
            resp = await c.post(
                f"{self.api_base}/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self.app_secret},
            )
            data = resp.json()
            self._token     = data["tenant_access_token"]
            self._token_exp = time.time() + data.get("expire", 7200)
        return self._token

    def _auth_headers(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    # ── 从 Lark 接收文件 → 保存到 VPS ───────────────────────────────
    async def download_from_lark(
        self,
        file_key: str,
        file_name: str,
        message_type: str = "file",  # file / image
    ) -> dict:
        """
        从 Lark 消息中下载文件到 VPS。
        返回 {"local_path": str, "size": int, "md5": str}
        """
        token = await self._get_token()

        # 选择正确的 API endpoint
        if message_type == "image":
            url = f"{self.api_base}/im/v1/images/{file_key}"
        else:
            url = f"{self.api_base}/im/v1/files/{file_key}/content"

        async with httpx.AsyncClient(timeout=120) as c:
            resp = await c.get(url, headers=self._auth_headers(token))
            resp.raise_for_status()

            # 检查大小
            size = int(resp.headers.get("content-length", 0))
            if size > self.max_size_mb * 1024 * 1024:
                raise ValueError(f"文件超过限制（{self.max_size_mb}MB）")

            content = resp.content

        # 安全文件名 + MD5 去重
        safe_name   = self._safe_filename(file_name)
        md5         = hashlib.md5(content).hexdigest()[:8]
        final_name  = f"{int(time.time())}_{md5}_{safe_name}"
        local_path  = self.storage / final_name
        local_path.write_bytes(content)

        log.info("file_received", name=file_name, size=len(content), path=str(local_path))
        return {
            "local_path": str(local_path),
            "file_name":  file_name,
            "size":       len(content),
            "md5":        md5,
        }

    # ── 从 VPS 发送文件到 Lark ───────────────────────────────────────
    async def upload_to_lark(
        self,
        local_path: str,
        chat_id: str,
        caption: str = "",
    ) -> dict:
        """
        上传 VPS 文件到 Lark 聊天。
        返回 {"message_id": str, "file_key": str}
        """
        path = Path(local_path)
        if not path.exists():
            raise FileNotFoundError(f"文件不存在：{local_path}")

        size_mb = path.stat().st_size / 1024 / 1024
        if size_mb > self.max_size_mb:
            raise ValueError(f"文件 {size_mb:.1f}MB 超过 {self.max_size_mb}MB 限制")

        token     = await self._get_token()
        mime_type, _ = mimetypes.guess_type(path.name)
        mime_type = mime_type or "application/octet-stream"
        is_image  = mime_type.startswith("image/")

        async with httpx.AsyncClient(timeout=120) as c:
            # Step 1: 上传文件获取 file_key
            if is_image:
                upload_url = f"{self.api_base}/im/v1/images"
                files = {"image": (path.name, path.read_bytes(), mime_type)}
                data  = {"image_type": "message"}
                resp  = await c.post(upload_url, headers=self._auth_headers(token),
                                     files=files, data=data)
                resp.raise_for_status()
                file_key = resp.json()["data"]["image_key"]
                msg_type = "image"
                content  = f'{{"image_key":"{file_key}"}}'
            else:
                upload_url = f"{self.api_base}/im/v1/files"
                files = {"file": (path.name, path.read_bytes(), mime_type)}
                data  = {"file_type": "stream", "file_name": path.name}
                resp  = await c.post(upload_url, headers=self._auth_headers(token),
                                     files=files, data=data)
                resp.raise_for_status()
                file_key = resp.json()["data"]["file_key"]
                msg_type = "file"
                content  = f'{{"file_key":"{file_key}"}}'

            # Step 2: 发送消息
            # receive_id_type 必须作为 query param 传递
            # content 已是 JSON 字符串，用 data= 而非 json= 避免二次序列化
            send_resp = await c.post(
                f"{self.api_base}/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                headers={**self._auth_headers(token), "Content-Type": "application/json"},
                content=json.dumps({
                    "receive_id": chat_id,
                    "msg_type":   msg_type,
                    "content":    content,
                }).encode(),
            )
            if not send_resp.is_success:
                raise RuntimeError(
                    f"发送消息失败 {send_resp.status_code}: "
                    f"{send_resp.json().get('msg', send_resp.text)}"
                )
            msg_id = send_resp.json()["data"]["message_id"]

        log.info("file_sent", path=local_path, chat_id=chat_id, file_key=file_key)

        # 如果有 caption，跟发一条文字
        if caption:
            await asyncio.sleep(0.3)
            async with httpx.AsyncClient() as c2:
                await c2.post(
                    f"{self.api_base}/im/v1/messages",
                    params={"receive_id_type": "chat_id"},
                    headers={**self._auth_headers(token), "Content-Type": "application/json"},
                    content=json.dumps({
                        "receive_id": chat_id,
                        "msg_type":   "text",
                        "content":    json.dumps({"text": caption}),
                    }).encode(),
                )

        return {"message_id": msg_id, "file_key": file_key, "file_name": path.name}

    # ── 列出 VPS 存储目录 ─────────────────────────────────────────────
    def list_stored_files(self, limit: int = 20) -> list[dict]:
        files = []
        for p in sorted(self.storage.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if p.is_file():
                files.append({
                    "name":       p.name,
                    "size_kb":    round(p.stat().st_size / 1024, 1),
                    "modified":   p.stat().st_mtime,
                    "full_path":  str(p),
                })
        return files[:limit]

    def _safe_filename(self, name: str) -> str:
        """清洗文件名。"""
        import re
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
        return name[:100]