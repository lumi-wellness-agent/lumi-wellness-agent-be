"""① Adaptive Care Planning — 이번 라운드의 활동 노드 1개를 고르고 근거를 찾아 검증한다.
고정 절차다.

    후보 · 추천 순서(코드) → 선택(LLM 1회) → 검색(코드) → 검증(코드) → 설명(LLM 1회) → CarePlan

LLM이 실제로 판단하는 것은 둘뿐이다 — 후보 중 어느 활동을 고를지(제약 · 장소 · 자유 텍스트 대조)와
검증된 근거를 어떻게 설명할지. 설명과 함께(같은 호출) 고른 활동의 콘텐츠 Factor(풍경 · 자연음 · 작품
주제)를 카탈로그 값 안에서 고르고, 코드가 활동에 맞게 다듬는다(app/content/factors.sanitize).
나머지(후보 정렬 · 검색 필터 · 검증 · usable만 남기기 · 활동 시간)는
규칙이라 코드가 한다. 처음에는 create_agent Tool 루프였는데 gpt-4o-mini가 검색을 건너뛰고, 인자를
빠뜨리고, 제외한 활동을 다시 고르고, 한 번은 10분 넘게 돌았다(2026-10-26 실측). 절차를 코드로 옮기니
종료가 보장되고 모델 호출이 4~5회에서 2회로 준다. 적응은 이 노드 안의 루프가 아니라 측정 → 평가 →
재계획 → plan 순환(graph.py)에서 온다.

후보의 순서는 등급 가중 추첨이다(protocols.recommendation_order — 세션 ID 시드, 이 사용자가
최근에 한 활동은 가중치를 낮춘다). 등급 순으로만 세우면 목표마다 같은 활동이 1순위로 고정됐다.
LLM은 맨 앞을 고르고 제약 · 장소 · 사용자가 꺼린 활동일 때만 건너뛴다. 순서와 고른 위치를
CarePlan.selection에 남겨 분석에서 무작위성을 보정한다.

후보는 목표 · 사용자 상태 · 시간 · 환경(소리 · 조용함 · 화면 · 야외)으로 거른다. 다 맞는 것이 없으면
사용자 상태 → 환경 순으로 풀고(protocols.plan_candidates), 푼 조건을 CarePlan.relaxed에, 고른 활동이
환경에서 어려운 이유를 environment_gaps에 남긴다. 시간은 그래도 없을 때만 맨 마지막에
풀고(짧은 시간 칩), 그때 활동 시간은 줄이지 않고 근거 용량(프로토콜 기본값)을 쓴다 — relaxed에
minutes가 남는다.

LLM의 선택은 그대로 믿지 않는다. 후보 목록에 있는지와 check_plan(검수 ID · 단계 모듈 · 목표 ·
순서 · 시간 · 환경 · 이번 세션에서 바꾸기로 한 활동 금지)을 확인하고, 어기면 거부 사유를 붙여 한 번
다시 묻는다. 그래도 어기면 PlanRejected다. 근거는 활동을 막지 않는다 — 검증을 통과한 것만
evidence_record_ids에 남기고, 없으면 비운 채 계획한다.

추천 이유도 글자 규칙(app/agent/rationale_rules.py — 다른 문자 · ID · 통계값 · Factor 이름 ·
근거 밖 연도 …)으로 후검증하고, 어기면 사유를 붙여 설명을 한 번 다시 묻는다(write_rationale).
그래도 어기면 두 번째 것을 쓰고 남긴다 — 문장 때문에 계획을 막지 않는다.

직전 라운드에서 '한 번 더'(ADDITIONAL_SESSION)를 골랐으면 활동을 다시 고르지 않는다. 직전 계획의
활동 · 시간 · 푼 조건 · 검색 질의를 그대로 쓰고(LLM 호출 1 없음), 근거 검색 · 검증 · 설명만 다시
돈다 — 설명이 지난 라운드와 다른 콘텐츠 Factor를 고른다.

모델 조립은 app/agent/assembly.py(OpenAI 1순위 · Anthropic 폴백 → LlmPlanner). 테스트는 Planner
대역을 넣는다(tests/agent/test_graph.py FakePlanner).
"""

