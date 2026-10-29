"""Reference 카드 — "왜 추천했나요?"에 답한다(계약은 app/schemas/card.py).

plan 노드가 검증을 마친 근거(검색 결과 + 판정)와 프로토콜 · intake 결과로 만든다. LLM을 쓰지 않고
추천 이유(rationale) 문장에 기대지 않는다 — 카드의 주장은 모두 검색한 Evidence Record의 원문 구간과
구조화된 값에서 온다. 연구 참여자에게서 본 결과를 진단 · 치료 효과로 넓히지 않는다.

- 배지는 프로토콜(composition · user_facing_flags), 검증 플래그, 제공 매체(AI 생성 이미지), 조건
  완화(relaxed), 목표를 정한 방식(DEFAULT), 목표 척도의 상태(PROVISIONAL)에서 정한다.
- 한계 문장은 배지마다 고정 문구다. 프로토콜 작성자가 카드용으로 적은 reference_card_note가 있으면
  그 배지의 고정 문구 대신 쓴다 — content_spec의 메모는 각색 대본(ADAPTED_STIMULUS), 슬롯 생성
  설정의 메모는 AI 생성 이미지(AI_IMAGE_UNVERIFIED)에 대한 것이다.
- 근거는 같은 실험의 chunk(METHODS · RESULTS)를 하나로 묶는다 — 실험 ID가 없는 리뷰 · 메타분석은
  논문 단위로. 목표 근거(개념 정의)의 논문 정보는 data/manifest.csv에서 찾는다.
- 카드의 ADAPTED_STIMULUS 배지는 프로토콜이 각색을 밝혔거나, 목표를 지지하는 근거가 모두 다른
  자극일 때만 단다. 지지 근거 가운데 하나라도 제공 콘텐츠와 같은 자극이면 근거마다의 한계 문장으로만
  밝힌다(예: 사진 실험과, 사진 · 영상 · 산책을 모은 메타분석).
"""

import csv
from collections.abc import Iterable, Sequence
from functools import lru_cache
from pathlib import Path

from app import instruments, intake_cues
from app.agent.state import IntakeResult, PlannedActivity
from app.schemas.card import (
    CardBadge,
    CardEvidence,
    Excerpt,
    GoalDefinitionRef,
    GoalSection,
    MeasureRef,
    PaperCitation,
    ReferenceCard,
)
from app.schemas.care import CareGoal
from app.schemas.evidence import Medium, Section
from app.schemas.intake import GoalSource
from app.schemas.measure import GOAL_SCALE, InstrumentId
from app.schemas.protocol import CareProtocol, Composition
from app.schemas.retrieval import GOAL_OUTCOMES, EvidenceHit, EvidenceVerdict, Flag, PaperRef

MANIFEST = Path(__file__).resolve().parents[2] / "data" / "manifest.csv"

GOAL_LABELS: dict[CareGoal, str] = {
    CareGoal.S1: "반추 완화",
    CareGoal.S2: "긴장·스트레스 완화",
    CareGoal.S3: "주의·피로 회복",
}

GENERAL_LIMIT = "연구 참여자에게서 관찰된 결과이며, 진단이나 치료 효과를 뜻하지 않습니다."

BADGE_LIMITS: dict[CardBadge, str] = {
    CardBadge.EXPERIMENTAL: (
        "여러 활동을 묶은 실험적 구성이라, 함께 했을 때의 효과는 검증되지 않았습니다."
    ),
    CardBadge.LIMITED_EVIDENCE: "이 활동을 뒷받침하는 연구가 아직 적습니다.",
    CardBadge.NO_VERIFIED_EVIDENCE: (
        "이번에는 이 활동을 직접 뒷받침하는 검증된 연구를 찾지 못했습니다."
    ),
    CardBadge.CONFLICTING_EVIDENCE: (
        "같은 활동 · 지표에서 효과가 없거나 반대로 나온 연구도 있습니다."
    ),
    CardBadge.ADAPTED_STIMULUS: (
        "연구에서 쓴 자극을 LUMI에 맞게 바꾼 콘텐츠라, 같은 효과가 나는지는 확인되지 않았습니다."
    ),
    CardBadge.AI_IMAGE_UNVERIFIED: (
        "AI로 만든 이미지는 연구의 자연 사진과 효과가 같은지 검증되지 않았습니다."
    ),
    CardBadge.RELAXED_ORDER: (
        "지금 상황에 맞춘 활동이 없어, 다른 상황을 위해 만든 활동을 골랐습니다."
    ),
    CardBadge.RELAXED_ENVIRONMENT: (
        "지금 있는 곳에 딱 맞는 활동이 없어, 환경이 맞지 않을 수 있는 활동을 골랐습니다."
    ),
}

