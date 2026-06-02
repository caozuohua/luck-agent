"""
tools/github_tools.py — GitHub 全套工具
Hugo 博客运营 + GitHub Actions + Issues/PR + 代码管理
所有函数均为 async，供 TaskQueue 和 ModelRouter 调用。
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from datetime import datetime
from typing import Any

import httpx

from core.log import get_logger

log = get_logger()


class GitHubClient:
    """GitHub REST API v3 封装，含连接池复用、429 重试、速率限制处理。"""

    BASE    = "https://api.github.com"
    # 连接池：整个进程共享一个 AsyncClient，复用 TCP 连接
    _shared_client: httpx.AsyncClient | None = None

    def __init__(self, token: str, default_owner: str = "") -> None:
        self._token = token
        self.owner  = default_owner
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    @property
    def _client(self) -> httpx.AsyncClient:
        """返回全局共享连接池（首次调用时创建）。"""
        if GitHubClient._shared_client is None or GitHubClient._shared_client.is_closed:
            GitHubClient._shared_client = httpx.AsyncClient(
                headers=self._headers,
                timeout=httpx.Timeout(connect=5, read=30, write=10, pool=5),
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
                http2=False,
            )
        return GitHubClient._shared_client

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """
        带重试的 HTTP 请求：
          - 429 Too Many Requests → 按 Retry-After 等待后重试
          - 5xx Server Error     → 指数退避重试，最多 3 次
          - 401/403              → 直接抛出，不重试
        """
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                resp = await self._client.request(method, url, **kwargs)
            except (httpx.ConnectError, httpx.ReadTimeout) as e:
                if attempt == max_attempts - 1:
                    raise
                wait = 2 ** attempt
                log.warning("github_network_retry", attempt=attempt+1,
                            error=str(e), wait=wait)
                await asyncio.sleep(wait)
                continue

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", "60"))
                log.warning("github_rate_limited", retry_after=retry_after)
                if attempt < max_attempts - 1:
                    await asyncio.sleep(min(retry_after, 120))
                    continue
            resp.raise_for_status()

            if resp.status_code >= 500 and attempt < max_attempts - 1:
                wait = 2 ** attempt
                log.warning("github_server_error", status=resp.status_code,
                            attempt=attempt+1, wait=wait)
                await asyncio.sleep(wait)
                continue

            # 检查次级速率限制（X-RateLimit-Remaining）
            remaining = resp.headers.get("X-RateLimit-Remaining", "")
            if remaining and int(remaining) < 10:
                reset_ts = int(resp.headers.get("X-RateLimit-Reset", "0"))
                wait = max(0, reset_ts - int(time.time()))
                log.warning("github_rate_near_limit",
                            remaining=remaining, reset_in=wait)

            return resp

        raise RuntimeError(f"GitHub API 请求失败（{max_attempts} 次重试后）：{url}")

    def _parse_repo(self, repo: str) -> tuple[str, str]:
        """'owner/repo' 或 'repo' → (owner, repo)"""
        if "/" in repo:
            return repo.split("/", 1)
        return self.owner, repo

    @staticmethod
    def explain_error(error: str) -> str:
        text = (error or "").lower()
        if "404" in text or "not found" in text:
            return "检查 repo、branch、path 是否正确；仓库路径必须是 GitHub 仓库内相对路径。"
        if "401" in text or "403" in text:
            return "检查 GitHub Token 权限，至少需要 Contents 和 Actions 权限。"
        if "422" in text:
            return "请求参数格式可能不对，通常是路径、分支、文件已存在或内容格式有问题。"
        if "workflow" in text or "dispatch" in text:
            return "确认 workflow 文件名和分支名正确，且仓库里存在 workflow_dispatch。"
        if "rate limit" in text or "429" in text:
            return "GitHub 触发了速率限制，稍后重试。"
        return "先检查仓库路径、权限和提交参数。"

    # ── 博客文章管理 ──────────────────────────────────────────────

    async def create_blog_post(
        self,
        repo: str,
        title: str,
        content: str,
        tags: list[str] | None = None,
        categories: list[str] | None = None,
        draft: bool = False,
        branch: str = "main",
        content_path: str = "content/posts",
        shell=None,
    ) -> dict:
        """创建 Hugo 博文。

        优先走 VPS 本地写文件 + git push（需传入 shell）。
        若仓库中已存在同 slug 文章，则按更新处理；否则创建新文章。
        失败时 fallback 到 GitHub Contents API。
        """
        owner, repo_name = self._parse_repo(repo)

        # slug：取英文部分（如有），否则全小写+连字符
        slug = self._make_slug(title)[:60].strip("-")
        now  = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S+00:00")
        date = datetime.utcnow().strftime("%Y-%m-%d")

        front_matter = f"""---
