"""
config.py — 配置中心（.env 文件版）
启动时从 .env 文件加载所有配置，无需 Secret Manager 网络请求。

.env 文件加载优先级：
  1. ENV_FILE 环境变量指定的路径
  2. 程序同目录下的 .env
  3. /opt/luck-agent/.env（systemd 生产路径）

GCP 认证优先级：
  1. .env 中 GOOGLE_APPLICATION_CREDENTIALS 指向的 JSON key 文件
  2. GCE 实例 ADC（实例 scope 含 cloud-platform 时自动生效）
  3. gcloud auth application-default login（本地开发）
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import google.auth

from core.log import get_logger

log = get_logger()


# ─── .env 解析（不依赖 python-dotenv，零额外依赖）────────────────────────────

def _load_env_file(path: str) -> dict[str, str]:
    """
    解析 .env 文件，支持：
      KEY=VALUE
      KEY="VALUE WITH SPACES"
      KEY='VALUE'
      # 注释行、空行
    返回 key-value 字典（不覆盖已有环境变量）。
    """
    p = Path(path)
    if not p.exists():
        return {}

    result: dict[str, str] = {}
    for lineno, raw in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            log.warning("env_parse_skip", file=path, line=lineno, content=line)
            continue

        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()

        # 去掉引号
        if len(val) >= 2 and val[0] in ('"', "'") and val[-1] == val[0]:
            val = val[1:-1]
        # 行内注释（仅无引号的值）
        elif " #" in val:
            val = val.split(" #", 1)[0].strip()

        result[key] = val

    return result


def _find_env_file() -> str:
    """按优先级查找 .env 文件路径。"""
    candidates = [
        os.environ.get("ENV_FILE", ""),
        str(Path(__file__).parent / ".env"),
        "/opt/luck-agent/.env",
    ]
    for path in candidates:
        if path and Path(path).exists():
            return path
    return ""


# ─── 模块导入时执行：加载 .env → 注入 os.environ ─────────────────────────────

_env_file = _find_env_file()
_env_data = _load_env_file(_env_file)

# 已有系统环境变量不覆盖（优先级：系统 > .env）
for _k, _v in _env_data.items():
    os.environ.setdefault(_k, _v)

if _env_file:
    log.info("env_loaded", path=_env_file, keys=len(_env_data))
else:
    log.warning("env_file_not_found",
                hint="创建 .env 或设置 ENV_FILE 环境变量，参考 .env.example")


# ─── 读取辅助 ─────────────────────────────────────────────────────────────────

def _req(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        raise RuntimeError(
            f"缺少必填配置：{key}\n"
            f"请在 .env 文件中添加：{key}=your_value"
        )
    return val


def _opt(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except ValueError:
        return default


# ─── Config ───────────────────────────────────────────────────────────────────

class Config:
    # GCP
    GCP_PROJECT:  str = ""
    GCP_LOCATION: str = ""
    GCP_KEY_FILE: str = ""

    # Models
    MODEL_PRO:   str = "gemini-2.5-pro"
    MODEL_FLASH: str = "gemini-2.5-flash"
    MODEL_LITE:  str = "gemini-2.5-flash-lite"
    PRO_KEYWORDS: tuple = (
        "分析", "写作", "规划", "设计", "review", "重构", "总结报告",
        "blog", "文章", "架构", "复杂", "详细", "深度",
    )

    # Lark
    LARK_APP_ID:     str = ""
    LARK_APP_SECRET: str = ""
    # 飞书国内版：https://open.feishu.cn  |  Lark国际版：https://open.larksuite.com
    LARK_DOMAIN:     str = "https://open.larksuite.com"

    # GitHub
    GITHUB_TOKEN:         str = ""
    GITHUB_DEFAULT_OWNER: str = ""

    # Hugo
    HUGO_REPO:         str = ""
    HUGO_BRANCH:       str = "main"
    HUGO_CONTENT_PATH: str = "content/posts"

    # Shell
    SHELL_WORK_DIR:   str = "/opt/workspace"
    SHELL_TIMEOUT:    int = 60
    SHELL_MAX_OUTPUT: int = 4000

    # File Bridge
    FILE_UPLOAD_DIR:  str = "/opt/luck-agent/files"
    FILE_MAX_SIZE_MB: int = 50

    # Memory
    DB_PATH:              str = "/opt/luck-agent/memory.db"
    SESSION_TTL_SEC:      int = 1800
    MEMORY_MAX_CONTEXT:   int = 20

    # Task Queue
    TASK_WORKERS:   int = 3
    TASK_MAX_RETRY: int = 2

    def load(self) -> None:
        """从已注入的环境变量读取所有配置，启动时调用一次。"""

        # 必填
        self.GCP_PROJECT     = _req("GCP_PROJECT")
        self.LARK_APP_ID     = _req("LARK_APP_ID")
        self.LARK_APP_SECRET = _req("LARK_APP_SECRET")
        self.GITHUB_TOKEN    = _req("GITHUB_TOKEN")

        # 可选
        self.GCP_LOCATION         = _opt("GCP_LOCATION",    "us-central1")
        self.GCP_KEY_FILE         = _opt("GOOGLE_APPLICATION_CREDENTIALS")
        self.GITHUB_DEFAULT_OWNER = _opt("GITHUB_OWNER")
        self.LARK_DOMAIN          = _opt("LARK_DOMAIN", "https://open.larksuite.com")
        self.HUGO_REPO            = _opt("HUGO_REPO")
        self.HUGO_BRANCH          = _opt("HUGO_BRANCH",         "main")
        self.HUGO_CONTENT_PATH    = _opt("HUGO_CONTENT_PATH",   "content/posts")
        self.SHELL_WORK_DIR       = _opt("SHELL_WORK_DIR",      "/opt/workspace")
        self.FILE_UPLOAD_DIR      = _opt("FILE_DIR",            "/opt/luck-agent/files")
        self.DB_PATH              = _opt("DB_PATH",             "/opt/luck-agent/memory.db")

        # 数值型
        self.SHELL_TIMEOUT      = _int("SHELL_TIMEOUT",      60)
        self.SHELL_MAX_OUTPUT   = _int("SHELL_MAX_OUTPUT",   4000)
        self.FILE_MAX_SIZE_MB   = _int("FILE_MAX_SIZE_MB",   50)
        self.SESSION_TTL_SEC    = _int("SESSION_TTL_SEC",    1800)
        self.MEMORY_MAX_CONTEXT = _int("MEMORY_MAX_CONTEXT", 20)
        self.TASK_WORKERS       = _int("TASK_WORKERS",       3)
        self.TASK_MAX_RETRY     = _int("TASK_MAX_RETRY",     2)

        # 目录初始化
        os.makedirs(self.FILE_UPLOAD_DIR, exist_ok=True)
        os.makedirs(self.SHELL_WORK_DIR,  exist_ok=True)
        os.makedirs(os.path.dirname(self.DB_PATH) or ".", exist_ok=True)

        # GCP 认证
        auth_mode = self._detect_auth()

        log.info("config_loaded",
                 auth=auth_mode,
                 gcp_project=self.GCP_PROJECT,
                 lark_app=self.LARK_APP_ID[:6] + "***",
                 github_owner=self.GITHUB_DEFAULT_OWNER,
                 hugo_repo=self.HUGO_REPO)

    def _detect_auth(self) -> str:
        """检测并初始化 GCP 认证，返回模式描述。"""
        if self.GCP_KEY_FILE:
            if not Path(self.GCP_KEY_FILE).exists():
                raise FileNotFoundError(
                    f"GCP key 文件不存在：{self.GCP_KEY_FILE}\n"
                    f"请检查 .env 中 GOOGLE_APPLICATION_CREDENTIALS 路径。"
                )
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = self.GCP_KEY_FILE
            with open(self.GCP_KEY_FILE) as f:
                sa = json.load(f).get("client_email", "unknown")
            log.info("gcp_auth", mode="key_file", sa=sa)
            return f"key_file:{sa}"

        try:
            _, project = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            log.info("gcp_auth", mode="adc", project=project or self.GCP_PROJECT)
            return "adc"
        except google.auth.exceptions.DefaultCredentialsError as e:
            raise RuntimeError(
                "GCP 认证失败。请在 .env 中添加：\n"
                "  GOOGLE_APPLICATION_CREDENTIALS=/opt/luck-agent/credentials/gcp-key.json\n"
                f"原始错误：{e}"
            ) from e

    def pick_model(self, text: str) -> str:
        text_lower = text.lower()
        if any(kw in text_lower for kw in self.PRO_KEYWORDS):
            return self.MODEL_PRO
        if len(text) > 500:
            return self.MODEL_FLASH
        return self.MODEL_LITE


# 全局单例（load() 在 agent.py 启动时调用）
cfg = Config()