import logging
from collections.abc import Awaitable, Collection, Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.runtime import Runtime
from pydantic import BaseModel, Field

from app import protocols
from app.agent import rationale_rules, reference_card, validation
from app.agent.context import AgentContext, PlannerContext
from app.agent.llm import with_fallback
from app.agent.prompts import (
    CHOOSE_SYSTEM_PROMPT,
    EXPLAIN_SYSTEM_PROMPT,
    build_choice_input,
    build_rationale_input,
)
from app.agent.state import (
    CarePlan,
    CareState,
    NextStep,
    PlannedActivity,
    Selection,
    SelectionMethod,
    changed_away,
)
from app.content import factors as content_factors
from app.retrieval import hybrid
from app.schemas.card import ReferenceCard
from app.schemas.content import ContentFactors
from app.schemas.evidence import study_medium
from app.schemas.protocol import CareProtocol, Composition
from app.schemas.retrieval import GOAL_OUTCOMES, EvidenceHit, EvidenceVerdict

log = logging.getLogger(__name__)


class ActivityChoice(BaseModel):
    """LLM 호출 1의 구조화 출력 — 후보 중 하나와 그 근거를 찾을 질의. 필드 설명이 LLM에 보인다."""

    protocol_id: str = Field(description="후보 목록에 있는 Care Protocol ID")
    search_query: str = Field(
        description="이 활동의 연구 근거를 찾을 영어 질의 3~6단어 — 활동(매체)과 목표 지표를 논문 "
        "용어로. 예: slow breathing anxiety stress"
    )
    reason: str = Field(
        description="이 활동을 고른 이유 한 문장(한국어). 제약·장소 때문에 앞의 후보를 "
        "건너뛰었으면 그 이유"
    )


class PlanRationale(BaseModel):
    """LLM 호출 2의 구조화 출력 — 추천 이유와 콘텐츠 Factor."""

    rationale: str = Field(
        description="사용자에게 보여 줄 추천 이유 2~3문장(한국어). 꼭 밝힐 것이 있으면 '다만 …' "
        "한 문장을 더한다(최대 4문장)"
    )
    content_factors: ContentFactors = Field(
        description="고른 활동의 콘텐츠 Factor — '## 콘텐츠 Factor' 칸의 값만. 칸이 없으면 비운다"
    )

    @classmethod
    def text(cls, rationale: str) -> "PlanRationale":
        """Factor 없이 추천 이유만 — 테스트 대역 · 콘텐츠 칸이 없는 활동."""
        return cls(rationale=rationale, content_factors=ContentFactors.none())


@dataclass(frozen=True)
class CitedEvidence:
    """검증을 통과한 근거 하나 — 검색 결과(논문 · 발췌)와 판정(지지 지표 · 플래그)."""

    hit: EvidenceHit
    verdict: EvidenceVerdict

    @property
    def evidence_record_id(self) -> int:
        return self.hit.evidence_record_id


class Planner(Protocol):
    """LLM이 하는 두 판단. 운영은 LlmPlanner, 테스트는 대역을 넣는다."""

    def choose(
        self, state: CareState, candidates: Sequence[dict], problems: Sequence[str]
    ) -> Awaitable[ActivityChoice]: ...

    def explain(
        self,
        state: CareState,
        choice: ActivityChoice,
        protocol: CareProtocol,
        evidence: Sequence[CitedEvidence],
        problems: Sequence[str] = (),
    ) -> Awaitable[PlanRationale]: ...


