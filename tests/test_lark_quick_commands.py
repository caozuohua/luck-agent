from __future__ import annotations

from core.operation_policy import OperationPermissionPolicy
from core.targets import VpsTarget, VpsTargetRegistry
from interface.lark_approval import LarkApprovalManager
from interface.lark_commands import QuickCommandResult, QuickCommandRouter
from interface.lark_ws import LarkWebSocketInterface
from memory.proposal import MemoryProposalDetector
from tools.mem0_client import Mem0Health, Mem0SmokeResult
from tools.service_health import ServiceHealthResult
from tools.vps_status import HostStatus
from tools.vps_sysops import VpsSysopsResult


def _text(result: str | QuickCommandResult | None) -> str:
    return result.text if isinstance(result, QuickCommandResult) else str(result or "")


class FakeHealth:
    async def collect_status(self) -> dict:
        return {
            "process": {"status": "ok"},
            "sqlite": {"connected": True},
            "goals": {"done": 3, "failed": 1},
        }


class FakeVps:
    async def collect(self) -> HostStatus:
        return HostStatus(
            hostname="aws-test",
            platform="Linux test",
            uptime_seconds=3661,
            load_1m=0.12,
            memory_total_bytes=1024 * 1024 * 1024,
            memory_available_bytes=512 * 1024 * 1024,
            disk_total_bytes=10 * 1024 * 1024 * 1024,
            disk_free_bytes=8 * 1024 * 1024 * 1024,
            collected_at=0,
        )


class FakeSysops:
    async def run(self, operation: str) -> VpsSysopsResult:
        return VpsSysopsResult(operation=operation, ok=True, output=f"checked {operation}")


class FakePagedSysops(FakeSysops):
    async def run(self, operation: str) -> VpsSysopsResult:
        return VpsSysopsResult(
            operation=operation,
            ok=False,
            output="log page 1",
            error="日志部分读取受限",
            partial=True,
            truncated=True,
            output_pages=("log page 1", "log page 2"),
        )


class FakePagedResourcesSysops(FakeSysops):
    async def run(self, operation: str) -> VpsSysopsResult:
        return VpsSysopsResult(
            operation=operation,
            ok=True,
            output="resource page 1",
            truncated=True,
            output_pages=("resource page 1", "resource page 2"),
        )


class FakeRestartSysops(FakeSysops):
    def __init__(self) -> None:
        self.restart_calls: list[tuple[str, str]] = []

    async def restart_service(
        self,
        service: str,
        *,
        user_id: str = "default",
    ) -> VpsSysopsResult:
        self.restart_calls.append((service, user_id))
        return VpsSysopsResult(operation="restart", ok=True, output="active")


class FakeProbeSysops(FakeSysops):
    async def probe_service(self, service: str, *, user_id: str = "default") -> VpsSysopsResult:
        return VpsSysopsResult(
            operation=f"service:{service}",
            ok=True,
            output='{"name":"gcp-hermeslite","version":"0.3.0"}',
        )


class FakeNewApi:
    async def health(self) -> ServiceHealthResult:
        return ServiceHealthResult("new-api", True, 7, "models reachable")


class FakeMem0:
    def __init__(self) -> None:
        self.add_calls: list[tuple[str, dict]] = []
        self.delete_calls: list[str] = []
        self.search_calls: list[str] = []
        self.list_calls: list[str] = []

    def scope_label(self, actor_id: str = "") -> str:
        return f"mode=configured · user=personal · agent=luck-agent"

    async def health(self) -> Mem0Health:
        return Mem0Health(ok=True, latency_ms=4, detail="test")

    async def smoke(self, *, actor_id: str = "") -> Mem0SmokeResult:
        return Mem0SmokeResult(
            ok=True,
            marker="test-marker",
            added=1,
            found=1,
            deleted=1,
            cleanup_confirmed=True,
        )

    async def search(self, query: str, *, actor_id: str = "") -> list[dict]:
        self.search_calls.append(actor_id)
        return [{"id": "memory-1", "memory": f"remember {query}", "score": 0.9}]

    async def list_memories(self, *, limit: int = 10, actor_id: str = "") -> list[dict]:
        self.list_calls.append(actor_id)
        return [{"id": "memory-1", "memory": "user prefers concise answers"}][:limit]

    async def add(self, text: str, **kwargs) -> dict:
        self.add_calls.append((text, kwargs))
        return {"memories": [{"id": "memory-new", "memory": text}]}

    async def delete(self, memory_id: str, *, actor_id: str = "") -> None:
        self.delete_calls.append(memory_id)


class FailingMem0(FakeMem0):
    async def add(self, text: str, **kwargs) -> dict:
        raise RuntimeError("Mem0 unavailable")

    async def delete(self, memory_id: str, *, actor_id: str = "") -> None:
        raise RuntimeError("Mem0 unavailable")

    async def list_memories(self, *, limit: int = 10, actor_id: str = "") -> list[dict]:
        raise RuntimeError("Mem0 unavailable")


