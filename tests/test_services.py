from __future__ import annotations

import pytest

from core.services import (
    SERVICE_ASSET_CATALOG,
    SERVICE_OPERATIONS,
    format_service_asset_catalog,
    get_service_assets,
    get_service_operation,
)


def test_read_only_asset_catalog_covers_all_three_targets() -> None:
    assert {asset.target_id for asset in SERVICE_ASSET_CATALOG} == {
        "gcp-free-vps-oregon",
        "az-free-vm",
        "aws-codex-vps",
    }
    rendered = format_service_asset_catalog()
    for marker in (
        "x-ui",
        "Nginx",
        "Docker Compose",
        "Mem0 PostgreSQL 每日备份",
        "暂无受控 restart/backup",
    ):
        assert marker in rendered


def test_asset_allowlist_reuses_existing_service_identity() -> None:
    assets = get_service_assets(allowed={"mem0"})

    assert assets
    assert {asset.target_id for asset in assets} == {"aws-codex-vps"}
    assert {asset.access_service_id for asset in assets} == {"mem0"}


def test_inventory_does_not_register_new_mutations() -> None:
    assert get_service_operation("x-ui", "restart") is None
    assert get_service_operation("nginx", "backup") is None
    assert get_service_operation("mem0-postgres", "restart") is None


def test_registered_mutations_have_fixed_entrypoint_rollback_and_verification() -> None:
    assert SERVICE_OPERATIONS
    for spec in SERVICE_OPERATIONS:
        assert spec.service_id
        assert spec.operation
        assert spec.entrypoint.startswith(("sudo -n ", "systemctl --user "))
        assert spec.rollback_strategy
        assert spec.verification
        assert spec.preconditions
        assert spec.idempotency


def test_unregistered_service_mutation_is_not_available() -> None:
    assert get_service_operation("mem0", "restart") is None
    assert get_service_operation("luck-agent", "upgrade") is None


def test_new_api_restart_has_a_fixed_contract() -> None:
    operation = get_service_operation("new-api", "restart")

    assert operation is not None
    assert operation.entrypoint == (
        "sudo -n systemctl restart new-api.service && systemctl is-active new-api.service"
    )
    assert "/v1/models" in operation.verification


def test_new_api_backup_is_bound_to_the_gcp_production_target() -> None:
    operation = get_service_operation("new-api", "backup")

    assert operation is not None
    assert operation.supports_target("gcp-free-vps-oregon", "gcp")
    assert not operation.supports_target("another-gcp-target", "gcp")
    assert not operation.supports_target("gcp-free-vps-oregon", "aws")
    assert "new_api_backup.sh" in operation.entrypoint
    assert "integrity_check" in operation.verification


def test_a2a_restart_has_provider_specific_fixed_contract() -> None:
    operation = get_service_operation("a2a", "restart")

    assert operation is not None
    assert operation.supports_provider("gcp")
    assert operation.supports_provider("azure")
    assert not operation.supports_provider("aws")
    assert "hermes-a2a-bridge.service" in operation.entrypoint_for("gcp")
    assert "--user" in operation.entrypoint_for("azure")
    assert "Agent Card" in operation.verification


def test_hermes_gateway_is_an_azure_only_user_service() -> None:
    operation = get_service_operation("hermes-gateway", "restart")

    assert operation is not None
    assert operation.supports_provider("azure")
    assert not operation.supports_provider("gcp")
    assert operation.entrypoint.startswith("systemctl --user restart")
    assert "is-active" in operation.verification


def test_incomplete_operation_contract_cannot_be_registered() -> None:
    from core.services import ServiceOperationSpec

    with pytest.raises(ValueError, match="preconditions"):
        ServiceOperationSpec(
            service_id="example",
            operation="upgrade",
            entrypoint="sudo -n /usr/local/sbin/example-upgrade",
            rollback_strategy="restore the previous artifact",
            verification="service is healthy",
        )
