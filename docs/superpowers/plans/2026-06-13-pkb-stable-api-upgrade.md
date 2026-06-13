# PKB Stable API Upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade luck-agent to the stable PKB API with seven model tools, preserved direct-message entry points, bounded error handling, and reversible VPS verification.

**Architecture:** Create `tools/pkb_tools.py` as the only PKB HTTP boundary and keep message parsing, model schemas, dispatch, and presentation in `handlers/message.py`. Route deterministic PKB intents through the new tool names, while `#` and `/pkb` call compatibility helpers backed by the same client.

**Tech Stack:** Python 3.10+, `httpx`, `asyncio`, `unittest`, `unittest.mock`, Lark tool schemas, systemd, Google Cloud CLI.

---

## File Structure

- Create `tools/pkb_tools.py`: configuration, validation, HTTP requests, retries, response normalization, and seven PKB operations.
- Create `tests/test_pkb_tools.py`: isolated client contract and error-policy tests.
- Modify `handlers/message.py`: note types, compatibility wrappers, model schemas, dispatch, and summaries.
- Modify `tests/test_pkb_message.py`: direct-entry compatibility, tool schema, dispatch, and idempotency tests.
- Modify `core/intent_router.py`: stable tool names and PKB lifecycle guidance.
- Modify `tests/test_intent_router.py`: route and policy assertions.
- Modify `README.md`: stable environment variables, note types, tools, and manual validation.
- Modify `deploy.sh`: ensure the new client is included by the explicit upload path.

### Task 1: Build The Stable PKB Client

**Files:**
- Create: `tools/pkb_tools.py`
- Create: `tests/test_pkb_tools.py`

- [ ] **Step 1: Write failing configuration and request-contract tests**

Add tests using `unittest.IsolatedAsyncioTestCase` and `httpx.MockTransport`:

```python
class PkbClientContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_save_uses_stable_route_headers_and_timeout(self) -> None:
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["secret"] = request.headers.get("x-api-secret")
            seen["content_type"] = request.headers.get("content-type")
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={
                "ok": True, "id": "note-1", "type": "fact",
                "topics": ["Python"], "idempotent": False,
            })

        client = PkbClient(
            base_url="https://pkb.example/",
            api_secret="secret",
            timeout_ms=1234,
            transport=httpx.MockTransport(handler),
        )
        result = await client.save("Async tasks need supervision", topics=["Python"])

        self.assertEqual(seen["url"], "https://pkb.example/api/pkb")
        self.assertEqual(seen["secret"], "secret")
        self.assertEqual(seen["content_type"], "application/json")
        self.assertEqual(seen["body"]["source"], "luck-agent")
        self.assertEqual(result["id"], "note-1")

    async def test_health_does_not_send_secret(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertIsNone(request.headers.get("x-api-secret"))
            return httpx.Response(200, json={"ok": True})

        client = PkbClient(
            "https://pkb.example", "secret",
            transport=httpx.MockTransport(handler),
        )
        self.assertEqual((await client.health())["status"], "ok")
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
python -m unittest tests.test_pkb_tools -v
```

Expected: import failure because `tools.pkb_tools` does not exist.

- [ ] **Step 3: Implement configuration and request core**

Create:

```python
VALID_PKB_TYPES = {"fact", "idea", "task", "question", "code"}

class PkbClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_secret: str | None = None,
        timeout_ms: int | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or os.getenv("PKB_BASE_URL", "")).strip().rstrip("/")
        self.api_secret = (api_secret or os.getenv("PKB_API_SECRET", "")).strip()
        self.timeout_ms = timeout_ms or _env_timeout_ms()
        self.transport = transport

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        # Validate configuration, construct headers, execute request,
        # apply retry policy, and parse an object JSON response.
```

Use `httpx.Timeout(self.timeout_ms / 1000)` and inject `transport` only for
tests. Health uses `authenticated=False`.

- [ ] **Step 4: Add failing operation and validation tests**

Cover exact contracts:

