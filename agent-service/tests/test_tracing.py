"""Langfuse setup: off without keys, masking on when enabled."""

from typing import Any

import pytest
from pydantic import SecretStr

from core.masking import langfuse_mask
from core.settings import Settings
from core.tracing import Tracing, init_langfuse


def test_tracing_is_off_without_keys() -> None:
    tracing = Tracing.from_settings(Settings(langfuse_public_key="", langfuse_secret_key=""))
    assert not tracing.enabled
    config = tracing.config(run_name="assistant", session_id="conv-1")
    assert config["callbacks"] == []
    assert config["metadata"]["llm_profile"] == "ollama"
    assert config["metadata"]["langfuse_session_id"] == "conv-1"


def test_langfuse_client_gets_the_mask_function(monkeypatch: pytest.MonkeyPatch) -> None:
    created: dict[str, Any] = {}

    class FakeLangfuse:
        def __init__(self, **kwargs: Any) -> None:
            created.update(kwargs)

    import langfuse

    monkeypatch.setattr(langfuse, "Langfuse", FakeLangfuse)
    settings = Settings(
        langfuse_public_key="pk-lf-test",
        langfuse_secret_key=SecretStr("sk-lf-test"),
        langfuse_host="http://langfuse:3000",
        llm_profile="mixed",
    )
    client = init_langfuse(settings)
    assert isinstance(client, FakeLangfuse)
    assert created["mask"] is langfuse_mask
    assert created["host"] == "http://langfuse:3000"
    assert created["secret_key"] == "sk-lf-test"
    assert created["environment"] == "profile-mixed"


def test_trace_config_is_masked_and_tagged() -> None:
    tracing = Tracing(client=None, profile="anthropic")
    config = tracing.config(
        run_name="investigation",
        user_id="11111111-1111-4111-8111-111111111111",
        tags=["case_decline"],
        metadata={"prompt_version": "triage-v1", "note": "card 4111 1111 1111 1111", "expected_root_cause": "x"},
    )
    meta = config["metadata"]
    assert meta["prompt_version"] == "triage-v1"
    assert meta["note"] == "card [CARD ****1111]"
    assert meta["expected_root_cause"] == "[REDACTED]"
    assert meta["langfuse_user_id"] == "11111111-1111-4111-8111-111111111111"
    assert "profile:anthropic" in config["tags"] and "case_decline" in config["tags"]


def test_callback_handler_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    import langfuse.langchain

    class FakeHandler:
        pass

    monkeypatch.setattr(langfuse.langchain, "CallbackHandler", FakeHandler)
    tracing = Tracing(client=object(), profile="ollama")
    assert isinstance(tracing.callbacks()[0], FakeHandler)
