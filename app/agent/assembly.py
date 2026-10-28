"""설정(.env)으로 세션 그래프를 조립한다. 공급자는 OpenAI(1순위) → Anthropic(폴백), 모델은 하나씩.

- OPENAI_API_KEY가 있으면 openai(LLM_MODEL, 기본 gpt-5.4-mini)를, ANTHROPIC_API_KEY가 있으면
  anthropic(FALLBACK_MODEL, 기본 claude-haiku-5-5)을 등록한다. 둘 다 비면 조립하지 않는다.
- reasoning_effort는 보내지 않는다. gpt-5.4-mini는 기본이 none이라 추론 토큰이 0이고(10-28 실측),
  gpt-4o-mini 같은 비추론 모델은 이 인자를 400으로 거부해 LLM_MODEL을 되돌리면 늘 폴백으로 간다.
- 재시도·폴백 규칙은 app/agent/llm.py — 5xx·네트워크는 재시도 1회, 4xx는 즉시 다음 공급자.
  벤더 SDK의 자체 재시도는 끈다(max_retries=0). 호출 1회는 LLM_TIMEOUT_SECONDS에서 끊는다 —
  SDK 기본값(OpenAI 600초)은 Vercel 함수 한도(Hobby 300초)보다 길다. 타임아웃은 재시도 대상이다.
- LLM 호출은 전부 구조화 출력 1회(json_schema)다 — intake의 상황 읽기(StateReading)와 목표 추론
  (GoalInference), plan의 활동 선택(ActivityChoice)과 추천 이유(PlanRationale). Tool 루프는 없다.
  Haiku는 thinking을 끈다 — 짧은 분류·문장이라 필요 없고, 켜 두면 langchain-anthropic이
  구조화 출력의 강제 tool_choice를 떼어 낸다.
- 테스트는 대역(tests/agent/fakes.py · test_graph.py)을 쓰고 공급자 패키지를 읽지 않는다.
"""

from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph

from app.agent.graph import build_graph
from app.agent.nodes.intake import LlmIntaker
from app.agent.nodes.planner import LlmPlanner
from app.config import Settings, get_settings

LLM_TIMEOUT_SECONDS = 30  # 호출 1회. 구조화 출력 한 번은 보통 1~3초
MAX_TOKENS = 1024  # 출력 상한. 가장 긴 출력이 추천 이유 2~3문장이다


def chat_models(
    settings: Settings, *, max_tokens: int = MAX_TOKENS
) -> list[tuple[str, BaseChatModel]]:
    """우선순위 순 (이름, 모델). 키가 있는 공급자만."""
    models: list[tuple[str, BaseChatModel]] = []
    if settings.openai_api_key:
        from langchain_openai import ChatOpenAI  # 조립할 때만 읽는다

        models.append(
            (
                "openai",
                ChatOpenAI(
                    model=settings.llm_model,
                    api_key=settings.openai_api_key,
                    max_tokens=max_tokens,
                    max_retries=0,
                    timeout=LLM_TIMEOUT_SECONDS,
                ),
            )
        )
    if settings.anthropic_api_key:
        from langchain_anthropic import ChatAnthropic

        models.append(
            (
                "anthropic",
                ChatAnthropic(
                    model=settings.fallback_model,
                    api_key=settings.anthropic_api_key,
                    max_tokens=max_tokens,
                    max_retries=0,
                    timeout=LLM_TIMEOUT_SECONDS,
                    thinking={"type": "disabled"},
                ),
            )
        )
    return models


def provider_names(settings: Settings) -> list[str]:
    return [name for name, _ in chat_models(settings, max_tokens=1)]


def build_session_graph(
    checkpointer: BaseCheckpointSaver, settings: Settings | None = None
) -> CompiledStateGraph:
    settings = settings or get_settings()
    models = chat_models(settings)
    if not models:
        raise RuntimeError(
            "OPENAI_API_KEY와 ANTHROPIC_API_KEY가 모두 비어 있습니다. .env를 채우세요."
        )
    return build_graph(LlmPlanner(models), LlmIntaker(models), checkpointer)