```python
await client.search("Python reliability", limit=5)
# POST /api/pkb/search
# {"query": "...", "limit": 5, "action": "search"} with no source

await client.get("note-1")
# GET /api/pkb/note-1

await client.list(limit=100, offset=2, note_type="fact",
                  topics=["Python", "AI"], include_deleted=True)
# GET /api/pkb/list?limit=100&offset=2&type=fact&topics=Python%2CAI&include_deleted=true

await client.update("note-1", summary="Updated")
# PATCH /api/pkb/note-1

await client.delete("note-1")
# DELETE /api/pkb/note-1 with no hard query parameter

await client.restore("note-1")
# POST /api/pkb/note-1/restore
```

Assert invalid types raise `ValueError`, list limits clamp to `1..100`, empty
updates raise `ValueError`, and explicit search source is included only when
passed.

- [ ] **Step 5: Run operation tests and verify RED**

Run:

```powershell
python -m unittest tests.test_pkb_tools -v
```

Expected: failures for missing operation methods and validation.

- [ ] **Step 6: Implement all seven operations**

Add async methods with these signatures:

```python
async def save(self, content: str, *, source: str = "luck-agent",
               note_type: str = "fact", topics: list[str] | None = None) -> dict:
async def search(self, query: str, *, limit: int = 5,
                 source: str | None = None) -> dict:
async def get(self, note_id: str) -> dict:
async def list(self, *, limit: int = 50, offset: int = 0,
               note_type: str | None = None, topics: list[str] | None = None,
               from_: str | None = None, to: str | None = None,
               include_deleted: bool = False) -> dict:
async def update(self, note_id: str, *, content: str | None = None,
                 note_type: str | None = None, topics: list[str] | None = None,
                 summary: str | None = None) -> dict:
async def delete(self, note_id: str) -> dict:
async def restore(self, note_id: str) -> dict:
async def health(self) -> dict:
```

Return API success objects without discarding stable fields.

- [ ] **Step 7: Add failing error-policy tests**

Test:

```python
test_400_returns_parameter_error_without_transport_retry
test_401_returns_auth_error_without_retry
test_404_returns_missing_or_deleted_error
test_409_returns_duplicate_error
test_500_and_503_retry_twice_with_exponential_backoff
test_timeout_returns_unavailable_error
test_invalid_json_returns_protocol_error
```

Patch `asyncio.sleep` to avoid real delays and assert request counts of one for
400/401/404/409 and three for 500/503.

- [ ] **Step 8: Run error tests and verify RED**

Run:

```powershell
python -m unittest tests.test_pkb_tools -v
```

Expected: status mapping and retry-count failures.

- [ ] **Step 9: Implement structured errors and retries**

Return errors in this shape:

```python
{
    "ok": False,
    "status": 503,
    "code": "unavailable",
    "error": "PKB 暂时不可用",
    "retryable": True,
}
```

Use delays `0.25` and `0.5` seconds for the two retries. Never log the secret.

- [ ] **Step 10: Verify client tests pass**

Run:

```powershell
python -m unittest tests.test_pkb_tools -v
```

Expected: all `tests.test_pkb_tools` tests pass.

- [ ] **Step 11: Commit**

```powershell
git add tools/pkb_tools.py tests/test_pkb_tools.py
git commit -m "tools: add stable PKB client"
```

### Task 2: Preserve Direct Lark Entry Points

**Files:**
- Modify: `handlers/message.py`
- Modify: `handlers/command.py`
- Modify: `agent.py`
- Modify: `tests/test_pkb_message.py`

- [ ] **Step 1: Replace legacy-helper tests with failing stable-client tests**

Delete tests for `_pkb_url`, `_pkb_health_url`, `_pkb_env`, and `_pkb_post`.
Add:

```python
def test_note_parser_accepts_all_stable_types(self) -> None:
    for note_type in ("fact", "idea", "task", "question", "code"):
        parsed = parse_note_message(f"# [{note_type}] content")
        self.assertEqual(parsed[1], note_type)

async def test_forward_to_pkb_result_uses_luck_agent_source(self) -> None:
    client = SimpleNamespace(save=AsyncMock(return_value={
        "ok": True, "id": "n1", "type": "fact",
        "topics": ["Python"], "idempotent": False,
    }))
    with patch("handlers.message.get_pkb_client", return_value=client):
        result = await forward_to_pkb_result("note", "fact", ["Python"])
    client.save.assert_awaited_once_with(
        "note", source="luck-agent", note_type="fact", topics=["Python"],
    )
    self.assertFalse(result["idempotent"])

async def test_search_pkb_omits_source_by_default(self) -> None:
    client = SimpleNamespace(search=AsyncMock(return_value={
        "ok": True, "results": [], "count": 0,
    }))
    with patch("handlers.message.get_pkb_client", return_value=client):
        await search_pkb("Python", limit=5)
    client.search.assert_awaited_once_with("Python", limit=5)
```

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```powershell
python -m unittest tests.test_pkb_message -v
```

Expected: stable type and `get_pkb_client` failures.

- [ ] **Step 3: Replace inline HTTP code with client wrappers**

In `handlers/message.py`:

- import `PkbClient`, `VALID_PKB_TYPES`, and `get_pkb_client`;
- remove legacy URL/request helpers and `httpx` import;
- set `VALID_NOTE_TYPES = VALID_PKB_TYPES`;
- make `forward_to_pkb_result`, `search_pkb`, and `check_pkb_health` delegate to
  the client;
- keep `forward_to_pkb` as a boolean compatibility wrapper;
- clamp search display limits to 10 while leaving list API limits to the client.

In `agent.py`, change the idempotent success detail:

```python
if ok and pkb_result.get("idempotent"):
    detail = "知识库中已有该内容"
elif ok:
    detail = "已保存到个人知识库"
else:
    detail = str(error_detail)
```

`handlers/command.py` continues using `search_pkb` and therefore needs no
behavioral rewrite unless imports move.

- [ ] **Step 4: Verify direct-entry tests pass**

Run:

```powershell
python -m unittest tests.test_pkb_message tests.test_pkb_cards tests.test_command_system -v
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```powershell
git add agent.py handlers/message.py handlers/command.py tests/test_pkb_message.py
git commit -m "handlers: migrate PKB entry points"
```

### Task 3: Add Seven Model Tools And Lifecycle Policy

**Files:**
- Modify: `handlers/message.py`
- Modify: `core/intent_router.py`
- Modify: `tests/test_pkb_message.py`
- Modify: `tests/test_intent_router.py`

- [ ] **Step 1: Write failing schema and dispatch tests**

Construct `AgentMessageHandler` with lightweight dependencies and assert:

```python
tool_names = {tool["name"] for tool in handler.all_tools}
self.assertTrue({
    "pkb_save", "pkb_search", "pkb_get", "pkb_list",
    "pkb_update", "pkb_delete", "pkb_restore",
}.issubset(tool_names))
self.assertNotIn("write_pkb", tool_names)
self.assertNotIn("search_pkb", tool_names)
```

Patch `handlers.message.get_pkb_client` and call `_execute_tool` for every tool.
Assert argument mapping, including omitted default search source, list filters,
update fields, and delete calling only `client.delete(note_id)`.

- [ ] **Step 2: Run schema/dispatch tests and verify RED**

Run:

```powershell
python -m unittest tests.test_pkb_message -v
```

Expected: missing stable tool schemas and dispatch branches.

- [ ] **Step 3: Add stable tool schemas and dispatch**

Define `PKB_TOOL_SCHEMAS` near the client or message handler and append it to
`self.all_tools`. Use exact enums and required fields from the design.

Dispatch:

```python
elif name == "pkb_save":
    return await client.save(...)
elif name == "pkb_search":
    return await client.search(...)
elif name == "pkb_get":
    return await client.get(...)
elif name == "pkb_list":
    return await client.list(...)
elif name == "pkb_update":
    return await client.update(...)
elif name == "pkb_delete":
    return await client.delete(...)
elif name == "pkb_restore":
    return await client.restore(...)
