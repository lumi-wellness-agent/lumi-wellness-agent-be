"""계약 2 — EvidenceMetadata. 시트·JSON의 실제 라벨이 전부 변환되는지와 레코드 규칙을 확인한다.

라벨 상수는 LUMI_측정도구_프로토콜_2026-10-23.xlsx(근거 arm · 프로토콜)와
LUMI_R_C_논문목록_2026-10-22.xlsx('논문 유형 기준')의 distinct 값이다.
시트가 바뀌면 여기도 갱신한다.
"""

import pytest
from pydantic import ValidationError

from app.schemas.evidence import (
    DESIGN_LABELS,
    GRADED_OUTCOMES,
    OUTCOME_LABELS,
    STUDY_MEDIUM_FOR,
    VERIFICATION_LABELS,
    Effect,
    EvidenceMetadata,
    EvidenceRecordIn,
    ExtractedFields,
    InterventionOrder,
    Medium,
    Outcome,
    OutcomeResult,
    RecordType,
    Section,
    StudyDesign,
    Verification,
    parse_media,
    parse_orders,
    study_medium,
)

NO = InterventionOrder.NO_INDUCTION
POST_S, POST_F, PRE_S = (
    InterventionOrder.POST_STRESS,
    InterventionOrder.POST_FATIGUE,
    InterventionOrder.PRE_STRESS,
)

MASTER_DESIGN_LABELS = [
    "이론·개념 리뷰",
    "체계적 고찰·메타분석",
    "척도·측정도구 개발/검증",
    "무작위 실험",
    "준실험·비무작위 비교",
    "관찰·설문·질적 연구",
]
ARM_SHEET_DESIGN_LABELS = [
    "관찰·설문·질적",
    "무작위 실험",
    "준실험·비무작위 비교",
    "체계적 고찰·메타분석",
]
ORDER_LABELS_TO_ENUMS = {
    # 근거 arm 시트 '순서'
    "POST_FATIGUE": [POST_F],
    "POST_FATIGUE / 무관": [POST_F, NO],  # P07 요인 설계
    "POST_STRESS": [POST_S],
    "POST_STRESS(반추 유도)": [POST_S],
    "PRE_STRESS": [PRE_S],
    "—": [],
    "과제 전후": [],  # N16 — 시점 미상
    "무관": [NO],
    "무관(일상)": [NO],
    # 프로토콜 시트 '순서 조건(intervention_order)'
    "POST_STRESS(P24·P33) / POST_FATIGUE(P03·P05-E2)": [POST_S, POST_F],
    "POST_STRESS(N14) / 과제 전후(N16)": [POST_S],
    "PRE_STRESS(E09) / POST_STRESS(E08 포함 연구)": [PRE_S, POST_S],
    "무관(일상 반복)": [NO],
    "무관(일상 중 반복)": [NO],
    "POST_FATIGUE(P07 절반·P05) / 무관": [POST_F, NO],
    "POST_STRESS(불안 유발 후)": [POST_S],
}
MEDIA_LABELS_TO_ENUMS = {  # 프로토콜 시트 '매체(BE Medium)'
    "STATIC_IMAGE": [Medium.STATIC_IMAGE],
    "VIDEO + NATURE_SOUND": [Medium.VIDEO, Medium.NATURE_SOUND],
    "NATURE_SOUND": [Medium.NATURE_SOUND],
    "MUSIC (enum 추가 필요)": [Medium.MUSIC],
    "BREATHING": [Medium.BREATHING],
    "PMR": [Medium.PMR],
    "GUIDED_IMAGERY (enum 추가 필요)": [Medium.GUIDED_IMAGERY],
    "MINDFULNESS": [Medium.MINDFULNESS],
    "OUTDOOR_WALK + MINDFULNESS": [Medium.OUTDOOR_WALK, Medium.MINDFULNESS],
    "OUTDOOR_WALK": [Medium.OUTDOOR_WALK],
    "OUTDOOR_WALK + NOTICE": [Medium.OUTDOOR_WALK, Medium.NOTICING],
    "COLORING": [Medium.COLORING],
    "— (단계)": [],  # PR-16 단계 모듈
    "GROUNDING (enum 추가 필요)": [Medium.GROUNDING],
    "WRITING (enum 추가 필요)": [Medium.WRITING],
    "ART_VIEWING (enum 추가 필요)": [Medium.ART_VIEWING],
    "STATIC_IMAGE + MINDFULNESS": [Medium.STATIC_IMAGE, Medium.MINDFULNESS],
    "STATIC_IMAGE + BREATHING": [Medium.STATIC_IMAGE, Medium.BREATHING],
}
VERIFICATION_SHEET_LABELS = ["본문", "본문(일부)", "초록", "2차"]


def test_master_design_labels_cover_all_six_designs():
    assert {DESIGN_LABELS[label] for label in MASTER_DESIGN_LABELS} == set(StudyDesign)


@pytest.mark.parametrize("label", ARM_SHEET_DESIGN_LABELS + ["RCT", "META_ANALYSIS"])
def test_short_design_labels(label):
    assert label in DESIGN_LABELS


@pytest.mark.parametrize(("label", "expected"), ORDER_LABELS_TO_ENUMS.items())
def test_parse_orders(label, expected):
    assert parse_orders(label) == expected


