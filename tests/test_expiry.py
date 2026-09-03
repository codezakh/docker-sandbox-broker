from datetime import timedelta

import pytest

from docker_sandbox_broker.config import BrokerSettings
from docker_sandbox_broker.models import CreateSandboxRequest
from docker_sandbox_broker.service import BrokerService

AUTH_TOKEN = "test-token-that-is-long-enough"


def _service(fake_runtime, **overrides):
    settings = BrokerSettings(auth_token=AUTH_TOKEN, broker_id="test-broker", **overrides)
    return BrokerService(settings, fake_runtime)


def _after(sandbox, seconds=1):
    """A moment past a sandbox's deadline, so a sweep sees it as expired."""
    return sandbox.expires_at + timedelta(seconds=seconds)


def describe_sandbox_expiry():
    def it_reclaims_a_sandbox_whose_client_never_deleted_it(fake_runtime):
        """Deletes an expired sandbox, which is what a departed client leaves."""
        service = _service(fake_runtime)
        sandbox = service.create(CreateSandboxRequest(image="alpine:3.20"))

        assert service.sweep_expired(now=_after(sandbox)) == [sandbox.id]
        assert service.list() == []

    def it_leaves_a_sandbox_that_has_not_expired(fake_runtime):
        """Keeps a sandbox inside its lifetime, however idle it looks."""
        service = _service(fake_runtime)
        sandbox = service.create(CreateSandboxRequest(image="alpine:3.20"))

        assert service.sweep_expired() == []
        assert [view.id for view in service.list()] == [sandbox.id]

    def it_prefers_the_lifetime_the_request_asks_for(fake_runtime):
        """Honours a per-request ttl_seconds over the broker default."""
        service = _service(fake_runtime, sandbox_ttl_seconds=1800)
        sandbox = service.create(CreateSandboxRequest(image="alpine:3.20", ttl_seconds=7200))

        assert sandbox.expires_at == sandbox.created_at + timedelta(seconds=7200)

    def it_applies_the_broker_default_when_the_request_is_silent(fake_runtime):
        """Uses the configured default, so a client cannot leak by omission."""
        service = _service(fake_runtime, sandbox_ttl_seconds=900)
        sandbox = service.create(CreateSandboxRequest(image="alpine:3.20"))

        assert sandbox.expires_at == sandbox.created_at + timedelta(seconds=900)

    def it_reports_no_deadline_when_expiry_is_disabled(fake_runtime):
        """Zero returns the broker to relying on clients to delete their own."""
        service = _service(fake_runtime, sandbox_ttl_seconds=0)
        sandbox = service.create(CreateSandboxRequest(image="alpine:3.20"))

        assert sandbox.expires_at is None
        assert service.sweep_expired() == []

    def it_expires_only_the_sandboxes_that_are_due(fake_runtime):
        """Sweeps per sandbox rather than by batch, so live work survives."""
        service = _service(fake_runtime)
        stale = service.create(CreateSandboxRequest(image="alpine:3.20", ttl_seconds=1))
        fresh = service.create(CreateSandboxRequest(image="alpine:3.20", ttl_seconds=3600))

        assert service.sweep_expired(now=_after(stale)) == [stale.id]
        assert [view.id for view in service.list()] == [fresh.id]

    def it_keeps_sweeping_after_one_sandbox_fails_to_delete(fake_runtime, monkeypatch):
        """One undeletable sandbox must not strand every other expired one."""
        service = _service(fake_runtime)
        first = service.create(CreateSandboxRequest(image="alpine:3.20"))
        second = service.create(CreateSandboxRequest(image="alpine:3.20"))

        original = service.delete

        def delete(sandbox_id):
            if sandbox_id == first.id:
                raise RuntimeError("runtime is unavailable")
            original(sandbox_id)

        monkeypatch.setattr(service, "delete", delete)

        assert service.sweep_expired(now=_after(first)) == [second.id]

    def it_rejects_a_lifetime_below_one_second(fake_runtime):
        """A zero or negative ttl would delete a sandbox before it is usable."""
        with pytest.raises(ValueError):
            CreateSandboxRequest(image="alpine:3.20", ttl_seconds=0)