FLAG_LIMITS: dict[Flag, str] = {
    Flag.POPULATION_MISMATCH: "임상 진단을 받은 사람들을 대상으로 한 연구라 참고로만 인용합니다.",
    Flag.CONFLICTING_EVIDENCE: "같은 활동 · 지표에서 효과가 없거나 반대로 나온 연구가 있습니다.",
    Flag.ADAPTED_STIMULUS: "LUMI 콘텐츠는 이 연구의 자극과 다릅니다.",
    Flag.NOT_SUPPORTING: "목표 지표의 개선은 보이지 않아 참고로만 인용합니다.",
}

# 근거의 1차 지표와 LUMI가 재는 것이 다른 목표 — 카드에 밝힌다(schemas/measure.GOAL_SCALE)
MEASURE_GAPS: dict[CareGoal, str] = {
    CareGoal.S3: (
        "근거 연구는 주의 과제 성적을 봤지만, LUMI는 주의 용량을 재지 않고 지금 느끼는 정신 피로로 "
        "변화를 봅니다."
    ),
}

# 목표 척도가 PROVISIONAL일 때 밝히는 문장. 원척도가 검증된 번역 초안(STAI-6 · BSRI)과 팀이 만든
# 문항(MFVAS)은 사정이 달라 척도마다 둔다. 척도 이름은 measured_by에 보이므로 문장에 넣지 않는다
TRANSLATED_DRAFT_NOTE = (
    "이 척도는 연구에서 타당성이 확인된 영어판 문항을 LUMI가 한국어로 옮긴 것입니다. 번역본은 아직 "
    "따로 검증하지 않아, 점수 변화는 참고로 봐 주세요."
)
PROVISIONAL_NOTES: dict[InstrumentId, str] = {
    InstrumentId.STAI6: TRANSLATED_DRAFT_NOTE,
    InstrumentId.BSRI: TRANSLATED_DRAFT_NOTE,
    InstrumentId.MFVAS: (
        "이 척도는 지금 느끼는 정신 피로를 보려고 LUMI가 만든 질문이라, 아직 검증되지 않았습니다. "
        "점수 변화는 참고로 봐 주세요."
    ),
}

_ADAPTED = "ADAPTED_STIMULUS"
_EXPERIMENTAL = "EXPERIMENTAL"
_SECTION_ORDER = {section: i for i, section in enumerate(Section)}


@lru_cache
def manifest() -> dict[str, PaperCitation]:
    """data/manifest.csv — 논문 ID → 출처."""
    with MANIFEST.open(encoding="utf-8", newline="") as f:
        return {row["rc_id"]: _manifest_citation(row) for row in csv.DictReader(f)}


def _manifest_citation(row: dict[str, str]) -> PaperCitation:
    year = row.get("year") or ""
    return PaperCitation(
        paper_id=row["rc_id"],
        title=row["title"],
        authors=row.get("authors") or None,
        year=int(year) if year.isdigit() else None,
        doi=row.get("doi") or None,
        url=row.get("url") or None,
    )


def citation(paper: PaperRef) -> PaperCitation:
    url = paper.url or (f"https://doi.org/{paper.doi}" if paper.doi else None)
    return PaperCitation(
        paper_id=paper.paper_id,
        title=paper.title,
        authors=paper.authors,
        year=paper.year,
        doi=paper.doi,
        url=url,
    )