class PlanRejected(Exception):
    """후보가 없거나, 선택이 후검증을 두 번 통과하지 못했다.

    retryable — 같은 state로 plan을 다시 돌리면 달라질 수 있는가. 후보가 없으면 규칙이라 아니고,
    LLM 선택이 두 번 어긋났으면 다시 고를 수 있다(세션 API의 retry).
    """

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def check_plan(
    activity: PlannedActivity, state: CareState, relaxed: Collection[str] = ()
) -> list[str]:
    """활동 후검증. 어긴 규칙을 돌려준다 (비어 있으면 통과).

    검수 ID · 단계 모듈 제외 · 목표 일치 · 순서 조건 · 시간 범위 · 환경 · 이번 세션에서 바꾸기로
    한 활동 금지. 후보 조회(protocols.plan_candidates)가 같은 조건으로 거르지만, LLM이 목록 밖을
    고를 수 있어 따로 둔다. 후보 조회가 푼 조건(relaxed — order · environment)은 여기서도 보지
    않는다.
    """
    intake = state["intake"]
    protocol = protocols.get_protocol(activity.protocol_id)
    if protocol is None:
        return [f"{activity.protocol_id}는 검수된 프로토콜이 아니다"]
    problems = []
    if protocol.composition is Composition.STAGE_MODULE:
        problems.append(f"{activity.protocol_id}는 단계 모듈이라 단독 활동으로 쓸 수 없다")
    if intake.goal and not protocol.serves(intake.goal):
        problems.append(f"{activity.protocol_id}는 {intake.goal.value} 목표용이 아니다")
    if "order" not in relaxed and not protocol.allows_order(intake.order_condition):
        allowed = [o.value for o in protocol.intervention_order]
        problems.append(f"사용자 상태 {intake.order_condition}는 허용 상태 {allowed}에 맞지 않는다")
    gaps = protocols.environment_gaps(protocol, intake.environment)
    if "environment" not in relaxed and gaps:
        problems.append(f"{activity.protocol_id}는 지금 환경에서 하기 어렵다({', '.join(gaps)})")
    duration = protocol.duration_minutes
    if duration.min is not None and activity.minutes < duration.min:
        problems.append(f"활동 시간 {activity.minutes}분이 최소 {duration.min}분보다 짧다")
    if duration.max is not None and activity.minutes > duration.max:
        problems.append(f"활동 시간 {activity.minutes}분이 최대 {duration.max}분보다 길다")
    if "minutes" not in relaxed and intake.minutes and activity.minutes > intake.minutes:
        problems.append(f"활동 시간 {activity.minutes}분이 가용 시간 {intake.minutes}분을 넘는다")
    if activity.protocol_id in excluded_protocols(state):
        problems.append(f"{activity.protocol_id}는 이번 세션에서 바꾸기로 한 활동이다")
    return problems


def excluded_protocols(state: CareState) -> tuple[str, ...]:
    """이번 라운드에 내놓지 않을 프로토콜 — 이번 세션에서 바꾸기로 한 활동 전부.

    직전 라운드 것만 빼면 활동 변경을 두 번 고른 3라운드에 1라운드 활동이 다시 후보 1순위로
    돌아온다. check_plan의 '이번 세션에서 바꾸기로 한 활동 금지'와 같은 규칙이다.
    """
    return changed_away(state.get("rounds") or [])


def repeated_plan(state: CareState) -> CarePlan | None:
    """직전 라운드에서 '한 번 더'(ADDITIONAL_SESSION)를 골랐으면 그 라운드의 계획."""
    rounds = state.get("rounds") or []
    if rounds and rounds[-1].next_step is NextStep.ADDITIONAL_SESSION:
        return rounds[-1].plan
    return None


def planner_context(context: AgentContext | None, state: CareState) -> PlannerContext:
    """그래프 컨텍스트에 이번 세션의 정해진 값(인테이크 · 이미지 선호 · 뺄 활동 · 최근에 한 활동)을
    더한 컨텍스트."""
    intake = state["intake"]
    return PlannerContext(
        db_pool=context.db_pool if context else None,
        session_id=context.session_id if context else None,
        goal=intake.goal,
        user_order=intake.order_condition,
        minutes_available=intake.minutes,
        environment=intake.environment,
        content_type=state["request"].profile.content_type,
        excluded_protocol_ids=excluded_protocols(state),
        recent_protocol_ids=tuple(state.get("recent_protocol_ids") or ()),
    )


