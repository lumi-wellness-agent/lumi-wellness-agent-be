"""① Adaptive Care Planning — 이번 라운드의 활동 노드 1개를 고르고 근거를 찾아 검증한다.
고정 절차다.

    후보(코드) → 선택(LLM 1회) → 검색(코드) → 검증(코드) → 설명(LLM 1회) → CarePlan

LLM이 실제로 판단하는 것은 둘뿐이다 — 후보 중 어느 활동을 고를지(제약 · 장소 · 자유 텍스트 대조)와
검증된 근거를 어떻게 설명할지. 나머지(후보 정렬 · 검색 필터 · 검증 · usable만 남기기 · 활동 시간)는
규칙이라 코드가 한다. 처음에는 create_agent Tool 루프였는데 gpt-4o-mini가 검색을 건너뛰고, 인자를
빠뜨리고, 제외한 활동을 다시 고르고, 한 번은 10분 넘게 돌았다(2026-10-26 실측). 절차를 코드로 옮기니
종료가 보장되고 모델 호출이 4~5회에서 2회로 준다. 적응은 이 노드 안의 루프가 아니라 측정 → 평가 →
재계획 → plan 순환(graph.py)에서 온다.

후보는 목표 · 사용자 상태 · 시간 · 환경(소리 · 조용함 · 화면 · 야외)으로 거른다. 다 맞는 것이 없으면
사용자 상태 → 환경 순으로 풀고(protocols.plan_candidates), 푼 조건을 CarePlan.relaxed에, 고른 활동이
환경에서 어려운 이유를 environment_gaps에 남긴다. 시간은 풀지 않는다.

LLM의 선택은 그대로 믿지 않는다. 후보 목록에 있는지와 check_plan(검수 ID · 단계 모듈 · 목표 ·
순서 · 시간 · 환경 · 활동 변경 시 같은 프로토콜 금지)을 확인하고, 어기면 거부 사유를 붙여 한 번 다시
묻는다. 그래도 어기면 PlanRejected다. 근거는 활동을 막지 않는다 — 검증을 통과한 것만
evidence_record_ids에 남기고, 없으면 비운 채 계획한다.

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
from app.agent import reference_card, validation
from app.agent.context import AgentContext, PlannerContext
from app.agent.llm import with_fallback
from app.agent.prompts import (
    CHOOSE_SYSTEM_PROMPT,
    EXPLAIN_SYSTEM_PROMPT,
    build_choice_input,
    build_rationale_input,
)
from app.agent.state import CarePlan, CareState, NextStep, PlannedActivity
from app.retrieval import hybrid
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
    """LLM 호출 2의 구조화 출력."""

    rationale: str = Field(description="사용자에게 보여 줄 추천 이유 2~3문장(한국어)")


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
    ) -> Awaitable[str]: ...


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

    검수 ID · 단계 모듈 제외 · 목표 일치 · 순서 조건 · 시간 범위 · 환경 · 활동 변경 시 같은
    프로토콜 금지. 후보 조회(protocols.plan_candidates)가 같은 조건으로 거르지만, LLM이 목록 밖을
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
    if intake.minutes and activity.minutes > intake.minutes:
        problems.append(f"활동 시간 {activity.minutes}분이 가용 시간 {intake.minutes}분을 넘는다")
    rounds = state.get("rounds") or []
    if (
        rounds
        and rounds[-1].next_step is NextStep.CHANGE_ACTIVITY
        and rounds[-1].plan.activity.protocol_id == activity.protocol_id
    ):
        problems.append("활동 변경을 골랐는데 같은 프로토콜을 다시 넣었다")
    return problems


def excluded_protocols(state: CareState) -> tuple[str, ...]:
    """이번 라운드에 내놓지 않을 프로토콜 — 직전 라운드에서 바꾸기로 한 활동.

    check_plan의 '활동 변경 시 같은 프로토콜 금지'와 같은 규칙이다.
    """
    rounds = state.get("rounds") or []
    if rounds and rounds[-1].next_step is NextStep.CHANGE_ACTIVITY:
        return (rounds[-1].plan.activity.protocol_id,)
    return ()


def planner_context(context: AgentContext | None, state: CareState) -> PlannerContext:
    """그래프 컨텍스트에 이번 세션의 정해진 값(인테이크 · 이미지 선호 · 뺄 활동)을 더한 컨텍스트."""
    intake = state["intake"]
    return PlannerContext(
        db_pool=context.db_pool if context else None,
        goal=intake.goal,
        user_order=intake.order_condition,
        minutes_available=intake.minutes,
        environment=intake.environment,
        content_type=state["request"].profile.content_type,
        excluded_protocol_ids=excluded_protocols(state),
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
    """제공 매체마다 하이브리드 검색을 돌려 점수 순으로 합친다(상위 hybrid.TOP_K)."""
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
        )
        for hit in hits:
            known = found.get(hit.evidence_record_id)
            if known is None or hit.score > known.score:
                found[hit.evidence_record_id] = hit
    ranked = sorted(found.values(), key=lambda h: (-h.score, h.evidence_record_id))
    return ranked[: hybrid.TOP_K]


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
    """LLM 호출 2 — 검증된 근거로 추천 이유(rationale)를 쓴다."""
    structured = model.with_structured_output(PlanRationale, method="json_schema")

    async def explain(
        state: CareState,
        choice: ActivityChoice,
        protocol: CareProtocol,
        evidence: Sequence[CitedEvidence],
    ) -> str:
        messages = [
            SystemMessage(EXPLAIN_SYSTEM_PROMPT),
            HumanMessage(build_rationale_input(state, choice, protocol, evidence)),
        ]
        return (await structured.ainvoke(messages)).rationale

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
    ) -> str:
        return await self._explain(state, choice, protocol, evidence)


def make_plan_node(planner: Planner):
    async def plan(state: CareState, runtime: Runtime[AgentContext]) -> dict:
        # 단계마다 세션 API의 SSE progress(custom)를 낸다. 스트리밍이 아니면 아무 일도 하지 않는다
        progress = runtime.stream_writer
        session = planner_context(runtime.context, state)
        if session.goal is None:  # intake가 목표 없이는 세션을 열지 않는다
            raise PlanRejected("목표가 없어 계획할 수 없다")
        search = search_candidates(session)
        found = search.protocols
        if not found:
            raise PlanRejected(
                f"{session.goal.value} 목표 · 사용자 상태 {session.user_order} · "
                f"{session.minutes_available}분에 맞는 검수 프로토콜이 없다"
            )
        if search.relaxed:
            log.info("후보가 없어 %s 조건을 풀었다", " · ".join(search.relaxed))
        by_id = {p.protocol_id: p for p in found}
        candidates = [protocols.compact(p) for p in found]

        progress({"node": "plan", "step": "choose_activity"})
        problems: list[str] = []
        for _ in range(2):  # 처음 한 번 + 거부 사유를 붙여 한 번 더
            choice = await planner.choose(state, candidates, problems)
            protocol = by_id.get(choice.protocol_id)
            if protocol is None:
                problems = [f"{choice.protocol_id}는 후보 목록에 없다"]
                known = protocols.get_protocol(choice.protocol_id)
                if known is not None:  # 왜 후보가 아닌지도 알려 준다
                    minutes = protocols.planned_minutes(known, session.minutes_available)
                    activity = PlannedActivity(protocol_id=known.protocol_id, minutes=minutes)
                    problems += check_plan(activity, state, search.relaxed)
                continue
            minutes = protocols.planned_minutes(protocol, session.minutes_available)
            activity = PlannedActivity(protocol_id=protocol.protocol_id, minutes=minutes)
            problems = check_plan(activity, state, search.relaxed)
            if not problems:
                break
        else:
            raise PlanRejected("; ".join(problems), retryable=True)
        log.info(
            "계획 선택 %s %d분 (%s) — 질의 %r",
            activity.protocol_id,
            minutes,
            choice.reason,
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
        progress({"node": "plan", "step": "write_rationale"})
        rationale = await planner.explain(state, choice, protocol, evidence)
        activity = activity.model_copy(
            update={"evidence_record_ids": [c.evidence_record_id for c in evidence]}
        )
        plan = CarePlan(
            activity=activity,
            rationale=rationale,
            relaxed=list(search.relaxed),
            environment_gaps=protocols.environment_gaps(protocol, session.environment),
            reference_card=reference_card.build_card(
                state["intake"],
                protocol,
                activity,
                [(c.hit, c.verdict) for c in evidence],
                delivered=protocols.delivered_media(protocol, session.content_type),
                relaxed=search.relaxed,
            ),
        )
        return {"plan": plan}

    return plan