def build_card(
    intake: IntakeResult,
    protocol: CareProtocol,
    activity: PlannedActivity,
    cited: Sequence[tuple[EvidenceHit, EvidenceVerdict]],
    *,
    delivered: Sequence[Medium],
    relaxed: Iterable[str] = (),
) -> ReferenceCard:
    """계획 하나의 카드. cited는 plan 노드가 인용하기로 한 (검색 결과, 판정) — 목표 지지가 앞."""
    goal = intake.goal
    if goal is None:  # plan 노드는 목표 없이 계획하지 않는다
        raise ValueError("목표 없이 Reference 카드를 만들 수 없다")
    evidence = card_evidence(cited)
    goal_section = goal_section_for(intake, goal)
    badges = card_badges(protocol, goal, evidence, delivered, relaxed, goal_section)
    notes = protocol_notes(protocol, delivered)
    limits = [GENERAL_LIMIT]
    for badge in badges:
        note = notes.get(badge)
        if note or badge in BADGE_LIMITS:
            limits.append(note or BADGE_LIMITS[badge])
    if protocol.population_note:
        limits.append(protocol.population_note)
    return ReferenceCard(
        protocol_id=protocol.protocol_id,
        name=protocol.name,
        minutes=activity.minutes,
        composition=protocol.composition,
        badges=badges,
        goal=goal_section,
        grades={
            outcome: protocol.grades[outcome].grade
            for outcome in GOAL_OUTCOMES[goal].primary
            if outcome in protocol.grades
        },
        evidence=evidence,
        limits=list(dict.fromkeys(limits)),
    )


def card_evidence(cited: Sequence[tuple[EvidenceHit, EvidenceVerdict]]) -> list[CardEvidence]:
    """같은 실험의 chunk를 하나로 묶는다(실험 ID가 없으면 논문 단위). 순서는 처음 나온 순서."""
    groups: dict[str, list[tuple[EvidenceHit, EvidenceVerdict]]] = {}
    for hit, verdict in cited:
        groups.setdefault(hit.experiment_id or hit.paper.paper_id, []).append((hit, verdict))
    out = []
    for items in groups.values():
        hit, verdict = items[0]
        meta = hit.metadata
        flags = list(dict.fromkeys(f for _, v in items for f in v.flags))
        supported = list(dict.fromkeys(o for _, v in items for o in v.supported_outcomes))
        chunks = sorted(
            (h for h, _ in items), key=lambda h: (_SECTION_ORDER[h.section], h.chunk_idx)
        )
        out.append(
            CardEvidence(
                paper=citation(hit.paper),
                experiment_id=hit.experiment_id,
                arm_label=meta.arm_label,
                theories=meta.theories,
                study_design=meta.study_design,
                population=meta.population_detail,
                n=meta.n,
                n_detail=meta.n_detail,
                exposure_minutes=meta.exposure_minutes,
                regimen=meta.regimen,
                studied_orders=meta.intervention_orders,
                results=verdict.goal_results,
                supports_goal=any(v.supports_goal for _, v in items),
                supported_outcomes=supported,
                flags=flags,
                limits=[FLAG_LIMITS[f] for f in flags if f in FLAG_LIMITS],
                excerpts=[
                    Excerpt(
                        evidence_record_id=h.evidence_record_id, section=h.section, text=h.excerpt
                    )
                    for h in chunks
                ],
                checks=verdict.checks,
                evidence_record_ids=[h.evidence_record_id for h in chunks],
            )
        )
    return out


def goal_section_for(intake: IntakeResult, goal: CareGoal) -> GoalSection:
    label = GOAL_LABELS[goal]
    notes = []
    if intake.goal_source is GoalSource.DEFAULT:
        notes.append(f"상황 단서가 부족해 기본 목표({label})로 시작했습니다.")
    if goal in MEASURE_GAPS:
        notes.append(MEASURE_GAPS[goal])
    measure = None
    if (scale := GOAL_SCALE[goal]) is not None:
        instrument = instruments.get_instrument(scale)
        measure = MeasureRef(
            instrument_id=scale,
            name=instrument.name,
            concept=instrument.concept,
            provisional=instrument.status == "PROVISIONAL",
        )
        if measure.provisional:
            notes.append(PROVISIONAL_NOTES[scale])
    return GoalSection(
        goal=goal,
        label=label,
        secondary_goal=intake.secondary_goal,
        source=intake.goal_source,
        reason=intake.goal_reason,
        definitions=goal_definitions(intake),
        evidence_outcomes=list(GOAL_OUTCOMES[goal].primary),
        measured_by=measure,
        notes=notes,
    )