def search_candidates(
    session: PlannerContext, exclude: Iterable[str] | None = None
) -> protocols.CandidateSearch:
    """세션 값으로 후보를 찾는다. plan과 replan(활동 변경을 고를 수 있는지)이 같은 조회를 본다.

    exclude를 주지 않으면 세션의 뺄 활동(직전 라운드에서 바꾸기로 한 활동)을 뺀다.
    """
    if session.goal is None:  # intake가 목표 없이는 세션을 열지 않는다
        raise PlanRejected("목표가 없어 계획할 수 없다")
    return protocols.plan_candidates(
        session.goal,
        order=session.user_order,
        minutes=session.minutes_available,
        environment=session.environment,
        exclude=session.excluded_protocol_ids if exclude is None else exclude,
    )


async def find_evidence(
    session: PlannerContext, protocol: CareProtocol, query: str
) -> list[EvidenceHit]:
    """그 활동의 evidence[] 안에서 먼저 찾고, 0건이면 같은 필터로 전체에서 찾는다.

    evidence[]는 활동 노드 담당이 원문을 보고 고른 레코드다. 매체가 같은 활동이 여럿이라(자연
    이미지 4개 · 명상 3개 · 호흡 2개 …) 매체 필터만으로는 다른 활동의 근거가 섞였다 — 2026-10-28
    평가에서 template 질의 상위 5 레코드의 23%(scripts/rag_eval.py Foreign@5). evidence[]가 비었거나
    (PR-21) 그 안에 목표 지표 · 사용자 상태에 맞는 레코드가 없을 때만 같은 매체 연구 전체로 넓힌다.

    필터는 검증(validation.judge)과 같은 축이다 — 연구 매체 · 사용자 상태(ORDER_COMPATIBILITY) ·
    목표 1차 지표. 그래서 0건이면 인용할 근거가 없다는 뜻이고, 이 필터는 완화하지 않는다: 완화해서
    찾은 레코드는 어차피 검증에서 막힌다.
    """
    own = [ref.key for ref in protocol.evidence]  # ARM은 experiment_id, 리뷰는 paper_id
    if own:
        hits = await _search_media(session, protocol, query, own)
        if hits:
            return hits
        log.info("%s evidence[]에 맞는 근거가 없어 같은 매체 연구에서 찾는다", protocol.protocol_id)
    return await _search_media(session, protocol, query)


async def _search_media(
    session: PlannerContext,
    protocol: CareProtocol,
    query: str,
    evidence_keys: Sequence[str] = (),
) -> list[EvidenceHit]:
    """제공 매체마다 하이브리드 검색을 돌려 점수 순으로 합치고, 서로 다른 레코드 상위
    hybrid.TOP_K개의 chunk를 모두 남긴다(hybrid.top_records)."""
    delivered = protocols.delivered_media(protocol, session.content_type)
    found: dict[int, EvidenceHit] = {}
    for medium in dict.fromkeys(study_medium(m) for m in delivered):
        hits = await hybrid.search(
            session.db_pool,
            query=query,
            medium=medium,
            intervention_order=session.user_order,
            outcomes=GOAL_OUTCOMES[session.goal].primary,
            evidence_keys=evidence_keys,
            limit=hybrid.CHUNK_LIMIT,
        )
        for hit in hits:
            known = found.get(hit.evidence_record_id)
            if known is None or hit.score > known.score:
                found[hit.evidence_record_id] = hit
    ranked = sorted(found.values(), key=lambda h: (-h.score, h.evidence_record_id))
    return hybrid.top_records(ranked)


