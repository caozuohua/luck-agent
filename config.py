"""
config.py — 配置中心
所有配置从环境变量 + GCP Secret Manager 加载，零硬编码。
"""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from google.cloud import secretmanager
import structlog

log = structlog.get_logger()


@dataclass
class Config:
    # ── GCP ──────────────────────────────────────────────────────
    GCP_PROJECT: str = field(default_factory=lambda: os.environ["GCP_PROJECT"])
    GCP_LOCATION: str = field(default_factory=lambda: os.environ.get("GCP_LOCATION", "us-central1"))

    # ── Models ───────────────────────────────────────────────────
    MODEL_PRO:   str = "gemini-2.5-pro"
    MODEL_FLASH: str = "gemini-2.5-flash"
    MODEL_LITE:  str = "gemini-2.5-flash-lite"

    # 触发 pro 的关键词（其余用 flash）
    PRO_KEYWORDS: tuple = (
        "分析", "写作", "规划", "设计", "review", "重构", "总结报告",
        "blog", "文章", "架构", "复杂", "详细", "深度",
    )

    # ── Lark（运行时从 Secret Manager 注入）─────────────────────
    LARK_APP_ID: str = ""
    LARK_APP_SECRET: str = ""

    # ── GitHub（运行时从 Secret Manager 注入）───────────────────
    GITHUB_TOKEN: str = ""
    GITHUB_DEFAULT_OWNER: str = field(default_factory=lambda: os.environ.get("GITHUB_OWNER", ""))

    # ── Hugo Blog ────────────────────────────────────────────────
    HUGO_REPO: str = field(default_factory=lambda: os.environ.get("HUGO_REPO", ""))
    HUGO_BRANCH: str = "main"
    HUGO_CONTENT_PATH: str = "content/posts"

    # ── Shell ─────────────────────────────────────────────────────
    SHELL_WORK_DIR: str = field(default_factory=lambda: os.environ.get("SHELL_WORK_DIR", "/opt/workspace"))
    SHELL_TIMEOUT: int = 60           # seconds per command
    SHELL_MAX_OUTPUT: int = 4000      # chars truncated to card

    # ── File Bridge ───────────────────────────────────────────────
    FILE_UPLOAD_DIR: str = field(default_factory=lambda: os.environ.get("FILE_DIR", "/opt/lark-agent/files"))
    FILE_MAX_SIZE_MB: int = 50

    # ── Memory ────────────────────────────────────────────────────
    DB_PATH: str = field(default_factory=lambda: os.environ.get("DB_PATH", "/opt/lark-agent/memory.db"))
    SESSION_TTL_SEC: int = 1800       # 30 min 无活动清理 session
    MEMORY_MAX_CONTEXT: int = 20      # 携带最近 N 条记忆给模型

    # ── Task Queue ────────────────────────────────────────────────
    TASK_WORKERS: int = 3
    TASK_MAX_RETRY: int = 2

    def load_secrets(self) -> None:
        """启动时从 Secret Manager 拉取敏感配置（只调一次）。"""
        client = secretmanager.SecretManagerServiceClient()
        p = self.GCP_PROJECT

        def _get(name: str, fallback_env: str = "") -> str:
            env_val = os.environ.get(fallback_env, "")
            if env_val:
                return env_val
            try:
                resp = client.access_secret_version(
                    request={"name": f"projects/{p}/secrets/{name}/versions/latest"}
                )
                return resp.payload.data.decode().strip()
            except Exception as e:
                log.warning("secret_not_found", name=name, error=str(e))
                return ""

        self.LARK_APP_ID     = _get("lark-app-id",     "LARK_APP_ID")
        self.LARK_APP_SECRET = _get("lark-app-secret", "LARK_APP_SECRET")
        self.GITHUB_TOKEN    = _get("github-token",    "GITHUB_TOKEN")

        os.makedirs(self.FILE_UPLOAD_DIR, exist_ok=True)
        os.makedirs(self.SHELL_WORK_DIR,  exist_ok=True)

        log.info("config_loaded",
                 lark_app=self.LARK_APP_ID[:6] + "***",
                 github_token=bool(self.GITHUB_TOKEN),
                 hugo_repo=self.HUGO_REPO)

    def pick_model(self, text: str) -> str:
        """根据用户输入内容智能选择模型。"""
        text_lower = text.lower()
        if any(kw in text_lower for kw in self.PRO_KEYWORDS):
            return self.MODEL_PRO
        if len(text) > 500:
            return self.MODEL_FLASH
        return self.MODEL_LITE


# 全局单例
cfg = Config()
