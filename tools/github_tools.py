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
import structlog

log = structlog.get_logger()


class GitHubClient:
    """GitHub REST API v3 + GraphQL 封装。"""

    BASE = "https://api.github.com"

    def __init__(self, token: str, default_owner: str = "") -> None:
        self._token = token
        self.owner  = default_owner
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(headers=self._headers, timeout=30)

    def _parse_repo(self, repo: str) -> tuple[str, str]:
        """'owner/repo' 或 'repo' → (owner, repo)"""
        if "/" in repo:
            return repo.split("/", 1)
        return self.owner, repo

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
    ) -> dict:
        """创建 Hugo 博文（自动生成 front matter，提交到指定分支）。"""
        owner, repo_name = self._parse_repo(repo)
        slug = re.sub(r"[^\w\-]", "-", title.lower())[:60].strip("-")
        now  = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S+00:00")

        front_matter = f"""---
title: "{title}"
date: {now}
draft: {str(draft).lower()}
tags: {json.dumps(tags or [], ensure_ascii=False)}
categories: {json.dumps(categories or [], ensure_ascii=False)}
---

"""
        full_content = front_matter + content
        path = f"{content_path}/{slug}.md"
        encoded = base64.b64encode(full_content.encode()).decode()

        async with self._client() as c:
            # 检查文件是否已存在（获取 sha）
            sha = None
            check = await c.get(f"{self.BASE}/repos/{owner}/{repo_name}/contents/{path}",
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

            resp = await c.put(
                f"{self.BASE}/repos/{owner}/{repo_name}/contents/{path}",
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        return {
            "action":  "update" if sha else "create",
            "path":    path,
            "sha":     data["content"]["sha"],
            "html_url": data["content"]["html_url"],
            "commit":  data["commit"]["sha"][:7],
        }

    async def list_blog_posts(self, repo: str, branch: str = "main",
                              content_path: str = "content/posts") -> list[dict]:
        """列出博客文章。"""
        owner, repo_name = self._parse_repo(repo)
        async with self._client() as c:
            resp = await c.get(
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
        async with self._client() as c:
            resp = await c.get(
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
        async with self._client() as c:
            resp = await c.post(
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

        async with self._client() as c:
            resp = await c.get(url, params=params)
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
        async with self._client() as c:
            resp = await c.get(
                f"{self.BASE}/repos/{owner}/{repo_name}/actions/runs/{run_id}/logs",
                follow_redirects=False,
            )
            return resp.headers.get("location", "")

    async def cancel_workflow_run(self, repo: str, run_id: int) -> dict:
        owner, repo_name = self._parse_repo(repo)
        async with self._client() as c:
            resp = await c.post(
                f"{self.BASE}/repos/{owner}/{repo_name}/actions/runs/{run_id}/cancel"
            )
            resp.raise_for_status()
        return {"cancelled": True, "run_id": run_id}

    # ── Issues & PR ────────────────────────────────────────────────

    async def create_issue(self, repo: str, title: str, body: str = "",
                           labels: list[str] | None = None,
                           assignees: list[str] | None = None) -> dict:
        owner, repo_name = self._parse_repo(repo)
        async with self._client() as c:
            resp = await c.post(
                f"{self.BASE}/repos/{owner}/{repo_name}/issues",
                json={"title": title, "body": body,
                      "labels": labels or [], "assignees": assignees or []},
            )
            resp.raise_for_status()
            data = resp.json()
        return {"number": data["number"], "url": data["html_url"], "state": data["state"]}

    async def list_issues(self, repo: str, state: str = "open", limit: int = 10) -> list[dict]:
        owner, repo_name = self._parse_repo(repo)
        async with self._client() as c:
            resp = await c.get(
                f"{self.BASE}/repos/{owner}/{repo_name}/issues",
                params={"state": state, "per_page": limit, "pulls": "false"},
            )
            resp.raise_for_status()
        return [
            {"number": i["number"], "title": i["title"],
             "state": i["state"], "url": i["html_url"]}
            for i in resp.json() if not i.get("pull_request")
        ]

    async def list_prs(self, repo: str, state: str = "open", limit: int = 10) -> list[dict]:
        owner, repo_name = self._parse_repo(repo)
        async with self._client() as c:
            resp = await c.get(
                f"{self.BASE}/repos/{owner}/{repo_name}/pulls",
                params={"state": state, "per_page": limit},
            )
            resp.raise_for_status()
        return [
            {"number": p["number"], "title": p["title"],
             "state": p["state"], "url": p["html_url"],
             "head": p["head"]["ref"], "base": p["base"]["ref"]}
            for p in resp.json()
        ]

    async def comment_on_issue(self, repo: str, issue_number: int, body: str) -> dict:
        owner, repo_name = self._parse_repo(repo)
        async with self._client() as c:
            resp = await c.post(
                f"{self.BASE}/repos/{owner}/{repo_name}/issues/{issue_number}/comments",
                json={"body": body},
            )
            resp.raise_for_status()
            data = resp.json()
        return {"comment_id": data["id"], "url": data["html_url"]}

    async def merge_pr(self, repo: str, pr_number: int,
                       method: str = "squash", title: str = "") -> dict:
        owner, repo_name = self._parse_repo(repo)
        payload: dict = {"merge_method": method}
        if title:
            payload["commit_title"] = title
        async with self._client() as c:
            resp = await c.put(
                f"{self.BASE}/repos/{owner}/{repo_name}/pulls/{pr_number}/merge",
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        return {"merged": data.get("merged", False), "sha": data.get("sha", "")[:7]}

    # ── 代码管理 ────────────────────────────────────────────────────

    async def get_file(self, repo: str, path: str, branch: str = "main") -> str:
        """读取仓库文件内容。"""
        owner, repo_name = self._parse_repo(repo)
        async with self._client() as c:
            resp = await c.get(
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
        async with self._client() as c:
            # 获取当前 sha
            check = await c.get(
                f"{self.BASE}/repos/{owner}/{repo_name}/contents/{path}",
                params={"ref": branch},
            )
            sha = check.json().get("sha") if check.status_code == 200 else None

            payload = {"message": message, "content": encoded, "branch": branch}
            if sha:
                payload["sha"] = sha

            resp = await c.put(
                f"{self.BASE}/repos/{owner}/{repo_name}/contents/{path}",
                json=payload,
            )
            resp.raise_for_status()
        return {"path": path, "commit": resp.json()["commit"]["sha"][:7]}

    async def list_commits(self, repo: str, branch: str = "main",
                           limit: int = 10) -> list[dict]:
        owner, repo_name = self._parse_repo(repo)
        async with self._client() as c:
            resp = await c.get(
                f"{self.BASE}/repos/{owner}/{repo_name}/commits",
                params={"sha": branch, "per_page": limit},
            )
            resp.raise_for_status()
        return [
            {
                "sha":     c_["sha"][:7],
                "message": c_["commit"]["message"].split("\n")[0],
                "author":  c_["commit"]["author"]["name"],
                "date":    c_["commit"]["author"]["date"],
            }
            for c_ in resp.json()
        ]

    async def create_branch(self, repo: str, branch: str,
                             from_branch: str = "main") -> dict:
        owner, repo_name = self._parse_repo(repo)
        async with self._client() as c:
            # 获取源分支 SHA
            ref = await c.get(
                f"{self.BASE}/repos/{owner}/{repo_name}/git/ref/heads/{from_branch}"
            )
            ref.raise_for_status()
            sha = ref.json()["object"]["sha"]

            resp = await c.post(
                f"{self.BASE}/repos/{owner}/{repo_name}/git/refs",
                json={"ref": f"refs/heads/{branch}", "sha": sha},
            )
            resp.raise_for_status()
        return {"branch": branch, "sha": sha[:7]}

    async def get_repo_info(self, repo: str) -> dict:
        owner, repo_name = self._parse_repo(repo)
        async with self._client() as c:
            resp = await c.get(f"{self.BASE}/repos/{owner}/{repo_name}")
            resp.raise_for_status()
            data = resp.json()
        return {
            "name":        data["name"],
            "description": data.get("description", ""),
            "stars":       data["stargazers_count"],
            "forks":       data["forks_count"],
            "open_issues": data["open_issues_count"],
            "default_branch": data["default_branch"],
            "updated_at":  data["updated_at"],
        }

    async def search_code(self, repo: str, query: str) -> list[dict]:
        owner, repo_name = self._parse_repo(repo)
        async with self._client() as c:
            resp = await c.get(
                f"{self.BASE}/search/code",
                params={"q": f"{query} repo:{owner}/{repo_name}", "per_page": 5},
            )
            resp.raise_for_status()
        return [
            {"path": i["path"], "url": i["html_url"]}
            for i in resp.json().get("items", [])
        ]


# ── Tool Schema（供 ModelRouter 工具调用注册）─────────────────────────────────
GITHUB_TOOL_SCHEMAS = [
    {
        "name": "create_blog_post",
        "description": "创建或更新 Hugo 博客文章，自动生成 front matter 并提交到 GitHub。",
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
        "description": "列出 Hugo 博客的所有文章。",
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
        "description": "手动触发 GitHub Actions workflow（workflow_dispatch）。",
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
        "description": "查看 GitHub Actions 运行历史和状态。",
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
        "name": "list_issues",
        "description": "列出 GitHub Issues。",
        "parameters": {
            "type": "object",
            "properties": {
                "repo":  {"type": "string"},
                "state": {"type": "string", "enum": ["open", "closed", "all"]},
                "limit": {"type": "integer"},
            },
            "required": ["repo"],
        },
    },
    {
        "name": "create_issue",
        "description": "创建 GitHub Issue。",
        "parameters": {
            "type": "object",
            "properties": {
                "repo":   {"type": "string"},
                "title":  {"type": "string"},
                "body":   {"type": "string"},
                "labels": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["repo", "title"],
        },
    },
    {
        "name": "list_prs",
        "description": "列出 Pull Requests。",
        "parameters": {
            "type": "object",
            "properties": {
                "repo":  {"type": "string"},
                "state": {"type": "string", "enum": ["open", "closed", "all"]},
            },
            "required": ["repo"],
        },
    },
    {
        "name": "get_file",
        "description": "读取 GitHub 仓库中的文件内容。",
        "parameters": {
            "type": "object",
            "properties": {
                "repo":   {"type": "string"},
                "path":   {"type": "string", "description": "文件路径"},
                "branch": {"type": "string"},
            },
            "required": ["repo", "path"],
        },
    },
    {
        "name": "update_file",
        "description": "更新 GitHub 仓库中的文件内容并提交。",
        "parameters": {
            "type": "object",
            "properties": {
                "repo":    {"type": "string"},
                "path":    {"type": "string"},
                "content": {"type": "string"},
                "message": {"type": "string", "description": "commit message"},
            },
            "required": ["repo", "path", "content", "message"],
        },
    },
    {
        "name": "list_commits",
        "description": "查看提交历史。",
        "parameters": {
            "type": "object",
            "properties": {
                "repo":   {"type": "string"},
                "branch": {"type": "string"},
                "limit":  {"type": "integer"},
            },
            "required": ["repo"],
        },
    },
    {
        "name": "get_repo_info",
        "description": "获取 GitHub 仓库基本信息（stars、issues 数等）。",
        "parameters": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
            },
            "required": ["repo"],
        },
    },
]