async def verify_evidence(
    session: PlannerContext, protocol: CareProtocol, hits: Sequence[EvidenceHit]
) -> list[CitedEvidence]:
    """검증을 통과한(usable) 근거만, 목표를 지지하는 것(supports_goal)을 앞에. 검색 순서는 유지."""
    if not hits:
        return []
    verdicts = await validation.validate(
        session.db_pool,
        evidence_record_ids=[hit.evidence_record_id for hit in hits],
        goal=session.goal,
        media=protocols.delivered_media(protocol, session.content_type),
        protocol_id=protocol.protocol_id,
        user_order=session.user_order,
    )
    by_id = {hit.evidence_record_id: hit for hit in hits}
    cited = [CitedEvidence(by_id[v.evidence_record_id], v) for v in verdicts if v.usable]
    return sorted(cited, key=lambda c: not c.verdict.supports_goal)


def make_chooser(model: BaseChatModel):
    """LLM 호출 1 — 구조화 출력(json_schema) 한 번으로 ActivityChoice를 받는다."""
    structured = model.with_structured_output(ActivityChoice, method="json_schema")

    async def choose(
        state: CareState, candidates: Sequence[dict], problems: Sequence[str]
    ) -> ActivityChoice:
        messages = [
            SystemMessage(CHOOSE_SYSTEM_PROMPT),
            HumanMessage(build_choice_input(state, candidates, problems)),
        ]
        return await structured.ainvoke(messages)

    return choose


def make_explainer(model: BaseChatModel):
    """LLM 호출 2 — 검증된 근거로 추천 이유(rationale)를 쓰고 콘텐츠 Factor를 고른다."""
    structured = model.with_structured_output(PlanRationale, method="json_schema")

    async def explain(
        state: CareState,
        choice: ActivityChoice,
        protocol: CareProtocol,
        evidence: Sequence[CitedEvidence],
        problems: Sequence[str] = (),
    ) -> PlanRationale:
        messages = [
            SystemMessage(EXPLAIN_SYSTEM_PROMPT),
            HumanMessage(build_rationale_input(state, choice, protocol, evidence, problems)),
        ]
        return await structured.ainvoke(messages)

    return explain


class LlmPlanner:
    """공급자별 모델로 만든 choose · explain에 재시도·폴백(app/agent/llm.py)을 두른 Planner."""

    def __init__(self, models: Sequence[tuple[str, BaseChatModel]]):
        self._choose = with_fallback([(name, make_chooser(m)) for name, m in models])
        self._explain = with_fallback([(name, make_explainer(m)) for name, m in models])

    async def choose(
        self, state: CareState, candidates: Sequence[dict], problems: Sequence[str]
    ) -> ActivityChoice:
        return await self._choose(state, candidates, problems)

    async def explain(
        self,
        state: CareState,
        choice: ActivityChoice,
        protocol: CareProtocol,
        evidence: Sequence[CitedEvidence],
        problems: Sequence[str] = (),
    ) -> PlanRationale:
        return await self._explain(state, choice, protocol, evidence, problems)


REPEAT_REASON = "사용자가 같은 활동을 한 번 더 하기로 했다"
# LLM에 보이는 후보 수 — 추천 순서 앞에서부터. 제약 · 꺼린 활동이면 건너뛸 여지는 두되, 뒤쪽의
# 익숙한 활동(PR-06 · PR-01)을 찾아 내려가 추첨을 되돌리지 못하게 한다. 전부 보였을 때
# gpt-5.4-mini가 맨 앞을 고른 것은 36번 중 27번이었고, '5분이라 부담이 적다'며 9번째까지
# 내려갔다(2026-10-29 plan_eval)
CHOICE_WINDOW = 5


@dataclass(frozen=True)
class Selected:
    """고른 활동 — 프로토콜 · 선택 · 시간을 정한 활동 · 후보 조회가 푼 조건 · 고른 과정."""

    protocol: CareProtocol
    choice: ActivityChoice
    activity: PlannedActivity
    relaxed: tuple[str, ...]
    selection: Selection