class FakeAgent:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run_turn(self, text: str, *, user_id: str = "default") -> str:
        self.calls.append(text)
        return "LLM response"


class FakeSender:
    def __init__(self) -> None:
        self.cards: list[dict] = []

    async def send_card(self, chat_id: str, card: dict) -> None:
        self.cards.append(card)


async def test_quick_commands_return_without_llm() -> None:
    mem0 = FakeMem0()
    router = QuickCommandRouter(
        health=FakeHealth(),
        vps=FakeVps(),
        sysops=FakeSysops(),
        mem0=mem0,
    )

    assert await router.handle("/ping") == "🏓 pong"
    assert "SQLite：✅" in (await router.handle("/health") or "")
    assert "aws-test" in (await router.handle("/vps") or "")
    assert "checked resources" in (await router.handle("/vps resources") or "")
    assert "延迟：4 ms" in _text(await router.handle("/mem0 status"))
    assert "临时标识" in _text(await router.handle("/mem0 smoke"))
    assert "remember database" in _text(await router.handle("/mem0 search database"))
    assert "user prefers concise answers" in _text(await router.handle("/mem0 list", user_id="alice"))
    assert mem0.search_calls == ["default"]
    assert mem0.list_calls == ["alice"]
    assert await router.handle("check the server") is None


async def test_mem0_write_commands_require_approval_and_do_not_auto_write() -> None:
    mem0 = FakeMem0()
    router = QuickCommandRouter(
        health=FakeHealth(),
        vps=FakeVps(),
        mem0=mem0,
        approval_checker=lambda user, token, tool, args: token == "approved",
    )

    blocked = await router.handle("/mem0 save user prefers concise answers", user_id="alice")
    assert "必须先完成" in (blocked or "")
    assert mem0.add_calls == []

    saved = await router.handle(
        "/mem0 save user prefers concise answers",
        user_id="alice",
        approval_token="approved",
    )
    assert "已保存" in _text(saved)
    assert mem0.add_calls[0][0] == "user prefers concise answers"
    assert mem0.add_calls[0][1]["metadata"]["user_confirmed"] is True
    assert mem0.add_calls[0][1]["actor_id"] == "alice"

    await router.handle("/mem0 search concise", user_id="alice")
    assert len(mem0.add_calls) == 1


async def test_mem0_list_is_read_only_and_available_through_service_catalog() -> None:
    mem0 = FakeMem0()
    router = QuickCommandRouter(
        health=FakeHealth(),
        vps=FakeVps(),
        mem0=mem0,
    )

    result = await router.handle("/vps service mem0 list", user_id="alice")

    assert "Scope" in _text(result)
    assert "memory-1" in _text(result)
    assert mem0.add_calls == []
    assert mem0.delete_calls == []


async def test_mem0_delete_is_scoped_and_validated() -> None:
    mem0 = FakeMem0()
    router = QuickCommandRouter(
        health=FakeHealth(),
        vps=FakeVps(),
        mem0=mem0,
        approval_checker=lambda user, token, tool, args: token == "approved",
    )

    invalid = await router.handle("/mem0 delete ../../secrets", user_id="alice")
    assert "只接受单个记忆 ID" in (invalid or "")
    assert mem0.delete_calls == []

    deleted = await router.handle(
        "/mem0 delete memory-1",
        user_id="alice",
        approval_token="approved",
    )
    assert "已删除" in _text(deleted)
    assert mem0.delete_calls == ["memory-1"]


async def test_mem0_write_failure_degrades_without_raising() -> None:
    router = QuickCommandRouter(
        health=FakeHealth(),
        vps=FakeVps(),
        mem0=FailingMem0(),
        approval_checker=lambda user, token, tool, args: token == "approved",
    )

    saved = await router.handle(
        "/mem0 save temporary note",
        user_id="alice",
        approval_token="approved",
    )
    deleted = await router.handle(
        "/mem0 delete memory-1",
        user_id="alice",
        approval_token="approved",
    )
    listed = await router.handle("/mem0 list", user_id="alice")

    assert "服务不可用" in _text(saved)
    assert "服务不可用" in _text(deleted)
    assert "查询失败" in _text(listed)


async def test_vps_logs_returns_paged_card_and_binds_session_to_user() -> None:
    router = QuickCommandRouter(
        health=FakeHealth(),
        vps=FakeVps(),
        sysops=FakePagedSysops(),
    )

    first = await router.handle("/vps logs", user_id="alice")

    assert isinstance(first, QuickCommandResult)
    assert "第 1/2 页" in first.text
    assert first.card is not None
    button = first.card["body"]["elements"][-1]["columns"][0]["elements"][0]
    callback = button["behaviors"][0]["value"]
    assert callback["action"] == "vps_logs_page"

    second = router.render_log_page(callback["token"], 2, user_id="alice")
    assert "log page 2" in second.text
    assert "第 2/2 页" in second.text

    denied = router.render_log_page(callback["token"], 2, user_id="bob")
    assert "已过期" in denied.text


