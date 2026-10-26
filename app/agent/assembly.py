"""설정(.env)으로 세션 그래프를 조립한다. 공급자는 OpenAI(1순위) → Anthropic(폴백), 모델은 하나씩.

- OPENAI_API_KEY가 있으면 openai(LLM_MODEL, 기본 gpt-4o-mini)를, ANTHROPIC_API_KEY가 있으면
  anthropic(FALLBACK_MODEL, 기본 claude-haiku-5-5)을 등록한다. 둘 다 비면 조립하지 않는다.
- 재시도·폴백 규칙은 app/agent/llm.py — 5xx·네트워크는 재시도 1회, 4xx는 즉시 다음 공급자.
  벤더 SDK의 자체 재시도는 끈다(max_retries=0). 호출 1회는 LLM_TIMEOUT_SECONDS에서 끊는다 —
  SDK 기본값(OpenAI 600초)은 Vercel 함수 한도(Hobby 300초)보다 길다. 타임아웃은 재시도 대상이다.
- intake: 구조화 출력 1회(json_schema). plan: create_agent + ToolStrategy(CarePlan을 Tool 호출로
  제출).
  Haiku는 thinking을 끈다 — 켜 두면 langchain-anthropic이 ToolStrategy의 강제 tool_choice를 떼어
  내 CarePlan 제출이 보장되지 않는다.
- 테스트는 대역(tests/agent/fakes.py)을 쓰고 공급자 패키지를 읽지 않는다.
"""

from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph

from app.agent.graph import build_graph
from app.agent.llm import with_fallback
from app.agent.nodes.intake import make_llm_intaker
from app.agent.nodes.planner import make_agent_planner
from app.config import Settings, get_settings

LLM_TIMEOUT_SECONDS = 30  # 호출 1회. 플래너 호출은 보통 1~4초(입력 ~2만 토큰)


def chat_models(settings: Settings, *, max_tokens: int) -> list[tuple[str, BaseChatModel]]:
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
    planner_models = chat_models(settings, max_tokens=4096)
    if not planner_models:
        raise RuntimeError(
            "OPENAI_API_KEY와 ANTHROPIC_API_KEY가 모두 비어 있습니다. .env를 채우세요."
        )
    planner = with_fallback([(name, make_agent_planner(m)) for name, m in planner_models])
    intaker = with_fallback(
        [(name, make_llm_intaker(m)) for name, m in chat_models(settings, max_tokens=1024)]
    )
    return build_graph(planner, intaker, checkpointer)