async def choose_activity(planner: Planner, state: CareState, session: PlannerContext) -> Selected:
    """후보(코드) → 추천 순서(코드) → 선택(LLM 호출 1) → 후검증(check_plan). 어기면 거부 사유를 붙여
    한 번 더 묻는다."""
    search = search_candidates(session)
    found = search.protocols
    if not found:
        raise PlanRejected(
            f"{session.goal.value} 목표 · 사용자 상태 {session.user_order} · "
            f"{session.minutes_available}분에 맞는 검수 프로토콜이 없다"
        )
    if search.relaxed:
        log.info("후보가 없어 %s 조건을 풀었다", " · ".join(search.relaxed))
    # 시간을 풀었으면 가용 시간으로 자르지 않는다 — 근거 용량 그대로
    available = None if "minutes" in search.relaxed else session.minutes_available
    order = protocols.recommendation_order(
        search, session.goal, seed=session.session_id, recent=session.recent_protocol_ids
    )
    ids = [p.protocol_id for p in order.protocols]
    by_id = {p.protocol_id: p for p in found}
    # LLM에는 앞 CHOICE_WINDOW개만 보인다. 검증은 후보 전체로 한다 — 보이지 않은 활동은 LLM이 고를
    # 수 없고, 대역(테스트)이 고른 활동도 후보면 받는다
    shown = order.protocols[:CHOICE_WINDOW]
    candidates = [{"rank": i, **protocols.compact(p)} for i, p in enumerate(shown, start=1)]

    problems: list[str] = []
    for _ in range(2):  # 처음 한 번 + 거부 사유를 붙여 한 번 더
        choice = await planner.choose(state, candidates, problems)
        protocol = by_id.get(choice.protocol_id)
        if protocol is None:
            problems = [f"{choice.protocol_id}는 후보 목록에 없다"]
            known = protocols.get_protocol(choice.protocol_id)
            if known is not None:  # 왜 후보가 아닌지도 알려 준다
                minutes = protocols.planned_minutes(known, available)
                activity = PlannedActivity(protocol_id=known.protocol_id, minutes=minutes)
                problems += check_plan(activity, state, search.relaxed)
            continue
        minutes = protocols.planned_minutes(protocol, available)
        activity = PlannedActivity(protocol_id=protocol.protocol_id, minutes=minutes)
        problems = check_plan(activity, state, search.relaxed)
        if not problems:
            selection = Selection(
                method=SelectionMethod(order.method),
                order=ids,
                weights=order.weights,
                probability=order.probability(protocol.protocol_id),
                position=ids.index(protocol.protocol_id),
            )
            return Selected(protocol, choice, activity, search.relaxed, selection)
    raise PlanRejected("; ".join(problems), retryable=True)


def repeat_activity(previous: CarePlan, state: CareState) -> Selected:
    """'한 번 더' — 직전 계획의 활동 · 시간 · 푼 조건 · 검색 질의를 그대로 쓴다(LLM 호출 없음)."""
    protocol_id = previous.activity.protocol_id
    protocol = protocols.get_protocol(protocol_id)
    if protocol is None:
        raise PlanRejected(f"{protocol_id}는 검수된 프로토콜이 아니다")
    activity = PlannedActivity(protocol_id=protocol_id, minutes=previous.activity.minutes)
    relaxed = tuple(previous.relaxed)
    if problems := check_plan(activity, state, relaxed):
        raise PlanRejected("; ".join(problems))
    # 질의를 남기기 전의 계획이면 활동 이름으로 찾는다 — 검색은 그 활동의 evidence[]부터 본다
    query = previous.search_query or protocol.name
    choice = ActivityChoice(protocol_id=protocol_id, search_query=query, reason=REPEAT_REASON)
    selection = Selection(method=SelectionMethod.REPEAT, order=[protocol_id])
    return Selected(protocol, choice, activity, relaxed, selection)


