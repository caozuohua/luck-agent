from __future__ import annotations

from core.services import SERVICE_OPERATIONS, get_service_operation


def test_registered_mutations_have_fixed_entrypoint_rollback_and_verification() -> None:
    assert SERVICE_OPERATIONS
    for spec in SERVICE_OPERATIONS:
        assert spec.service_id
        assert spec.operation
        assert spec.entrypoint.startswith(("sudo -n ", "systemctl --user "))
        assert spec.rollback_strategy
        assert spec.verification


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
