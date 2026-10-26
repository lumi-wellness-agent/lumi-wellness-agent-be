"""재시도·폴백(app/agent/llm.py) — 5xx는 재시도 1회, 4xx는 즉시 폴백, 그 밖의 예외는 그대로 전파."""

import asyncio

import httpx2 as httpx
import openai
import pytest

from app.agent import llm
from app.agent.assembly import chat_models, provider_names
from app.agent.llm import ErrorKind, LlmExhaustedError, classify, with_fallback, with_retry
from app.config import Settings


class StatusError(Exception):
    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status_code = status


class Provider:
    """호출마다 outcomes를 차례로 낸다. 예외 객체면 던지고 아니면 돌려준다."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[tuple] = []

    async def __call__(self, *args):
        self.calls.append(args)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(llm, "BACKOFF_SECONDS", 0)


def run(call, *args):
    return asyncio.run(call(*args))


def test_classify_sdk_errors():
    request = httpx.Request("POST", "https://api.example.test")
    assert classify(StatusError(503)) is ErrorKind.RETRYABLE
    assert classify(StatusError(500)) is ErrorKind.RETRYABLE
    assert classify(StatusError(429)) is ErrorKind.CLIENT  # 한도 초과도 재시도하지 않는다
    assert classify(StatusError(401)) is ErrorKind.CLIENT
    assert classify(StatusError(400)) is ErrorKind.CLIENT
    status = openai.APIStatusError("boom", response=httpx.Response(502, request=request), body=None)
    assert classify(status) is ErrorKind.RETRYABLE
    assert classify(openai.APIConnectionError(request=request)) is ErrorKind.RETRYABLE
    assert classify(openai.APITimeoutError(request=request)) is ErrorKind.RETRYABLE
    assert classify(TimeoutError()) is ErrorKind.RETRYABLE
    assert classify(ValueError("우리 코드 버그")) is ErrorKind.OTHER


def test_5xx_is_retried_once_then_succeeds():
    primary = Provider(StatusError(503), "ok")
    assert run(with_retry, primary) == "ok"
    assert len(primary.calls) == 2


def test_5xx_twice_falls_back_to_next_provider():
    primary, fallback = Provider(StatusError(500), StatusError(502)), Provider("haiku")
    call = with_fallback([("openai", primary), ("anthropic", fallback)])
    assert run(call, "req") == "haiku"
    assert primary.calls == [("req",), ("req",)] and fallback.calls == [("req",)]


def test_4xx_is_not_retried_but_falls_back():
    primary, fallback = Provider(StatusError(429)), Provider("haiku")
    call = with_fallback([("openai", primary), ("anthropic", fallback)])
    assert run(call, "req") == "haiku"
    assert len(primary.calls) == 1 and len(fallback.calls) == 1


def test_connection_error_is_retryable():
    request = httpx.Request("POST", "https://api.example.test")
    primary = Provider(openai.APIConnectionError(request=request), "ok")
    assert run(with_fallback([("openai", primary)]), "req") == "ok"
    assert len(primary.calls) == 2


def test_all_providers_failing_raises_exhausted():
    primary, fallback = Provider(StatusError(401)), Provider(StatusError(503), StatusError(503))
    call = with_fallback([("openai", primary), ("anthropic", fallback)])
    with pytest.raises(
        LlmExhaustedError, match="openai=StatusError, anthropic=StatusError"
    ) as info:
        run(call, "req")
    assert set(info.value.failures) == {"openai", "anthropic"}
    assert len(primary.calls) == 1 and len(fallback.calls) == 2


def test_non_provider_errors_propagate_without_retry_or_fallback():
    primary, fallback = Provider(ValueError("우리 코드 버그")), Provider("haiku")
    call = with_fallback([("openai", primary), ("anthropic", fallback)])
    with pytest.raises(ValueError, match="우리 코드 버그"):
        run(call, "req")
    assert len(primary.calls) == 1 and fallback.calls == []


def test_success_on_primary_never_touches_fallback():
    primary, fallback = Provider("gpt"), Provider("haiku")
    assert run(with_fallback([("openai", primary), ("anthropic", fallback)]), "req") == "gpt"
    assert fallback.calls == []


def test_with_fallback_needs_at_least_one_provider():
    with pytest.raises(ValueError):
        with_fallback([])


def test_assembly_registers_providers_by_key_and_disables_sdk_retries():
    assert provider_names(Settings(openai_api_key="", anthropic_api_key="")) == []
    assert provider_names(Settings(openai_api_key="o", anthropic_api_key="")) == ["openai"]
    both = Settings(openai_api_key="o", anthropic_api_key="a")
    assert provider_names(both) == ["openai", "anthropic"]
    models = dict(chat_models(both, max_tokens=64))
    assert models["openai"].model_name == "gpt-4o-mini" and models["openai"].max_retries == 0
    assert models["anthropic"].model == "claude-haiku-5-5" and models["anthropic"].max_retries == 0
    assert models["anthropic"].thinking == {"type": "disabled"}
