"""
core/intent_router.py — 意图路由器（零 AI，纯规则）

第一性原理：
  不让模型从 18 个工具里自选，而是先用正则/关键词识别意图，
  再给模型注入：① 专用 prompt（带具体示例）② 最小工具子集（3-5个）
  模型只需"填空"，不需要做工具选择决策，准确率大幅提升。
"""
意图分类：
  BLOG_WRITE     写/发/更新博客文章
  BLOG_LIST      列出博客文章
  GITHUB_ACTION  触发/查看 CI/Actions
  GITHUB_ISSUE   Issues / PR 管理
  GITHUB_CODE    读取/更新仓库文件
  SHELL_RUN      在 VPS 执行命令/脚本
  FILE_OP        VPS 文件读写操作
  MEMORY_OP      记忆读写
  SCHEDULE_OP    定时任务
  GIT_PUSH       推送代码
  SEARCH         搜索（Vercel Tavily 优先）
  GENERAL        兜底，给全量工具
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class Intent(str, Enum):
    BLOG_WRITE     = "blog_write"
    BLOG_LIST      = "blog_list"
    GITHUB_ACTION  = "github_action"
    GITHUB_ISSUE   = "github_issue"
    GITHUB_CODE    = "github_code"
    SHELL_RUN      = "shell_run"
    FILE_OP        = "file_op"
    MEMORY_OP      = "memory_op"
    SCHEDULE_OP    = "schedule_op"
    GIT_PUSH       = "git_push"
    SEARCH         = "search"
    GENERAL        = "general"


@dataclass
class RouteResult:
    intent:     Intent
    confidence: float          # 0.0-1.0，高置信度跳过自动探索步骤
    tool_names: list[str]      # 给模型的最小工具子集
    prompt_hint: str           # 注入系统 Prompt 的任务提示（含示例）
    model_hint: str = "flash"  # 建议使用的模型等级


# ── 规则定义 ──────────────────────────────────────────────────────────────────
# 每条规则：(意图, 置信度, 关键词列表, 正则列表)
_RULES: list[tuple[Intent, float, list[str], list[str]]] = [

    # 写/发博客（最高优先）
    (Intent.BLOG_WRITE, 0.95, [
        "写博客", "写文章", "发布博客", "发布文章", "发文章", "发一篇",
        "新建文章", "创建博文", "更新博客", "更新文章", "草稿",
        "hugo", "front matter", "博文",
    ], [
        r"(写|发|更新|新建|创建|生成).{0,8}(博客|文章|博文|post)",
        r"(blog|post).{0,15}(写|发|创|update|create|publish)",
    ]),

    # 列出博客文章
    (Intent.BLOG_LIST, 0.90, [
        "列出文章", "看看博客", "查看博客", "博客有哪些", "文章列表",
        "我有哪些文章", "查看所有文章", "博客列表",
    ], [
        r"(列出|查看|看看|显示).{0,6}(文章|博客|博文)",
    ]),

    # GitHub Actions / CI
    (Intent.GITHUB_ACTION, 0.92, [
        "触发部署", "触发deploy", "跑ci", "跑cd", "触发workflow",
        "workflow", "action", "pipeline", "actions状态", "ci状态",
        "部署状态", "查看运行", "workflow失败", "ci失败",
    ], [
        r"(触发|运行|跑|查看|检查).{0,8}(deploy|action|workflow|ci|cd|流水线)",
        r"(action|workflow).{0,10}(状态|结果|日志|失败|成功)",
    ]),

    # Issues / PR
    (Intent.GITHUB_ISSUE, 0.90, [
        "创建issue", "新建issue", "提issue", "查看issue", "issue列表",
        "pull request", "pr列表", "合并pr", "查看pr", "评论issue",
    ], [
        r"(issue|pr|pull request).{0,20}(创建|查看|列出|合并|关闭|评论)",
        r"(创建|新建|提交).{0,6}(issue|问题|缺陷|需求)",
    ]),

    # 读取/更新仓库文件（代码）
    (Intent.GITHUB_CODE, 0.88, [
        "读取文件", "查看文件内容", "更新文件", "修改仓库", "提交文件",
        "仓库里的", "github上的", "读一下", "查看代码",
    ], [
        r"(读取|查看|更新|修改|提交).{0,8}(仓库|repo|github).{0,10}(文件|代码|内容)",
        r"(get_file|update_file)",
    ]),

    # VPS Shell / 脚本执行
    (Intent.SHELL_RUN, 0.92, [
        "执行命令", "运行脚本", "跑一下", "帮我执行", "systemctl",
        "安装", "pip install", "apt install", "重启服务", "查看进程",
        "ps aux", "top", "htop", "nohup", "后台运行",
    ], [
        r"(执行|运行|跑).{0,10}(命令|脚本|shell|bash)",
        r"(安装|卸载|重启|停止|启动).{0,6}(服务|进程|程序|软件)",
        r"(systemctl|apt|pip|npm|yarn|docker)",
    ]),

    # VPS 文件操作（读写 VPS 本地文件，非 GitHub）
    (Intent.FILE_OP, 0.88, [
        "读取vps", "查看日志", "读一下日志", "tail", "grep",
        "磁盘空间", "磁盘使用", "文件大小", "删除文件",
        "/opt/", "/var/", "/home/", "/etc/", "/tmp/",
    ], [
        r"(读取|查看|编辑|删除|写入).{0,6}(日志|log|文件|目录)",
        r"/(opt|var|home|etc|tmp)/\S+",
    ]),

    # 记忆操作
    (Intent.MEMORY_OP, 0.90, [
        "记住", "帮我记", "你还记得", "你记得什么", "忘掉", "删除记忆",
        "记忆里", "你知道我", "我的偏好", "更新偏好",
    ], [
        r"(记住|记一下|帮我记).{0,20}",
        r"(忘掉|忘记|删除).{0,10}(记忆|偏好|信息)",
    ]),

    # 定时任务
    (Intent.SCHEDULE_OP, 0.92, [
        "定时", "每天", "每周", "每小时", "每隔", "cron",
        "自动执行", "定期", "提醒我", "每天早上", "每周一",
    ], [
        r"每(天|日|周|月|小时|分钟).{0,20}(执行|检查|提醒|发送|运行)",
        r"(定时|定期|自动).{0,10}(任务|执行|提醒)",
    ]),

    # Git 推送
    (Intent.GIT_PUSH, 0.93, [
        "推送", "push", "提交代码", "commit并push", "git push",
        "推到github", "同步代码", "上传代码", "提交改动",
    ], [
        r"(推送|push|提交).{0,10}(代码|改动|修改|更新)",
        r"git.{0,6}(push|commit|add)",
    ]),
]