```

Validation errors become `{"ok": False, "code": "invalid_arguments",
"error": str(exc)}`. Tool descriptions explicitly state that delete is soft
delete and requires confirmed user intent.

- [ ] **Step 4: Add failing intent and policy tests**

Extend `tests/test_intent_router.py`:

```python
def test_pkb_write_uses_stable_save_tool(self) -> None:
    result = route("把这个结论记到知识库")
    self.assertEqual(result.tool_names, ["pkb_save"])

def test_pkb_search_exposes_read_tools(self) -> None:
    result = route("查一下以前关于 Python 的记录")
    self.assertIn("pkb_search", result.tool_names)
    self.assertIn("pkb_get", result.tool_names)

def test_pkb_delete_policy_requires_confirmation(self) -> None:
    result = route("删除知识库里那条 Python 记录")
    self.assertIn("确认", result.prompt_hint)
    self.assertIn("软删除", result.prompt_hint)
    self.assertNotIn("hard=true", result.prompt_hint)
```

Also assert guidance contains the save allowlist, credential prohibition,
proactive historical search, source omission, update localization, and restore.

- [ ] **Step 5: Run routing tests and verify RED**

Run:

```powershell
python -m unittest tests.test_intent_router -v
```

Expected: old tool names and missing lifecycle policy.

- [ ] **Step 6: Update intents, tool subsets, and prompt guidance**

Add explicit `PKB_LIST`, `PKB_UPDATE`, `PKB_DELETE`, and `PKB_RESTORE` intents
only for unambiguous phrases. Tool subsets:

```python
PKB_WRITE: ["pkb_save"]
PKB_SEARCH: ["pkb_search", "pkb_get", "pkb_list"]
PKB_LIST: ["pkb_list", "pkb_get"]
PKB_UPDATE: ["pkb_search", "pkb_get", "pkb_update"]
PKB_DELETE: ["pkb_search", "pkb_get", "pkb_delete"]
PKB_RESTORE: ["pkb_search", "pkb_list", "pkb_restore"]
```

Put update/delete/restore rules before broad PKB search rules. State that failed
PKB calls must not be described as successful reads or writes.

- [ ] **Step 7: Update result summaries**

Handle all stable names in `_summarize_tool_results`. For `pkb_save`, branch on
`idempotent`. For failed operations, show the returned error instead of a
success marker. Reuse `format_pkb_result_items` for search and list.

- [ ] **Step 8: Verify PKB behavior tests pass**

Run:

```powershell
python -m unittest tests.test_pkb_message tests.test_intent_router tests.test_pkb_cards -v
```

Expected: all selected tests pass.

- [ ] **Step 9: Commit**

```powershell
git add handlers/message.py core/intent_router.py tests/test_pkb_message.py tests/test_intent_router.py
git commit -m "agent: add PKB lifecycle tools"
```

### Task 4: Update Configuration And Deployment Documentation

**Files:**
- Modify: `README.md`
- Modify: `deploy.sh`
- Modify: `tests/test_ops_scripts.py`

- [ ] **Step 1: Write failing deployment inclusion test**

Add an assertion that `deploy.sh` includes:

```python
self.assertIn("tools/pkb_tools.py", deploy_script)
```

- [ ] **Step 2: Run deployment test and verify RED**

Run:

```powershell
python -m unittest tests.test_ops_scripts -v
```

Expected: failure because the new file is absent from the explicit upload list.

- [ ] **Step 3: Update deployment and README**

Add `tools/pkb_tools.py` to `FILES` in `deploy.sh`.

Replace legacy README variables with:

```text
PKB_BASE_URL=https://your-pkb.vercel.app
PKB_API_SECRET=replace-me
PKB_TIMEOUT_MS=10000
```

Document:

- stable types `fact|idea|task|question|code`;
- `#` and `/pkb` compatibility;
- seven model tools;
- idempotent save wording;
- soft-delete confirmation and restore;
- PKB-unavailable fallback behavior.

- [ ] **Step 4: Verify documentation and script tests**

Run:

```powershell
python -m unittest tests.test_ops_scripts -v
rg -n "VERCEL_API_URL|PKB_INGEST_URL|PKB_SEARCH_URL|PKB_HEALTH_URL" README.md handlers tools
```

Expected: tests pass and `rg` returns no legacy runtime/documentation matches.

- [ ] **Step 5: Commit**

```powershell
git add README.md deploy.sh tests/test_ops_scripts.py
git commit -m "docs: update PKB configuration"
```

### Task 5: Full Local Verification

**Files:**
- No intended modifications

- [ ] **Step 1: Run syntax compilation**

Run:

```powershell
python -m compileall agent.py config.py core handlers tools cards
```

Expected: exit code 0.

- [ ] **Step 2: Run the full test suite**

Run:

```powershell
python -m unittest discover -s tests -v
```

Expected: all tests pass with zero failures and zero errors.

- [ ] **Step 3: Inspect the final diff**

Run:

```powershell
git status --short
git diff --check HEAD
git diff HEAD -- tools/pkb_tools.py handlers/message.py core/intent_router.py agent.py README.md deploy.sh tests
```

Expected: only scoped PKB changes plus the untracked `.codegraph/`; no whitespace
errors and no secret values.

### Task 6: Deploy And Verify On VPS

**Files:**
- Remote: `/opt/luck-agent/.env`
- Remote: `/opt/luck-agent/tools/pkb_tools.py`
- Remote service: `luck-agent`

- [ ] **Step 1: Discover the configured VPS and protect the secret**

Use the existing `GCP_PROJECT`, `GCP_ZONE`, and `INSTANCE_NAME` values. Read the
local or remote existing PKB secret without echoing it. If no stable values
exist, derive `PKB_BASE_URL` from the old endpoint and reuse the existing API
secret only in-memory.

- [ ] **Step 2: Back up and update remote environment**

Over SSH, create a timestamped root-readable backup of `/opt/luck-agent/.env`.
Replace only PKB keys using a quoted remote script:

```text
PKB_BASE_URL=<normalized origin>
PKB_API_SECRET=<secret>
PKB_TIMEOUT_MS=10000
```

Remove legacy PKB URL keys and avoid printing file contents.

- [ ] **Step 3: Deploy code and restart**

Upload the changed runtime files using the repository's established deployment
path, then run:

```bash
sudo systemctl daemon-reload
sudo systemctl restart luck-agent
sudo systemctl is-active luck-agent
sudo journalctl -u luck-agent -n 100 --no-pager
```

Expected: service is `active`; logs contain no import, PKB configuration, or
startup error.

- [ ] **Step 4: Run unauthenticated health verification**

From the VPS, call `${PKB_BASE_URL}/api/pkb/health` without `x-api-secret`.

Expected: HTTP success and a valid JSON health response.

- [ ] **Step 5: Run reversible authenticated lifecycle verification**

Execute a short Python script using `/opt/luck-agent/venv/bin/python` and the
deployed `PkbClient`:

1. Save unique content tagged `luck-agent-verification`.
2. Save the same content again and assert `idempotent is True`.
3. Search without source and locate the note.
4. Get it by ID.
5. Update its summary or content.
6. List by topic and locate it.
7. Soft delete it.
8. Verify get returns the API's missing/deleted result.
9. Restore it.
10. Get it successfully.
11. Soft delete it for cleanup.

The script prints only operation names, status codes, IDs, and boolean checks;
it never prints headers, environment variables, or the secret.

- [ ] **Step 6: Run final remote service check**

Run:

```bash
sudo systemctl is-active luck-agent
sudo journalctl -u luck-agent --since "10 minutes ago" --no-pager
```

Expected: service remains active and verification caused no unhandled errors.

- [ ] **Step 7: Commit any compatibility correction**

If live verification required code changes, reproduce the mismatch with a
failing local test, implement the minimal correction, rerun Tasks 5 and 6, then:

```powershell
git add <corrected-files>
git commit -m "fix: align PKB production contract"
```

If no correction was required, do not create an empty commit.