title: "{title}"
date: {now}
draft: {str(draft).lower()}
tags: {json.dumps(tags or [], ensure_ascii=False)}
categories: {json.dumps(categories or [], ensure_ascii=False)}

"""
        full_content = front_matter + content
        # 目录型 bundle：content/posts/YYYY-MM-DD-slug/index.md
        file_path = f"{content_path}/{date}-{slug}/index.md"

        existing_path = await self._find_existing_blog_post(repo, branch, content_path, slug)
        existed_before = bool(existing_path)
        if existing_path and existing_path != file_path:
            file_path = existing_path

        if shell:
            # VPS 本地写文件 + git push
            result = await self._create_post_vps(
                repo, owner, repo_name, branch, file_path,
                full_content, title, shell, existed_before=existed_before,
            )
            if not result.get("error"):
                deploy = await self._maybe_trigger_deploy(repo, shell)
                result["deploy_triggered"] = bool(deploy.get("triggered"))
                if deploy.get("error"):
                    result["deploy_error"] = deploy["error"]
                result["content_path"] = file_path
            return result

        # GitHub Contents API（fallback）
        result = await self._create_post_api(
            owner, repo_name, branch, file_path, full_content, title, existed_before,
        )
        if not result.get("error"):
            result["deploy_triggered"] = False
            result["content_path"] = file_path
        return result

    async def _find_existing_blog_post(self, repo: str, branch: str, content_path: str, slug: str) -> str:
        """在仓库里查找同 slug 的现有文章，避免重复新建。"""
        try:
            posts = await self.list_blog_posts(repo, branch=branch, content_path=content_path)
        except Exception:
            return ""

        # 匹配目录型 bundle：content/posts/YYYY-MM-DD-slug/index.md
        for post in posts:
            path = post.get("path", "")
            # 路径格式：content/posts/YYYY-MM-DD-slug/index.md
            # 去掉前缀 content/posts/ 和后缀 /index.md，取中间部分
            core = path
            if core.startswith(f"{content_path}/"):
                core = core[len(content_path) + 1:]
            if core.endswith("/index.md"):
                core = core[:-len("/index.md")]
            # core = YYYY-MM-DD-slug，去掉日期前缀得 slug
            dir_slug = core[11:] if len(core) > 11 and core[4] == "-" and core[7] == "-" else ""
            if dir_slug == slug:
                return path
        return ""

    @staticmethod
    def _make_slug(title: str) -> str:
        """从标题生成 URL-safe slug（小写英文+连字符）。"""
        import unicodedata
        # 尝试提取英文单词
        ascii_parts = re.findall(r"[a-zA-Z0-9]+", title)
        if ascii_parts:
            return "-".join(ascii_parts).lower()
        # 全中文：取每个字的首字母 + 哈希
        normalized = unicodedata.normalize("NFKD", title)
        slug = re.sub(r"[^\w\-]", "-", normalized.lower())
        slug = re.sub(r"-+", "-", slug).strip("-")
        if not slug or slug == "-":
            # 终极 fallback：用哈希
            import hashlib
            slug = "post-" + hashlib.md5(title.encode()).hexdigest()[:8]
        return slug

    async def _create_post_vps(
        self, repo, owner, repo_name, branch, file_path, full_content, title, shell, existed_before: bool = False,
    ) -> dict:
        """在 VPS 本地写文件 + git push。"""
        from config import cfg
        blog_dir = getattr(cfg, "BLOG_LOCAL_PATH", "") or f"/tmp/{owner}-{repo_name}"

        # 写文件（base64 编码避免 shell 转义问题）
        parent_dir = file_path.rsplit("/", 1)[0] if "/" in file_path else ""
        if parent_dir:
            await shell.run(f"mkdir -p '{blog_dir}/{parent_dir}'")
        # base64 编码内容避免 shell 转义问题
        import base64 as _b64
        encoded = _b64.b64encode(full_content.encode()).decode()
        result = await shell.run(
            f"echo '{encoded}' | base64 -d > '{blog_dir}/{file_path}'"
        )
        if result.get("returncode", -1) != 0:
            return {"error": f"写文件失败: {result.get('stderr', '')[:200]}"}

        # git push
        commit_msg = f"Add post: {title}"
        push_cmd = f"cd '{blog_dir}' && git add -A && git commit -m '{commit_msg}' && git push origin {branch}"
        result = await shell.run(push_cmd)
        if result.get("returncode", -1) != 0:
            stderr = result.get("stderr", "")[:200]
            # 如果没有变化也当作成功
            if "nothing to commit" not in stderr.lower():
                return {"error": f"git push 失败: {stderr}"}

        commit_hash = ""
        if result.get("stdout"):
            import re as _re
            m = _re.search(r"([a-f0-9]{7,})\s", result["stdout"])
            if m:
                commit_hash = m.group(1)

        return {
            "action":   "update" if existed_before else "create",
            "path":     file_path,
            "content_path": file_path,
            "html_url": f"https://github.com/{owner}/{repo_name}/blob/{branch}/{file_path}",
            "commit":   commit_hash or "pushed",
            "deploy_triggered": False,
        }

    async def _create_post_api(
        self, owner, repo_name, branch, file_path, full_content, title, existed_before: bool = False,
    ) -> dict:
        """通过 GitHub Contents API 写入文件（fallback）。"""
        import base64
        encoded = base64.b64encode(full_content.encode()).decode()

        # 检查文件是否已获取 sha
        sha = None
        check = await self._request("GET",
                    f"{self.BASE}/repos/{owner}/{repo_name}/contents/{file_path}",
                    params={"ref": branch})
        if check.status_code == 200:
            sha = check.json().get("sha")

        payload = {
            "message": f"{'Update' if sha else 'Add'} post: {title}",
            "content": encoded,
            "branch":  branch,
        }
        if sha:
            payload["sha"] = sha

        resp = await self._request("PUT",
            f"{self.BASE}/repos/{owner}/{repo_name}/contents/{file_path}",
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()

        return {
            "action":   "update" if sha or existed_before else "create",
            "path":     file_path,
            "content_path": file_path,
            "sha":      data["content"]["sha"],
            "html_url": data["content"]["html_url"],
            "commit":   data["commit"]["sha"][:7],
        }

    async def _maybe_trigger_deploy(self, repo: str, shell) -> dict:
        """尝试触发部署工作流；失败不阻断主流程。"""
        try:
            result = await self.trigger_workflow(repo, "deploy.yml")
            return {"triggered": True, "result": result}
        except Exception as e:
            log.warning("blog_deploy_failed", repo=repo, error=str(e)[:200])
            return {"triggered": False, "error": str(e)}

    async def list_blog_posts(self, repo: str, branch: str = "main",
                              content_path: str = "content/posts") -> list[dict]:
        """列出博客文章。"""
        owner, repo_name = self._parse_repo(repo)
        resp = await self._request("GET",
            f"{self.BASE}/repos/{owner}/{repo_name}/contents/{content_path}",
            params={"ref": branch},
        )
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        files = [
                {
                    "name":     f["name"],
                    "path":     f["path"],
                    "size":     f["size"],
                    "html_url": f["html_url"],
                }
                for f in resp.json()
                if f["type"] == "file" and f["name"].endswith(".md")
            ]
        return files

    async def get_blog_post(self, repo: str, path: str, branch: str = "main") -> dict:
        """读取博文内容（解码 base64）。"""
        owner, repo_name = self._parse_repo(repo)
        resp = await self._request("GET",
                f"{self.BASE}/repos/{owner}/{repo_name}/contents/{path}",
                params={"ref": branch},
            )
        resp.raise_for_status()
        data = resp.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        return {"path": path, "sha": data["sha"], "content": content}

    # ── GitHub Actions ─────────────────────────────────────────────

    async def trigger_workflow(
        self,
        repo: str,
        workflow_id: str,
        ref: str = "main",
        inputs: dict | None = None,
    ) -> dict:
        """手动触发 workflow_dispatch。"""
        owner, repo_name = self._parse_repo(repo)
        resp = await self._request("POST",
                f"{self.BASE}/repos/{owner}/{repo_name}/actions/workflows/{workflow_id}/dispatches",
                json={"ref": ref, "inputs": inputs or {}},
            )
        resp.raise_for_status()
        return {"triggered": True, "workflow": workflow_id, "ref": ref}

    async def list_workflow_runs(self, repo: str, workflow_id: str = "",
                                 limit: int = 10) -> list[dict]:
        """列出 workflow 运行记录。"""
        owner, repo_name = self._parse_repo(repo)
        url = f"{self.BASE}/repos/{owner}/{repo_name}/actions/runs"
        params: dict = {"per_page": limit}
        if workflow_id:
            params["workflow_id"] = workflow_id

        resp = await self._request("GET", url, params=params)
        resp.raise_for_status()
        runs = resp.json().get("workflow_runs", [])

        return [
            {
                "id":          r["id"],
                "name":        r["name"],
                "status":      r["status"],
                "conclusion":  r.get("conclusion"),
                "branch":      r["head_branch"],
                "created_at":  r["created_at"],
                "html_url":    r["html_url"],
            }
            for r in runs
        ]

    async def get_workflow_run_logs_url(self, repo: str, run_id: int) -> str:
        """获取 workflow run 日志下载 URL。"""
        owner, repo_name = self._parse_repo(repo)
        resp = await self._request("GET",
            f"{self.BASE}/repos/{owner}/{repo_name}/actions/runs/{run_id}/logs",
            follow_redirects=False,
        )
        return resp.headers.get("location", "")

    async def cancel_workflow_run(self, repo: str, run_id: int) -> dict:
        owner, repo_name = self._parse_repo(repo)
        resp = await self._request("POST",
                f"{self.BASE}/repos/{owner}/{repo_name}/actions/runs/{run_id}/cancel"
            )
        resp.raise_for_status()
        return {"cancelled": True, "run_id": run_id}

    # ── Issues & PR ────────────────────────────────────────────────

    async def create_issue(self, repo: str, title: str, body: str = "",
                           labels: list[str] | None = None,
                           assignees: list[str] | None = None) -> dict:
        owner, repo_name = self._parse_repo(repo)
        resp = await self._request("POST",
                f"{self.BASE}/repos/{owner}/{repo_name}/issues",
                json={"title": title, "body": body,
                      "labels": labels or [], "assignees": assignees or []},
            )
        resp.raise_for_status()
        data = resp.json()
        return {"number": data["number"], "url": data["html_url"], "state": data["state"]}

    async def list_items(self, repo: str, type: str = "issues",
                         state: str = "open", limit: int = 10) -> list[dict]:
        """列出 Issues 或 PRs（type='issues' 或 'prs'）。"""
        owner, repo_name = self._parse_repo(repo)
        if type == "prs":
            resp = await self._request("GET",
                    f"{self.BASE}/repos/{owner}/{repo_name}/pulls",
                    params={"state": state, "per_page": limit},
                )
        else:
            resp = await self._request("GET",
                    f"{self.BASE}/repos/{owner}/{repo_name}/issues",
                    params={"state": state, "per_page": limit, "pulls": "false"},
                )
        resp.raise_for_status()
        return [
            {"number": i["number"], "title": i["title"],
             "state": i["state"], "url": i["html_url"]}
            for i in resp.json() if type != "issues" or not i.get("pull_request")
        ]

    # ── 代码管理 ────────────────────────────────────────────────────

    async def get_file(self, repo: str, path: str, branch: str = "main") -> str:
        """读取仓库文件内容。"""
        owner, repo_name = self._parse_repo(repo)
        resp = await self._request("GET",
                f"{self.BASE}/repos/{owner}/{repo_name}/contents/{path}",
                params={"ref": branch},
            )
        resp.raise_for_status()
        data = resp.json()
        return base64.b64decode(data["content"]).decode("utf-8")

    async def update_file(self, repo: str, path: str, content: str,
                          message: str, branch: str = "main") -> dict:
        """更新文件（自动获取 sha）。"""
        owner, repo_name = self._parse_repo(repo)
        encoded = base64.b64encode(content.encode()).decode()
        # 获取当前 sha
        check = await self._request("GET",
            f"{self.BASE}/repos/{owner}/{repo_name}/contents/{path}",
            params={"ref": branch},
        )
        sha = check.json().get("sha") if check.status_code == 200 else None

        payload = {"message": message, "content": encoded, "branch": branch}
        if sha:
            payload["sha"] = sha

        resp = await self._request("PUT",
            f"{self.BASE}/repos/{owner}/{repo_name}/contents/{path}",
            json=payload,
        )
        resp.raise_for_status()
        return {"path": path, "commit": resp.json()["commit"]["sha"][:7]}


# ── Tool Schema（供 ModelRouter 工具调用注册）─────────────────────────────────
GITHUB_TOOL_SCHEMAS = [
    {
        "name": "create_blog_post",
        "description": "创建或更新 Hugo 博客文章。优先用于 GitHub 仓库内的 Hugo 内容写入：先生成 front matter，再提交到仓库；如果用户给的是 VPS 本地路径，不要用这个工具，改用 run_shell/write_file。",
        "parameters": {
            "type": "object",
            "properties": {
                "repo":       {"type": "string", "description": "仓库名或 owner/repo"},
                "title":      {"type": "string", "description": "文章标题"},
                "content":    {"type": "string", "description": "文章正文（Markdown）"},
                "tags":       {"type": "array", "items": {"type": "string"}, "description": "标签列表"},
                "categories": {"type": "array", "items": {"type": "string"}, "description": "分类列表"},
                "draft":      {"type": "boolean", "description": "是否为草稿"},
            },
            "required": ["repo", "title", "content"],
        },
    },
    {
        "name": "list_blog_posts",
        "description": "列出 Hugo 博客的文章清单。返回仓库内 Markdown 文章，不是 VPS 本地文件列表。",
        "parameters": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "仓库名"},
            },
            "required": ["repo"],
        },
    },
    {
        "name": "trigger_workflow",
        "description": "手动触发 GitHub Actions workflow（workflow_dispatch）。通常用于部署、发布、CI 重跑。",
        "parameters": {
            "type": "object",
            "properties": {
                "repo":        {"type": "string", "description": "仓库名"},
                "workflow_id": {"type": "string", "description": "workflow 文件名，如 deploy.yml"},
                "ref":         {"type": "string", "description": "分支或 tag，默认 main"},
                "inputs":      {"type": "object", "description": "workflow inputs 参数"},
            },
            "required": ["repo", "workflow_id"],
        },
    },
    {
        "name": "list_workflow_runs",
        "description": "查看 GitHub Actions 运行历史和状态，用于确认部署/CI 是否成功。",
        "parameters": {
            "type": "object",
            "properties": {
                "repo":        {"type": "string"},
                "workflow_id": {"type": "string", "description": "可选，过滤特定 workflow"},
                "limit":       {"type": "integer", "description": "返回数量，默认 10"},
            },
            "required": ["repo"],
        },
    },
    {
        "name": "create_issue",
        "description": "在 GitHub 仓库创建 Issue。用于记录缺陷、需求、排障结论，body 支持 Markdown。",
        "parameters": {
            "type": "object",
            "properties": {
                "repo":   {"type": "string", "description": "仓库名或 owner/repo"},
                "title":  {"type": "string", "description": "Issue 标题"},
                "body":   {"type": "string", "description": "Issue 正文（Markdown）"},
                "labels": {"type": "array", "items": {"type": "string"}, "description": "标签列表"},
            },
            "required": ["repo", "title"],
        },
    },
    {
        "name": "list_items",
        "description": "列出 GitHub Issues 或 Pull Requests。type 参数只能是 issues 或 prs。",
        "parameters": {
            "type": "object",
            "properties": {
                "repo":  {"type": "string", "description": "仓库名或 owner/repo"},
                "type":  {"type": "string", "enum": ["issues", "prs"], "description": "issues 或 prs"},
                "state": {"type": "string", "enum": ["open", "closed", "all"], "description": "筛选状态，默认 open"},
                "limit": {"type": "integer", "description": "返回数量，默认 10"},
            },
            "required": ["repo"],
        },
    },
    {
        "name": "get_file",
        "description": "读取 GitHub 仓库中某个文件的内容。path 必须是仓库相对路径，不是 VPS 本地路径。",
        "parameters": {
            "type": "object",
            "properties": {
                "repo":   {"type": "string", "description": "仓库名或 owner/repo"},
                "path":   {"type": "string", "description": "文件路径，如 README.md"},
                "branch": {"type": "string", "description": "分支名，默认 main"},
            },
            "required": ["repo", "path"],
        },
    },
    {
        "name": "update_file",
        "description": "更新 GitHub 仓库中的文件并提交。会自动获取 sha，文件不存在时新建。",
        "parameters": {
            "type": "object",
            "properties": {
                "repo":    {"type": "string", "description": "仓库名或 owner/repo"},
                "path":    {"type": "string", "description": "文件路径"},
                "content": {"type": "string", "description": "文件新内容"},
                "message": {"type": "string", "description": "commit message"},
            },
            "required": ["repo", "path", "content", "message"],
        },
    },
    {
        "name": "get_blog_post",
        "description": "读取 Hugo 博客某篇博文的完整内容（自动解码 base64）。用于查看已有文章，不用于 VPS 本地文件。",
        "parameters": {
            "type": "object",
            "properties": {
                "repo":   {"type": "string", "description": "仓库名或 owner/repo"},
                "path":   {"type": "string", "description": "文章路径，如 content/posts/my-post.md"},
                "branch": {"type": "string", "description": "分支名，默认 main"},
            },
            "required": ["repo", "path"],
        },
    },
]