# ── 工具子集映射 ──────────────────────────────────────────────────────────────
TOOL_SUBSETS: dict[Intent, list[str]] = {
    Intent.BLOG_WRITE: [
        "create_blog_post",
        "list_blog_posts",       # 检查是否已有同名文章
        "trigger_workflow",      # 发布后触发部署
    ],
    Intent.BLOG_LIST: [
        "list_blog_posts",
        "get_repo_info",
    ],
    Intent.GITHUB_ACTION: [
        "trigger_workflow",
        "list_workflow_runs",
    ],
    Intent.GITHUB_ISSUE: [
        "list_issues",
        "create_issue",
        "list_prs",
        "comment_on_issue",
    ],
    Intent.GITHUB_CODE: [
        "get_file",
        "update_file",
        "list_commits",
        "get_repo_info",
    ],
    Intent.SHELL_RUN: [
        "run_shell",
        "run_script",
        "disk_usage",
    ],
    Intent.FILE_OP: [
        "run_shell",      # ls / cat / tail 等
        "read_file",
        "write_file",
        "disk_usage",
    ],
    Intent.MEMORY_OP: [
        "remember",
        "recall",
        "forget",
        "show_memory",
    ],
    Intent.SCHEDULE_OP: [
        "schedule_task",
        "list_schedules",
        "cancel_schedule",
        "pause_schedule",
        "resume_schedule",
    ],
    Intent.GIT_PUSH: [
        "run_shell",      # git add / commit / push
        "run_script",
    ],
    Intent.GENERAL: [],  # 空 = 全量工具
}


