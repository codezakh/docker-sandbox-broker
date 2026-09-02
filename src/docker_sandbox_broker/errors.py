"""Typed broker errors mapped to stable API responses."""


class BrokerError(RuntimeError):
    code = "broker_error"
    status_code = 500
    retryable = False


class SandboxNotFoundError(BrokerError):
    code = "sandbox_not_found"
    status_code = 404


class PolicyViolationError(BrokerError):
    code = "policy_violation"
    status_code = 422


class RuntimeOperationError(BrokerError):
    code = "runtime_operation_failed"
    status_code = 502


class SandboxOwnershipError(BrokerError):
    code = "sandbox_ownership_mismatch"
    status_code = 409


class PayloadTooLargeError(BrokerError):
    code = "payload_too_large"
    status_code = 413