async def write_rationale(
    planner: Planner,
    state: CareState,
    choice: ActivityChoice,
    protocol: CareProtocol,
    evidence: Sequence[CitedEvidence],
    card: ReferenceCard,
) -> tuple[str, ContentFactors]:
    """설명(LLM 호출 2) + 후검증 — 추천 이유와 다듬은 콘텐츠 Factor.

    추천 이유가 글자 규칙(rationale_rules)을 어기면 사유를 붙여 한 번 더 묻는다. 2026-10-29
    plan eval에서 한국어 문장에 아랍 · 데바나가리 문자가 섞이거나(108개 중 3개) 활동 ID · 고른
    Factor를 문장에 썼다(scripts/rationale_eval.py). 그래도 어기면 두 번째 것을 쓰고 남긴다.
    """
    cited = [(c.hit, c.verdict) for c in evidence]
    badges = [b.value for b in card.badges]
    problems: list[str] = []
    for attempt in (1, 2):
        explained = await planner.explain(state, choice, protocol, evidence, problems)
        factors = content_factors.sanitize(protocol, explained.content_factors)
        chosen = None if factors.is_empty() else factors.model_dump(mode="json")
        facts = rationale_rules.facts_for(protocol, state["intake"].goal, cited, badges, chosen)
        problems = rationale_rules.problems(explained.rationale, facts)
        if not problems:
            break
        log.warning("추천 이유 규칙 위반 %s (%d번째): %s", protocol.protocol_id, attempt, problems)
    return explained.rationale, factors


def make_plan_node(planner: Planner):
    async def plan(state: CareState, runtime: Runtime[AgentContext]) -> dict:
        # 단계마다 세션 API의 SSE progress(custom)를 낸다. 스트리밍이 아니면 아무 일도 하지 않는다
        progress = runtime.stream_writer
        session = planner_context(runtime.context, state)
        if session.goal is None:  # intake가 목표 없이는 세션을 열지 않는다
            raise PlanRejected("목표가 없어 계획할 수 없다")
        if (previous := repeated_plan(state)) is not None:
            selected = repeat_activity(previous, state)
        else:
            progress({"node": "plan", "step": "choose_activity"})
            selected = await choose_activity(planner, state, session)
        protocol, choice, activity = selected.protocol, selected.choice, selected.activity
        relaxed = selected.relaxed
        log.info(
            "계획 선택 %s %d분 (%s · %s %d번째) — 질의 %r",
            activity.protocol_id,
            activity.minutes,
            choice.reason,
            selected.selection.method.value,
            selected.selection.position + 1,
            choice.search_query,
        )

        progress({"node": "plan", "step": "search_evidence"})
        hits = await find_evidence(session, protocol, choice.search_query)
        progress({"node": "plan", "step": "verify_evidence"})
        evidence = await verify_evidence(session, protocol, hits)
        log.info(
            "계획 근거 %s: 검색 %s → 인용 %s",
            activity.protocol_id,
            [h.evidence_record_id for h in hits],
            [
                f"{c.evidence_record_id}{'' if c.verdict.supports_goal else '(참고)'}"
                for c in evidence
            ],
        )
        activity = activity.model_copy(
            update={"evidence_record_ids": [c.evidence_record_id for c in evidence]}
        )
        # 카드는 LLM 없이 근거로 만든다 — 추천 이유 후검증이 배지('실험적' 허용)를 본다
        card = reference_card.build_card(
            state["intake"],
            protocol,
            activity,
            [(c.hit, c.verdict) for c in evidence],
            delivered=protocols.delivered_media(protocol, session.content_type),
            relaxed=relaxed,
        )
        progress({"node": "plan", "step": "write_rationale"})
        rationale, factors = await write_rationale(planner, state, choice, protocol, evidence, card)
        if not factors.is_empty():
            log.info(
                "콘텐츠 Factor %s: %s (%s)",
                activity.protocol_id,
                factors.model_dump(mode="json", exclude={"reason"}),
                factors.reason,
            )
        plan = CarePlan(
            activity=activity,
            rationale=rationale,
            content_factors=None if factors.is_empty() else factors,
            relaxed=list(relaxed),
            environment_gaps=protocols.environment_gaps(protocol, session.environment),
            reference_card=card,
            search_query=choice.search_query,
            selection=selected.selection,
        )
        return {"plan": plan}

    return plan
