"""EvidenceValidationTool — 검색한 근거를 세션에 쓰기 전에 따로 검증한다.

| 검사 | Check | 실패 시 |
| --- | --- | --- |
| 출처 — 원문(DOI·URL + 파일)에서 온 chunk인가 | SOURCE_EXISTS | 차단 |
| 중재 — 제공 구성이 연구 매체·사용자 상태에 맞는가 | INTERVENTION_MATCH | 차단 |
| 대상 — 임상 표본은 아닌가 | POPULATION_FIT | 플래그 POPULATION_MISMATCH |
| 주장 — 목표의 1차 지표를 측정했는가 | OUTCOME_MEASURED | 차단 |
| 상충 — 같은 매체·지표에 비유의·반대 arm | CONFLICT_SCAN | 플래그 CONFLICTING_EVIDENCE |
| 자극 — 연구 자극과 같은 구성인가 | STIMULUS_FIDELITY | 플래그 ADAPTED_STIMULUS |

검증은 콘텐츠를 막지 않는다. AI 생성 이미지·조합 세션은 인용할 수 있되 ADAPTED_STIMULUS로
연구 자극과의 차이를 Reference 카드에 밝힌다. 활동을 여러 개 결합한 세션은 개별 연구가 있어도
통합 효과를 '미검증'으로 표시한다.

judge()는 DB 없는 순수 규칙이다. validate()가 evidence_records에서 레코드와 상충 후보를
읽어 judge()를 부른다. plan 노드(nodes/planner.verify_evidence)가 validate()를 부른다.
"""

import logging
from collections.abc import Iterable, Sequence

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from app.protocols import get_protocol
from app.retrieval.hybrid import to_hit
from app.schemas.care import CareGoal
from app.schemas.evidence import (
    STUDY_MEDIUM_FOR,
    ClinicalStatus,
    Effect,
    FulltextStatus,
    InterventionOrder,
    Medium,
    RecordType,
    Verification,
    study_medium,
)
from app.schemas.retrieval import (
    BLOCKING_CHECKS,
    GOAL_OUTCOMES,
    ORDER_COMPATIBILITY,
    Check,
    CheckResult,
    EvidenceHit,
    EvidenceVerdict,
    Flag,
    MediaMatch,
)

log = logging.getLogger(__name__)

_CONFLICT_EFFECTS = frozenset({Effect.NOT_SIGNIFICANT, Effect.NEGATIVE})
_CONFLICT_TYPES = frozenset({RecordType.ARM, RecordType.SYNTHESIS})

# 레코드 + 논문. 열은 hybrid.to_hit이 읽는 것이다
_HITS_SQL = """
select e.id, e.paper_id, e.experiment_id, e.section, e.chunk_idx, e.content, e.metadata,
       p.title, p.authors, p.year, p.doi, p.url, p.fulltext_status
from public.evidence_records e
join public.papers p on p.id = e.paper_id
"""
RECORDS_SQL = _HITS_SQL + "where e.id = any(%(ids)s::bigint[])"
# 상충 후보 — judge()의 상충 규칙(종류 · 매체가 겹침 · 1차 지표의 비유의·반대 결과)을
# DB에서 먼저 건다. id 순서라 arm마다 첫 chunk가 related_record_ids에 들어간다
CANDIDATES_SQL = (
    _HITS_SQL
    + """where e.metadata @> any(%(types)s::jsonb[])
  and e.metadata @> any(%(media)s::jsonb[])
  and e.metadata @> any(%(results)s::jsonb[])
order by e.id
"""
)


def media_match(delivered: Iterable[Medium], studied: Iterable[Medium]) -> MediaMatch:
    """제공 구성(AI_IMAGE는 STATIC_IMAGE로 비교)과 연구 매체 집합을 비교한다."""
    want = {study_medium(m) for m in delivered}
    have = set(studied)
    if not want or not have:
        return MediaMatch.NONE
    if want == have:
        return MediaMatch.EXACT
    return MediaMatch.PARTIAL if want & have else MediaMatch.NONE


def _source_check(hit: EvidenceHit) -> CheckResult:
    paper, verification = hit.paper, hit.metadata.verification
    locator = paper.doi or paper.url
    if verification is Verification.SECONDARY:
        passed, reason = False, "2차 자료에서 옮긴 내용이라 원문 출처로 쓸 수 없다"
    elif not locator:
        passed, reason = False, f"{paper.paper_id}에 DOI·URL이 없다"
    elif paper.fulltext_status in (None, FulltextStatus.MISSING):
        passed, reason = False, f"{paper.paper_id} 원문 파일 미확보({paper.fulltext_status})"
    elif (
        paper.fulltext_status is FulltextStatus.ABSTRACT_ONLY
        and verification is not Verification.ABSTRACT
    ):
        passed, reason = False, f"{paper.paper_id}는 초록만 확보했는데 본문 chunk로 표시됐다"
    else:
        passed, reason = True, f"{paper.paper_id} {locator} · {verification}"
    return CheckResult(check=Check.SOURCE_EXISTS, passed=passed, reason=reason)