async def test_non_log_sysops_output_uses_generic_pagination() -> None:
    router = QuickCommandRouter(
        health=FakeHealth(),
        vps=FakeVps(),
        sysops=FakePagedResourcesSysops(),
    )

    first = await router.handle("/vps resources", user_id="alice")

    assert isinstance(first, QuickCommandResult)
    assert "输出第 1/2 页" in first.text
    assert first.card is not None
    button = first.card["body"]["elements"][-1]["columns"][0]["elements"][0]
    callback = button["behaviors"][0]["value"]
    assert callback["action"] == "vps_output_page"

    second = router.render_output_page(callback["token"], 2, user_id="alice")
    assert "resource page 2" in second.text
    assert "输出第 2/2 页" in second.text


async def test_service_catalog_routes_mem0_and_host_services() -> None:
    router = QuickCommandRouter(
        health=FakeHealth(),
        vps=FakeVps(),
        sysops=FakeSysops(),
        mem0=FakeMem0(),
    )

    catalog = await router.handle("/vps service list")
    assert "`mem0`" in _text(catalog)
    assert "`a2a`" in _text(catalog)
    assert "延迟：4 ms" in _text(await router.handle("/vps service mem0 status"))
    assert "checked services" in _text(await router.handle("/vps service a2a status"))


async def test_service_catalog_honors_service_allowlist() -> None:
    router = QuickCommandRouter(
        health=FakeHealth(),
        vps=FakeVps(),
        sysops=FakeSysops(),
        mem0=FakeMem0(),
        permission_policy=OperationPermissionPolicy.from_csv(services="mem0"),
    )

    catalog = await router.handle("/vps service list")
    assert "`mem0`" in _text(catalog)
    assert "`a2a`" not in _text(catalog)
    denied = await router.handle("/vps service a2a status")
    assert "无权访问服务" in (denied or "")


async def test_user_allowlist_blocks_vps_commands_but_not_help() -> None:
    router = QuickCommandRouter(
        health=FakeHealth(),
        vps=FakeVps(),
        sysops=FakeSysops(),
        permission_policy=OperationPermissionPolicy.from_csv(user_ids="ou-ops"),
    )

    assert "无权执行 VPS 运维" in (await router.handle("/vps", user_id="ou-viewer") or "")
    assert "可用快捷命令" in (await router.handle("/help", user_id="ou-viewer") or "")


async def test_service_catalog_uses_api_and_fixed_probe_backends() -> None:
    router = QuickCommandRouter(
        health=FakeHealth(),
        vps=FakeVps(),
        sysops=FakeProbeSysops(),
        mem0=FakeMem0(),
        new_api=FakeNewApi(),
    )

    new_api = await router.handle("/vps service new-api status")
    a2a = await router.handle("/vps service a2a status")
    assert "延迟：7 ms" in _text(new_api)
    assert "gcp-hermeslite" in _text(a2a)


async def test_restart_requires_one_time_approval_and_audits_execution() -> None:
    sysops = FakeRestartSysops()
    audits: list[dict] = []

    async def audit_writer(**record) -> None:
        audits.append(record)

    router = QuickCommandRouter(
        health=FakeHealth(),
        vps=FakeVps(),
        sysops=sysops,
        targets=VpsTargetRegistry.from_csv(
            "",
            default_target=VpsTarget(provider="aws", target_id="aws-01"),
        ),
        permission_policy=OperationPermissionPolicy.from_csv(
            targets="aws-01",
            services="luck-agent",
            operations="restart",
        ),
        approval_checker=lambda user, token, tool, args: token == "approved",
        audit_writer=audit_writer,
    )

    blocked = await router.handle("/vps service luck-agent restart", user_id="alice")
    assert "必须先完成" in (blocked or "")
    assert sysops.restart_calls == []

    denied = await router.handle(
        "/vps service luck-agent restart",
        user_id="alice",
        approval_token="wrong",
    )
    assert "确认码无效" in (denied or "")
    assert sysops.restart_calls == []

    executed = await router.handle(
        "/vps service luck-agent restart",
        user_id="alice",
        approval_token="approved",
    )
    assert "active" in (executed or "")
    assert sysops.restart_calls == [("luck-agent", "alice")]
    assert [item["decision"] for item in audits] == ["denied", "approved", "executed"]


