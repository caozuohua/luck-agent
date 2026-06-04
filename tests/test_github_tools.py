from __future__ import annotations

import unittest

from tools.github_tools import GitHubClient


class FakeResponse:
    def __init__(self, data, status_code: int = 200) -> None:
        self._data = data
        self.status_code = status_code

    def json(self):
        return self._data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class RecordingGitHubClient(GitHubClient):
    def __init__(self) -> None:
        super().__init__("token", default_owner="owner")
        self.workflow_calls: list[tuple[str, str, str]] = []
        self.requests: list[str] = []

    async def _request(self, method: str, url: str, **kwargs) -> FakeResponse:
        self.requests.append(url)
        if url.endswith("/contents/content/posts"):
            return FakeResponse([
                {
                    "type": "dir",
                    "name": "2026-06-04-my-post",
                    "path": "content/posts/2026-06-04-my-post",
                },
                {
                    "type": "file",
                    "name": "standalone.md",
                    "path": "content/posts/standalone.md",
                    "size": 123,
                    "html_url": "https://example.com/standalone",
                },
            ])
        if url.endswith("/contents/content/posts/2026-06-04-my-post"):
            return FakeResponse([
                {
                    "type": "file",
                    "name": "index.md",
                    "path": "content/posts/2026-06-04-my-post/index.md",
                    "size": 456,
                    "html_url": "https://example.com/bundle",
                },
            ])
        return FakeResponse([], status_code=404)

    async def trigger_workflow(self, repo: str, workflow_id: str, ref: str = "main", inputs=None) -> dict:
        self.workflow_calls.append((repo, workflow_id, ref))
        return {"triggered": True, "workflow": workflow_id, "ref": ref}


class GitHubToolsTests(unittest.IsolatedAsyncioTestCase):
    async def test_maybe_trigger_deploy_uses_hugo_workflow(self) -> None:
        github = RecordingGitHubClient()

        result = await github._maybe_trigger_deploy("owner/repo", shell=None)

        self.assertTrue(result["triggered"])
        self.assertEqual(github.workflow_calls, [("owner/repo", "deploy-hugo.yml", "main")])

    async def test_list_blog_posts_uses_bundle_directory_name_for_display(self) -> None:
        github = RecordingGitHubClient()

        posts = await github.list_blog_posts("owner/repo")

        self.assertEqual(posts[0]["name"], "2026-06-04-my-post")
        self.assertEqual(posts[0]["path"], "content/posts/2026-06-04-my-post/index.md")
        self.assertEqual(posts[1]["name"], "standalone.md")


if __name__ == "__main__":
    unittest.main()
