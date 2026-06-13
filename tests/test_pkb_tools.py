from __future__ import annotations

import asyncio
import json
import os
import unittest
from unittest.mock import patch

import httpx

from tools.pkb_tools import PkbClient, get_pkb_client


class PkbClientTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self, handler, **kwargs) -> PkbClient:
        return PkbClient(
            base_url="https://pkb.example/",
            api_secret="secret-value",
            transport=httpx.MockTransport(handler),
            **kwargs,
        )

    async def test_save_uses_stable_route_headers_body_and_preserves_fields(self) -> None:
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["secret"] = request.headers.get("x-api-secret")
            seen["content_type"] = request.headers.get("content-type")
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "id": "note-1",
                    "type": "fact",
                    "topics": ["Python"],
                    "created_at": "2026-06-13T00:00:00Z",
                    "url": "https://pkb.example/notes/note-1",
                    "idempotent": False,
                },
            )

        result = await self.make_client(handler, timeout_ms=1234).save(
            "Async tasks need supervision", topics=["Python"]
        )

        self.assertEqual(seen["url"], "https://pkb.example/api/pkb")
        self.assertEqual(seen["secret"], "secret-value")
        self.assertEqual(seen["content_type"], "application/json")
        self.assertEqual(
            seen["body"],
            {
                "content": "Async tasks need supervision",
                "source": "luck-agent",
                "type": "fact",
                "topics": ["Python"],
            },
        )
        self.assertEqual(result["created_at"], "2026-06-13T00:00:00Z")
        self.assertFalse(result["idempotent"])

    async def test_health_uses_exact_route_without_authentication(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(str(request.url), "https://pkb.example/api/pkb/health")
            self.assertIsNone(request.headers.get("x-api-secret"))
            self.assertIsNone(request.headers.get("content-type"))
            return httpx.Response(200, json={"ok": True})

        self.assertEqual((await self.make_client(handler).health())["status"], "ok")

    async def test_search_omits_source_unless_explicitly_passed(self) -> None:
        bodies = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(str(request.url), "https://pkb.example/api/pkb/search")
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "results": []})

        client = self.make_client(handler)
        await client.search("Python reliability", limit=5)
        await client.search("Python reliability", limit=3, source="luck-agent")

        self.assertEqual(
            bodies[0],
            {"query": "Python reliability", "limit": 5, "action": "search"},
        )
        self.assertEqual(bodies[1]["source"], "luck-agent")

    async def test_all_lifecycle_operations_use_exact_routes_and_parameters(self) -> None:
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(
                (request.method, str(request.url), json.loads(request.content) if request.content else None)
            )
            return httpx.Response(200, json={"ok": True, "id": "note 1"})

        client = self.make_client(handler)
        await client.get("note 1")
        await client.list(
            limit=101,
            offset=2,
            note_type="fact",
            topics=["Python", "AI"],
            from_="2026-01-01",
            to="2026-06-13",
            include_deleted=True,
        )
        await client.update("note 1", summary="Updated")
        await client.delete("note 1")
        await client.restore("note 1")

        self.assertEqual(seen[0][:2], ("GET", "https://pkb.example/api/pkb/note%201"))
        self.assertIn("limit=100", seen[1][1])
        self.assertIn("offset=2", seen[1][1])
        self.assertIn("type=fact", seen[1][1])
        self.assertIn("topics=Python%2CAI", seen[1][1])
        self.assertIn("from=2026-01-01", seen[1][1])
        self.assertIn("to=2026-06-13", seen[1][1])
        self.assertIn("include_deleted=true", seen[1][1])
        self.assertEqual(seen[2], ("PATCH", "https://pkb.example/api/pkb/note%201", {"summary": "Updated"}))
        self.assertEqual(seen[3], ("DELETE", "https://pkb.example/api/pkb/note%201", None))
        self.assertNotIn("hard", seen[3][1])
        self.assertEqual(seen[4][:2], ("POST", "https://pkb.example/api/pkb/note%201/restore"))

    async def test_list_clamps_limit_at_both_bounds(self) -> None:
        limits = []

        def handler(request: httpx.Request) -> httpx.Response:
            limits.append(request.url.params["limit"])
            return httpx.Response(200, json={"ok": True, "results": []})

        client = self.make_client(handler)
        await client.list(limit=0)
        await client.list(limit=500)
        self.assertEqual(limits, ["1", "100"])

    async def test_invalid_types_and_empty_update_are_rejected(self) -> None:
        client = self.make_client(lambda request: httpx.Response(200, json={"ok": True}))
        with self.assertRaises(ValueError):
            await client.save("content", note_type="invalid")
        with self.assertRaises(ValueError):
            await client.list(note_type="invalid")
        with self.assertRaises(ValueError):
            await client.update("note-1", note_type="invalid")
        with self.assertRaises(ValueError):
            await client.update("note-1")

    async def test_nonretry_statuses_return_structured_errors_once(self) -> None:
        expected = {
            400: ("invalid_arguments", False),
            401: ("authentication_failed", False),
            404: ("not_found", False),
            409: ("duplicate", False),
        }
        for status, (code, retryable) in expected.items():
            with self.subTest(status=status):
                calls = 0

                def handler(request: httpx.Request, response_status=status) -> httpx.Response:
                    nonlocal calls
                    calls += 1
                    return httpx.Response(response_status, json={"error": "secret-value must not leak"})

                result = await self.make_client(handler).get("note-1")
                self.assertEqual(calls, 1)
                self.assertEqual(result["status"], status)
                self.assertEqual(result["code"], code)
                self.assertEqual(result["retryable"], retryable)
                self.assertNotIn("secret-value", result["error"])

    async def test_500_and_503_retry_twice_with_exponential_backoff(self) -> None:
        for status in (500, 503):
            with self.subTest(status=status):
                calls = 0

                def handler(request: httpx.Request) -> httpx.Response:
                    nonlocal calls
                    calls += 1
                    return httpx.Response(status, json={"error": "down"})

                with patch("tools.pkb_tools.asyncio.sleep", wraps=asyncio.sleep) as sleep:
                    result = await self.make_client(handler).get("note-1")

                self.assertEqual(calls, 3)
                self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.25, 0.5])
                self.assertEqual(result["code"], "unavailable")
                self.assertTrue(result["retryable"])

    async def test_timeout_network_and_invalid_json_return_structured_errors(self) -> None:
        cases = [
            (
                lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("late", request=request)),
                "unavailable",
                True,
            ),
            (
                lambda request: (_ for _ in ()).throw(httpx.ConnectError("offline", request=request)),
                "unavailable",
                True,
            ),
            (
                lambda request: httpx.Response(200, content=b"not-json"),
                "protocol_error",
                False,
            ),
        ]
        for handler, code, retryable in cases:
            with self.subTest(code=code, retryable=retryable):
                with patch("tools.pkb_tools.asyncio.sleep", return_value=None):
                    result = await self.make_client(handler).get("note-1")
                self.assertEqual(result["code"], code)
                self.assertEqual(result["retryable"], retryable)

    async def test_missing_configuration_is_structured_and_does_not_expose_secret(self) -> None:
        client = PkbClient(base_url="", api_secret="")
        result = await client.get("note-1")
        self.assertEqual(result["code"], "configuration_error")
        self.assertFalse(result["retryable"])

    def test_factory_reads_environment_and_default_timeout(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PKB_BASE_URL": "https://env.example/",
                "PKB_API_SECRET": "env-secret",
                "PKB_TIMEOUT_MS": "2345",
            },
            clear=False,
        ):
            client = get_pkb_client()
        self.assertEqual(client.base_url, "https://env.example")
        self.assertEqual(client.timeout_ms, 2345)
        self.assertNotIn("env-secret", repr(client))


if __name__ == "__main__":
    unittest.main()
