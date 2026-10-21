"""체크포인터 — interrupt 사이에 세션 상태를 저장한다.

state에 Pydantic 모델과 Enum을 그대로 둔다. LangGraph의 기본 직렬화기는 등록하지 않은 타입을
체크포인트에서 복원할 때 경고하고, 앞으로는 막겠다고 예고했다(LANGGRAPH_STRICT_MSGPACK).
그래서 허용 목록(STATE_TYPES)을 직접 넘긴다. 목록 밖의 타입은 복원이 막혀 dict나 원시 값으로
돌아오므로, state에 들어가는 모델과 Enum은 모델 안에 든 것까지 등록한다.
빠뜨리면 tests/agent/test_graph.py가 실패한다.

지금은 InMemorySaver다. 프로세스 메모리라 서버를 다시 띄우면 진행 중인 세션이 사라지고, 서버 여러
대가 상태를 나누지 못한다. 운영 전에 langgraph-checkpoint-postgres로 바꿔 Supabase에 저장한다.
체크포인트 테이블은 Data API에 노출되지 않는 스키마에 둔다.
"""

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.agent.state import (
    ActivityContent,
    CarePlan,
    NextStep,
    OutcomeEvaluation,
    PlanMode,
    PlannedActivity,
    ReplanProposal,
    Round,
    SessionReport,
)
from app.schemas.care import CareGoal, CareRequest, ContentType
from app.schemas.evidence import Medium

STATE_TYPES = (
    CareRequest,
    CareGoal,
    ContentType,
    CarePlan,
    PlanMode,
    PlannedActivity,
    Medium,
    ActivityContent,
    SessionReport,
    OutcomeEvaluation,
    NextStep,
    ReplanProposal,
    Round,
)


def serializer() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=STATE_TYPES)


def memory_checkpointer() -> InMemorySaver:
    return InMemorySaver(serde=serializer())