async def test_lark_confirmation_passes_token_to_quick_restart() -> None:
    sysops = FakeRestartSysops()
    manager = LarkApprovalManager()
    router = QuickCommandRouter(
        health=FakeHealth(),
        vps=FakeVps(),
        sysops=sysops,
        targets=VpsTargetRegistry.from_csv(
            "",
            default_target=VpsTarget(provider="aws", target_id="aws-01"),
        ),
        approval_checker=manager.consume_grant,
    )
    sender = FakeSender()
    interface = LarkWebSocketInterface(
        agent=FakeAgent(),
        sender=sender,
        quick_commands=router,
        approval_manager=manager,
    )
    base = {"chat_id": "chat-1", "user_id": "alice"}

    assert await interface.handle_message(
        {**base, "message_id": "restart-1", "text": "/vps service luck-agent restart"}
    )
    approval_text = str(sender.cards[-1])
    token = approval_text.split("/confirm ", 1)[1].split("`", 1)[0]
    assert await interface.handle_message(
        {**base, "message_id": "restart-2", "text": f"/confirm {token}"}
    )
    assert sysops.restart_calls == [("luck-agent", "alice")]


async def test_lark_interface_short_circuits_quick_command() -> None:
    agent = FakeAgent()
    sender = FakeSender()
    interface = LarkWebSocketInterface(
        agent=agent,
        sender=sender,
        quick_commands=QuickCommandRouter(health=FakeHealth(), vps=FakeVps()),
    )

    processed = await interface.handle_message(
        {
            "message_id": "quick-1",
            "chat_id": "chat-1",
            "user_id": "user-1",
            "text": "/ping",
        }
    )

    assert processed is True
    assert agent.calls == []
    assert "pong" in str(sender.cards[0])


async def test_lark_memory_proposal_never_calls_agent_or_mem0() -> None:
    agent = FakeAgent()
    sender = FakeSender()
    interface = LarkWebSocketInterface(
        agent=agent,
        sender=sender,
        memory_proposer=MemoryProposalDetector(),
    )

    processed = await interface.handle_message(
        {
            "message_id": "memory-proposal-1",
            "chat_id": "chat-1",
            "user_id": "alice",
            "text": "请记住我喜欢简洁回答",
        }
    )

    assert processed is True
    assert agent.calls == []
    assert "/mem0 save 我喜欢简洁回答" in str(sender.cards[-1])


async def test_target_commands_return_card_and_keep_user_selection() -> None:
    default = VpsTarget(provider="aws", target_id="aws-01")
    targets = VpsTargetRegistry.from_csv(
        "gcp-01|gcp||us-west1|staging",
        default_target=default,
    )
    router = QuickCommandRouter(
        health=FakeHealth(),
        vps=FakeVps(),
        targets=targets,
    )

    result = await router.handle("/targets", user_id="alice")
    assert result is not None
    assert result.text == "🎯 当前目标：AWS / aws-01"
    assert result.card is not None
    assert result.card["body"]["elements"][1]["options"]

    selected = await router.handle("/target gcp-01", user_id="alice")
    assert selected.text == "🎯 当前目标：GCP / gcp-01 / us-west1"
    assert targets.current("alice").label == "gcp-01"
    assert targets.current("bob").label == "aws-01"


async def test_vps_command_uses_sysops_resources_for_remote_target() -> None:
    default = VpsTarget(provider="aws", target_id="aws-01")
    targets = VpsTargetRegistry.from_csv(
        "gcp-01|gcp|||personal|gcp-ts|caozuohua99|22",
        default_target=default,
    )
    targets.select("alice", "gcp-01")
    router = QuickCommandRouter(
        health=FakeHealth(),
        vps=FakeVps(),
        sysops=FakeSysops(),
        targets=targets,
    )

    result = await router.handle("/vps", user_id="alice")
    assert "checked resources" in (result or "")


async def test_quick_commands_hide_and_reject_unauthorized_targets() -> None:
    default = VpsTarget(provider="aws", target_id="aws-01")
    targets = VpsTargetRegistry.from_csv(
        "gcp-01|gcp|||personal|gcp-ts|caozuohua99|22",
        default_target=default,
    )
    policy = OperationPermissionPolicy.from_csv(targets="gcp-01")
    router = QuickCommandRouter(
        health=FakeHealth(),
        vps=FakeVps(),
        sysops=FakeSysops(),
        targets=targets,
        permission_policy=policy,
    )

    listing = await router.handle("/targets", user_id="alice")
    assert listing.card is not None
    options = listing.card["body"]["elements"][1]["options"]
    assert [item["value"] for item in options] == ["gcp-01"]
    assert "无权访问目标" in (await router.handle("/vps", user_id="alice") or "")
    denied = await router.handle("/target aws-01", user_id="alice")
    assert denied is not None
    assert "无权访问目标" in (denied.text if hasattr(denied, "text") else str(denied))