# ── 任务专用 Prompt（带具体示例，比规则描述更有效）────────────────────────────
PROMPT_HINTS: dict[Intent, str] = {

    Intent.BLOG_WRITE: """
## 当前任务：发布 Hugo 博客文章
**唯一正确路径：调用 create_blog_post，通过 GitHub API 写入文件，绝对不要用 run_shell 写本地文件。**

示例调用：
create_blog_post(
  repo="owner/blog-repo",
  title="Python 异步编程实战",
  content="## 前言\\n\\n...",
  tags=["python", "async"],
  categories=["技术"],
  draft=false
)
调用成功后，函数会自动触发 deploy.yml，无需额外操作。
最后用一句话告诉用户文章已发布，附上文件路径和 commit hash。
""",

    Intent.BLOG_LIST: """
## 当前任务：查看博客文章列表
调用 list_blog_posts(repo="...") 获取列表，用清单格式回复文章名称和数量。
""",

    Intent.GITHUB_ACTION: """
## 当前任务：GitHub Actions 操作
- 触发部署 → trigger_workflow(repo="...", workflow_id="deploy.yml")
- 查看状态 → list_workflow_runs(repo="...", limit=5)
直接调用，结果以表格形式汇报 status 和 conclusion。
""",

    Intent.GITHUB_ISSUE: """
## 当前任务：Issues / PR 管理
- 查看 issues → list_issues(repo="...", state="open")
- 创建 issue → create_issue(repo="...", title="...", body="...")
- 查看 PR → list_prs(repo="...", state="open")
调用后简洁汇报结果，包含编号和链接。
""",

    Intent.GITHUB_CODE: """
## 当前任务：读取或更新 GitHub 仓库文件
- 读文件 → get_file(repo="...", path="README.md")
- 改文件 → update_file(repo="...", path="...", content="...", message="...")
路径是仓库内相对路径，不是 VPS 本地路径。
""",

    Intent.SHELL_RUN: """
## 当前任务：在 VPS 上执行命令
调用 run_shell(command="...") 执行，汇报 returncode 和关键输出。
多行脚本用 run_script(script="...\\n...")。
""",

    Intent.FILE_OP: """
## 当前任务：VPS 文件操作
读文件用 run_shell(command="cat /path/to/file") 或 read_file(path="...")。
写文件用 write_file(path="...", content="...")。
注意：这是 VPS 本地路径，不是 GitHub 仓库路径。
""",

    Intent.MEMORY_OP: """
## 当前任务：记忆操作
- 保存信息 → remember(key="...", value="...")
- 查询信息 → recall(key="...")
- 删除信息 → forget(key="...")
- 查看全部 → show_memory()
操作完成后简洁确认。
""",

    Intent.SCHEDULE_OP: """
## 当前任务：定时任务管理
- 创建 cron 任务 → schedule_task(name="...", prompt="...", mode="cron", schedule="0 9 * * 1-5")
- 创建间隔任务 → schedule_task(name="...", prompt="...", mode="interval", schedule="3600")
- 查看任务 → list_schedules()
cron 格式：分 时 日 月 周（如 "0 9 * * 1-5" = 工作日早9点）。
""",

    Intent.GIT_PUSH: """
## 当前任务：推送代码到 GitHub
调用 run_shell 依次执行：
1. run_shell(command="git -C /path add -A")
2. run_shell(command='git -C /path commit -m "message"')
3. run_shell(command="git -C /path push")
每步检查 returncode，失败立即停止并报告错误原因。
""",

    Intent.GENERAL: "",
}


# ── 路由函数 ──────────────────────────────────────────────────────────────────
def route(text: str) -> RouteResult:
    """
    输入用户消息，返回 RouteResult。
    调用方根据 intent 决定给模型的工具子集和 prompt hint。
    """
    text_lower = text.lower()
    best_intent    = Intent.GENERAL
    best_confidence = 0.0

    for intent, confidence, keywords, patterns in _RULES:
        # 关键词命中
        kw_hit = any(kw in text_lower for kw in keywords)
        # 正则命中
        re_hit = any(re.search(p, text_lower) for p in patterns)

        if kw_hit or re_hit:
            score = confidence
            if kw_hit and re_hit:
                score = min(1.0, confidence + 0.05)  # 双重命中略微加分
            if score > best_confidence:
                best_confidence = score
                best_intent     = intent

    tool_names   = TOOL_SUBSETS.get(best_intent, [])
    prompt_hint  = PROMPT_HINTS.get(best_intent, "")

    # 模型建议：高置信度简单任务用 flash，写作/分析用 flash
    model_hint = "flash"
    if best_intent in (Intent.BLOG_WRITE,) and len(text) > 100:
        model_hint = "pro"
    elif best_intent == Intent.GENERAL:
        model_hint = "flash"

    return RouteResult(
        intent=best_intent,
        confidence=best_confidence,
        tool_names=tool_names,
        prompt_hint=prompt_hint,
        model_hint=model_hint,
    )