def test_parse_orders_accepts_lists_and_none():
    assert parse_orders(None) == []
    assert parse_orders(["POST_STRESS", "POST_FATIGUE", "POST_STRESS"]) == [POST_S, POST_F]


def test_parse_orders_rejects_unknown_label():
    with pytest.raises(ValueError, match="모르는 순서 라벨"):
        parse_orders("DURING_STRESS")


@pytest.mark.parametrize(("label", "expected"), MEDIA_LABELS_TO_ENUMS.items())
def test_parse_media(label, expected):
    assert parse_media(label) == expected


def test_parse_media_rejects_unknown_label():
    with pytest.raises(ValueError, match="모르는 매체 라벨"):
        parse_media("PODCAST")


@pytest.mark.parametrize("label", VERIFICATION_SHEET_LABELS)
def test_verification_labels(label):
    assert VERIFICATION_LABELS[label] in Verification


def test_graded_outcomes_match_grade_rule_sheet():
    assert set(OUTCOME_LABELS.values()) == set(GRADED_OUTCOMES)
    assert set(GRADED_OUTCOMES) == set(Outcome) - {Outcome.OTHER}


def test_study_medium_maps_provided_only_media():
    assert study_medium(Medium.AI_IMAGE) is Medium.STATIC_IMAGE
    assert study_medium(Medium.VIDEO) is Medium.VIDEO
    assert set(STUDY_MEDIUM_FOR) == {Medium.AI_IMAGE}


def _arm(**overrides) -> EvidenceMetadata:
    base = dict(
        record_type=RecordType.ARM,
        study_design=StudyDesign.RANDOMIZED_EXPERIMENT,
        verification=Verification.FULL_TEXT,
        media=[Medium.STATIC_IMAGE],
        intervention_orders=[POST_S],
    )
    return EvidenceMetadata(**{**base, **overrides})


def test_outcome_result_keeps_comparison_and_timepoint_and_strips_blanks():
    result = OutcomeResult(
        outcome=Outcome.PHYSIO,
        effect=Effect.NOT_SIGNIFICANT,
        measure=" HF-HRV ",
        comparison="VR+소리 vs VR vs 대조",
        timepoint="회복기",
        stat="",
    )
    assert result.measure == "HF-HRV" and result.stat is None


def test_other_outcome_requires_measure():
    with pytest.raises(ValidationError, match="OTHER 지표는 measure"):
        OutcomeResult(outcome=Outcome.OTHER, effect=Effect.POSITIVE, measure="   ")


def test_same_outcome_may_repeat_per_measure():
    meta = _arm(
        results=[
            OutcomeResult(outcome=Outcome.PHYSIO, effect=Effect.POSITIVE, measure="SCL"),
            OutcomeResult(outcome=Outcome.PHYSIO, effect=Effect.NOT_SIGNIFICANT, measure="HF-HRV"),
        ]
    )
    assert [r.effect for r in meta.results] == [Effect.POSITIVE, Effect.NOT_SIGNIFICANT]


def test_record_type_must_match_design():
    with pytest.raises(ValidationError, match="맞지 않는다"):
        _arm(record_type=RecordType.SYNTHESIS)


def test_study_media_cannot_contain_provided_only_media():
    with pytest.raises(ValidationError, match="제공 전용 매체"):
        _arm(media=[Medium.AI_IMAGE])


@pytest.mark.parametrize("field, value", [("n", 0), ("n_studies", 0), ("exposure_minutes", 0)])
def test_numeric_ranges(field, value):
    with pytest.raises(ValidationError):
        ExtractedFields(**{field: value})


def test_llm_output_model_cannot_touch_seed_fields():
    seed_fields = {
        "record_type",
        "study_design",
        "verification",
        "arm_label",
        "media",
        "intervention_orders",
        "population",
        "clinical_status",
    }
    assert not seed_fields & set(ExtractedFields.model_fields)
    assert seed_fields <= set(EvidenceMetadata.model_fields)


def test_seed_and_extraction_combine():
    extracted = ExtractedFields(n=131, n_detail="131 (68/63)", results=[])
    meta = _arm(**extracted.model_dump())
    assert meta.n == 131 and meta.record_type is RecordType.ARM


def test_record_in_rules():
    meta = _arm()
    EvidenceRecordIn(
        paper_id="P24", experiment_id="P24-E1", section=Section.RESULTS, content="…", metadata=meta
    )
    with pytest.raises(ValidationError, match="ARM 레코드만 experiment_id"):
        EvidenceRecordIn(paper_id="P24", section=Section.RESULTS, content="…", metadata=meta)
    with pytest.raises(ValidationError, match="paper_id로 시작"):
        EvidenceRecordIn(
            paper_id="P24",
            experiment_id="P33-E1",
            section=Section.RESULTS,
            content="…",
            metadata=meta,
        )


@pytest.mark.parametrize("experiment_id", ["P24-E0", "P24-E01", "P２４-E1", "P24-S1"])
def test_experiment_id_format(experiment_id):
    with pytest.raises(ValidationError):
        EvidenceRecordIn(
            paper_id=experiment_id[:3],
            experiment_id=experiment_id,
            section=Section.RESULTS,
            content="…",
            metadata=_arm(),
        )