def _order_check(
    orders: Sequence[InterventionOrder], studied: Sequence[InterventionOrder]
) -> tuple[bool, str]:
    if not orders:
        return True, "순서 미지정(매체만 검사)"
    if not studied:
        return True, "연구 조건 미상"
    allowed = frozenset().union(*(ORDER_COMPATIBILITY[o] for o in orders))
    names = [o.value for o in studied]
    if allowed & set(studied):
        return True, f"연구 조건 {names}을 {[o.value for o in orders]}에 인용 가능"
    return False, f"연구 조건 {names}은 {[o.value for o in orders]}에 맞지 않는다"


def missing_verdict(evidence_record_id: int) -> EvidenceVerdict:
    """없는 ID. 출처 검사만 실패이고 나머지는 평가하지 않았다(None)."""
    checks = [
        CheckResult(
            check=check,
            passed=False if check is Check.SOURCE_EXISTS else None,
            reason="레코드가 없다"
            if check is Check.SOURCE_EXISTS
            else "레코드가 없어 평가하지 않았다",
        )
        for check in Check
    ]
    return EvidenceVerdict(
        evidence_record_id=evidence_record_id,
        usable=False,
        supports_goal=False,
        media_match=MediaMatch.NONE,
        checks=checks,
    )


def judge(
    hit: EvidenceHit,
    *,
    goal: CareGoal,
    media: Sequence[Medium],
    orders: Sequence[InterventionOrder] = (),
    candidates: Sequence[EvidenceHit] = (),
) -> EvidenceVerdict:
    """레코드 하나의 판정. orders는 사용자 상태 또는 프로토콜 허용 시점(비면 매체만 검사),
    candidates는 상충 후보 레코드(보통 같은 연구 매체·지표로 DB에서 추린 것). 순수 함수."""
    meta = hit.metadata
    goal_outcomes = GOAL_OUTCOMES[goal]
    primary = set(goal_outcomes.primary)
    results = meta.results

    source = _source_check(hit)

    match = media_match(media, meta.media)
    order_ok, order_reason = _order_check(orders, meta.intervention_orders)
    delivered = [m.value for m in media]
    studied = [m.value for m in meta.media]
    intervention = CheckResult(
        check=Check.INTERVENTION_MATCH,
        passed=match is not MediaMatch.NONE and order_ok,
        reason=f"제공 {delivered} vs 연구 {studied} → {match.value}; {order_reason}",
    )

    clinical = meta.clinical_status
    population = CheckResult(
        check=Check.POPULATION_FIT,
        passed=clinical is not ClinicalStatus.CLINICAL,
        reason=(
            "임상 여부 미상"
            if clinical is None
            else f"{clinical.value}"
            + (f" ({meta.population_detail})" if meta.population_detail else "")
        ),
    )

    measured = {r.outcome for r in results}
    hit_primary = sorted(o.value for o in primary & measured)
    outcome = CheckResult(
        check=Check.OUTCOME_MEASURED,
        passed=bool(hit_primary),
        reason=(
            f"{goal.value} 1차 지표 측정: {hit_primary}"
            if hit_primary
            else f"{goal.value} 1차 지표 {[o.value for o in goal_outcomes.primary]} 미측정"
            f" (측정: {sorted(o.value for o in measured)})"
        ),
    )

    own_arm = (hit.paper.paper_id, hit.experiment_id)
    conflicts: dict[tuple[str, str | None], int] = {}
    for other in candidates:
        arm = (other.paper.paper_id, other.experiment_id)
        if arm == own_arm or arm in conflicts:
            continue
        if other.metadata.record_type not in _CONFLICT_TYPES:
            continue
        if media_match(media, other.metadata.media) is MediaMatch.NONE:
            continue
        if any(
            r.outcome in primary and r.effect in _CONFLICT_EFFECTS for r in other.metadata.results
        ):
            conflicts[arm] = other.evidence_record_id
    conflict = CheckResult(
        check=Check.CONFLICT_SCAN,
        passed=not conflicts,
        reason=(
            "같은 매체·1차 지표에서 비유의·반대 결과: "
            + ", ".join(exp or pid for pid, exp in conflicts)
            if conflicts
            else "상충 후보 없음"
        ),
        related_record_ids=list(conflicts.values()),
    )

    adapted = [m.value for m in media if m in STUDY_MEDIUM_FOR]
    if adapted:
        fidelity_reason = f"{adapted}는 생성·변형 자극(연구는 {studied})"
    elif match is MediaMatch.EXACT:
        fidelity_reason = "연구 자극과 같은 구성"
    else:
        fidelity_reason = f"제공 구성 {delivered}이 연구 자극 {studied}과 다르다"
    fidelity = CheckResult(
        check=Check.STIMULUS_FIDELITY,
        passed=match is MediaMatch.EXACT and not adapted,
        reason=fidelity_reason,
    )

    checks = [source, intervention, population, outcome, conflict, fidelity]
    usable = all(c.passed is True for c in checks if c.check in BLOCKING_CHECKS)
    supported = [
        o
        for o in goal_outcomes.primary
        if any(r.outcome is o and r.effect is Effect.POSITIVE for r in results)
    ]
    flags = []
    if not population.passed:
        flags.append(Flag.POPULATION_MISMATCH)
    if conflicts:
        flags.append(Flag.CONFLICTING_EVIDENCE)
    if not fidelity.passed:
        flags.append(Flag.ADAPTED_STIMULUS)
    if usable and not supported:
        flags.append(Flag.NOT_SUPPORTING)
    relevant = primary | set(goal_outcomes.secondary)
    return EvidenceVerdict(
        evidence_record_id=hit.evidence_record_id,
        usable=usable,
        supports_goal=bool(supported),
        supported_outcomes=supported,
        media_match=match,
        flags=flags,
        checks=checks,
        goal_results=[r for r in results if r.outcome in relevant],
    )


