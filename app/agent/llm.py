"""LLM 호출의 재시도·폴백 — cure-agent-be의 4단 방어 중 재시도(§11-1)·폴백 라우터(§11-4)를 옮겼다.

- 프로바이더 우선순위: OpenAI(gpt-5.4-mini) → Anthropic(claude-haiku-5-5). 키가 있는 것만 등록한다
  (app/agent/assembly.py).
- 프로바이더당 2회 시도(재시도 1회, 300ms × 시도 번호 백오프). 재시도는 **5xx·네트워크·타임아웃**만.
  **4xx(400 · 401 · 403 · 429 …)는 재시도하지 않고** 바로 다음 프로바이더로 넘어간다 — 같은 요청을
  다시 보내도 결과가 같다(설정 · 키 · 한도 문제).
- 벤더 SDK의 자체 재시도(기본 2회, 429·5xx)는 끈다(`max_retries=0`). 여기 규칙과 이중화되기
  때문이다.
- 상태 코드도 연결 오류도 아닌 예외(우리 코드의 ValueError, PlanRejected …)는 프로바이더 장애가
  아니므로 재시도·폴백 없이 그대로 올린다. cure-agent-be는 어댑터가 프로바이더 오류를
  LlmProviderError로 감싸는데, 여기서는 SDK 예외(`status_code` · APIConnectionError)로 직접 가른다.
- 전 프로바이더 실패 → LlmExhaustedError. 서킷 브레이커 · rate-limit 차단 저장소는 발표 범위 밖이다.

플래너(Tool 루프)와 intake(구조화 출력 1회)를 호출 단위로 감싼다. 플래너는 재시도·폴백 때 Tool
호출까지
처음부터 다시 도는데, 도구가 읽기 전용(프로토콜 조회 · 근거 검색 · 검증)이라 부작용이 없다.
"""

import asyncio
import importlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from enum import StrEnum
from functools import partial
from typing import Any

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 2  # 프로바이더당. 1회 재시도
BACKOFF_SECONDS = 0.3  # × 시도 번호

AsyncCall = Callable[..., Awaitable[Any]]
Provider = tuple[str, AsyncCall]


class ErrorKind(StrEnum):
    RETRYABLE = "RETRYABLE"  # 5xx · 네트워크 · 타임아웃 → 같은 프로바이더에 재시도 1회
    CLIENT = "CLIENT"  # 4xx → 재시도 없이 다음 프로바이더
    OTHER = "OTHER"  # 프로바이더 장애가 아님 → 그대로 올린다


def _connection_error_types() -> tuple[type[BaseException], ...]:
    types: list[type[BaseException]] = [TimeoutError, ConnectionError]
    for module in ("openai", "anthropic"):
        try:
            sdk = importlib.import_module(module)
        except ImportError:
            continue
        types.append(sdk.APIConnectionError)  # APITimeoutError는 이 하위 클래스다
    return tuple(types)


def classify(exc: BaseException) -> ErrorKind:
    """SDK 예외를 재시도·폴백·전파로 가른다. 상태 코드는 두 SDK의 APIStatusError가 가진다."""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        return ErrorKind.RETRYABLE if status >= 500 else ErrorKind.CLIENT
    if isinstance(exc, _connection_error_types()):
        return ErrorKind.RETRYABLE
    return ErrorKind.OTHER


class LlmExhaustedError(Exception):
    """전 프로바이더 실패. 프로바이더별 마지막 오류를 failures에 남긴다."""

    def __init__(self, failures: dict[str, BaseException]):
        self.failures = failures
        detail = ", ".join(f"{name}={type(exc).__name__}" for name, exc in failures.items())
        super().__init__(f"사용 가능한 LLM 프로바이더가 없습니다 ({detail})")


async def with_retry(attempt: Callable[[], Awaitable[Any]], *, name: str = "llm") -> Any:
    """일시 오류(RETRYABLE)만 MAX_ATTEMPTS까지 다시 시도한다. 그 밖의 오류는 그대로 올린다."""
    for i in range(MAX_ATTEMPTS):
        try:
            return await attempt()
        except Exception as exc:
            if classify(exc) is not ErrorKind.RETRYABLE or i == MAX_ATTEMPTS - 1:
                raise
            delay = BACKOFF_SECONDS * (i + 1)
            log.warning(
                "LLM %s 일시 오류(%s: %s) — %.1fs 뒤 재시도", name, type(exc).__name__, exc, delay
            )
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


def with_fallback(providers: Sequence[Provider]) -> AsyncCall:
    """우선순위 폴백. 프로바이더마다 with_retry를 두르고, 실패하면 다음 프로바이더로 간다."""
    if not providers:
        raise ValueError("LLM 프로바이더가 하나도 없다")

    async def call(*args: Any, **kwargs: Any) -> Any:
        failures: dict[str, BaseException] = {}
        for name, fn in providers:
            try:
                return await with_retry(partial(fn, *args, **kwargs), name=name)
            except Exception as exc:
                if classify(exc) is ErrorKind.OTHER:
                    raise
                failures[name] = exc
                log.warning(
                    "LLM 프로바이더 %s 실패(%s): %s — 다음으로", name, type(exc).__name__, exc
                )
        raise LlmExhaustedError(failures)

    return call
