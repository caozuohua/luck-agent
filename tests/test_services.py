from __future__ import annotations

from core.services import SERVICE_OPERATIONS, get_service_operation


def test_registered_mutations_have_fixed_entrypoint_rollback_and_verification() -> None:
    assert SERVICE_OPERATIONS
    for spec in SERVICE_OPERATIONS:
        assert spec.service_id
        assert spec.operation
        assert spec.entrypoint.startswith("sudo -n ")
        assert spec.rollback_strategy
        assert spec.verification


def test_unregistered_service_mutation_is_not_available() -> None:
    assert get_service_operation("mem0", "restart") is None
    assert get_service_operation("luck-agent", "upgrade") is None