def resolve_orders(
    user_order: InterventionOrder | None, protocol_id: str | None
) -> list[InterventionOrder]:
    """검사할 사용자 상태. user_order, 없으면 프로토콜의 허용 시점, 둘 다 없으면 [](매체만 검사)."""
    if user_order:
        return [user_order]
    protocol = get_protocol(protocol_id) if protocol_id else None
    return list(protocol.intervention_order) if protocol else []


def conflict_params(goal: CareGoal, media: Sequence[Medium]) -> dict[str, list[dict]]:
    """CANDIDATES_SQL 인자. 목록마다 하나라도 `metadata @>`이면 통과, 매체는 연구 매체로 바꾼다."""
    return {
        "types": [{"record_type": t.value} for t in sorted(_CONFLICT_TYPES)],
        "media": [{"media": [m.value]} for m in dict.fromkeys(study_medium(m) for m in media)],
        "results": [
            {"results": [{"outcome": o.value, "effect": e.value}]}
            for o in GOAL_OUTCOMES[goal].primary
            for e in sorted(_CONFLICT_EFFECTS)
        ],
    }


async def validate(
    pool: AsyncConnectionPool | None,
    *,
    evidence_record_ids: Sequence[int],
    goal: CareGoal,
    media: Sequence[Medium],
    protocol_id: str | None = None,
    user_order: InterventionOrder | None = None,
) -> list[EvidenceVerdict]:
    """레코드·논문·상충 후보를 DB에서 읽어 judge()를 부른다. 입력 ID 순서대로 돌려준다.

    orders = [user_order], 없으면 protocol_id의 허용 시점(intervention_order), 둘 다 없으면 [].
    상충 후보 = 제공 매체(연구 매체로 바꿔)와 겹치고 goal 1차 지표에 NOT_SIGNIFICANT·NEGATIVE 결과가
    있는 ARM·SYNTHESIS 레코드. 없는 ID는 missing_verdict다. 풀이 없으면(DATABASE_URL 미설정)
    출처를 확인할 수 없어 전부 missing_verdict다.
    """
    ids = list(evidence_record_ids)
    if not ids:
        return []
    if pool is None:
        log.warning("DB 풀이 없어(DATABASE_URL 미설정) 근거를 검증하지 못했다: %s", ids)
        return [missing_verdict(i) for i in ids]
    orders = resolve_orders(user_order, protocol_id)
    filters = {
        name: [Jsonb(value) for value in values]
        for name, values in conflict_params(goal, media).items()
    }
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(RECORDS_SQL, {"ids": ids})
        hits = {row["id"]: to_hit(row) for row in await cur.fetchall()}
        await cur.execute(CANDIDATES_SQL, filters)
        candidates = [to_hit(row) for row in await cur.fetchall()]
    verdicts = [
        judge(hits[i], goal=goal, media=media, orders=orders, candidates=candidates)
        if i in hits
        else missing_verdict(i)
        for i in ids
    ]
    log.info(
        "근거 검증 %s media=%s orders=%s → %s",
        goal.value,
        [m.value for m in media],
        [o.value for o in orders],
        [
            f"{v.evidence_record_id} {'usable' if v.usable else 'blocked'}"
            f"{[f.value for f in v.flags]}"
            for v in verdicts
        ],
    )
    return verdicts