def goal_definitions(intake: IntakeResult) -> list[GoalDefinitionRef]:
    """intake가 붙인 개념 정의(goal_evidence)에 개념 · 쪽 · 논문 출처를 단다."""
    goals = [g for g in (intake.goal, intake.secondary_goal) if g is not None]
    known = {
        (d.paper_id, d.sentence): (goal, d)
        for goal in goals
        for d in intake_cues.definitions_for(goal)
    }
    papers = manifest()
    out = []
    for evidence in intake.goal_evidence:
        goal, definition = known.get((evidence.paper_id, evidence.sentence), (intake.goal, None))
        out.append(
            GoalDefinitionRef(
                goal=goal,
                concept=definition.concept if definition else None,
                page=definition.page if definition else None,
                sentence=evidence.sentence,
                paper=papers.get(evidence.paper_id),
            )
        )
    return out


def adapted_protocol(protocol: CareProtocol) -> bool:
    """활동 노드가 연구 자극을 LUMI에 맞게 바꿨다고 표시했다(각색 대본 · 다른 음원 — PR-06 ·
    07 …). 근거 플래그와 따로 ADAPTED_STIMULUS 배지를 켠다. 추천 이유 입력의 '꼭 밝힐 것'도 이
    판정을 쓴다."""
    flags = protocol.user_facing_flags
    return _ADAPTED in (flags.get("when_adapted_script"), flags.get("asset_fidelity"))


def card_badges(
    protocol: CareProtocol,
    goal: CareGoal,
    evidence: Sequence[CardEvidence],
    delivered: Sequence[Medium],
    relaxed: Iterable[str],
    goal_section: GoalSection,
) -> list[CardBadge]:
    """배지 — CardBadge 선언 순서로."""
    flags = protocol.user_facing_flags
    by_goal = flags.get("by_goal") or {}
    ai_image = Medium.AI_IMAGE in delivered
    # 지지 근거가 모두 다른 자극일 때만(없으면 인용한 근거 전부로 본다). 생성 이미지 때문에 붙은
    # 플래그는 AI_IMAGE_UNVERIFIED가 대신 밝힌다
    basis = [e for e in evidence if e.supports_goal] or list(evidence)
    adapted_evidence = (
        not ai_image and bool(basis) and all(Flag.ADAPTED_STIMULUS in e.flags for e in basis)
    )
    relaxed = set(relaxed)
    on = {
        CardBadge.EXPERIMENTAL: protocol.composition is Composition.EXPERIMENTAL
        or flags.get("default") == _EXPERIMENTAL
        or by_goal.get(goal.value) == _EXPERIMENTAL,
        CardBadge.LIMITED_EVIDENCE: flags.get("default") == "LIMITED_EVIDENCE",
        CardBadge.NO_VERIFIED_EVIDENCE: not evidence,
        CardBadge.CONFLICTING_EVIDENCE: any(Flag.CONFLICTING_EVIDENCE in e.flags for e in evidence),
        CardBadge.ADAPTED_STIMULUS: adapted_protocol(protocol) or adapted_evidence,
        CardBadge.AI_IMAGE_UNVERIFIED: ai_image,
        CardBadge.RELAXED_ORDER: "order" in relaxed,
        CardBadge.RELAXED_ENVIRONMENT: "environment" in relaxed,
        CardBadge.DEFAULT_GOAL: goal_section.source is GoalSource.DEFAULT,
        CardBadge.PROVISIONAL_MEASURE: bool(
            goal_section.measured_by and goal_section.measured_by.provisional
        ),
    }
    return [badge for badge in CardBadge if on[badge]]


def protocol_notes(protocol: CareProtocol, delivered: Sequence[Medium]) -> dict[CardBadge, str]:
    """프로토콜 작성자가 카드용으로 적은 메모(reference_card_note) — 배지 고정 문구 대신 쓴다."""
    spec = protocol.content_spec.model_dump()
    notes: dict[CardBadge, str] = {}
    if note := spec.get("reference_card_note"):
        notes[CardBadge.ADAPTED_STIMULUS] = note
    if Medium.AI_IMAGE in delivered:
        for slot in spec.get("slots") or []:
            if note := (slot.get("generation") or {}).get("reference_card_note"):
                notes[CardBadge.AI_IMAGE_UNVERIFIED] = note
                break
    return notes
