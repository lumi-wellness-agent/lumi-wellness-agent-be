"""data/protocols/PR-*.json 21개 — 본문 계약(CareProtocol)과 후보 조회. 활동 노드 담당의 합격 기준.

근거 연결 부분(evidence · grades)은 tests/schemas/test_protocol_contract.py가 본다.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app import protocols
from app.schemas.care import CareGoal, ContentType, Environment, Setting, environment_for
from app.schemas.evidence import InterventionOrder, Medium
from app.schemas.protocol import CareProtocol, Composition, SlotType, StepType

ROOT = Path(__file__).resolve().parents[2]
FILES = sorted((ROOT / "data" / "protocols").glob("PR-*.json"))
LOADED = protocols.load_protocols()
EXPECTED_IDS = [f"PR-{n:02d}" for n in range(1, 23) if n != 13]


def test_twenty_one_protocols_match_the_workbook():
    assert [p.stem for p in FILES] == EXPECTED_IDS
    assert list(LOADED) == EXPECTED_IDS


@pytest.mark.parametrize("path", FILES, ids=[p.stem for p in FILES])
def test_protocol_file_parses_as_care_protocol(path):
    protocol = CareProtocol.model_validate_json(path.read_text(encoding="utf-8"))
    assert protocol.protocol_id == path.stem
    assert protocol.steps and protocol.duration_minutes.default > 0
    for step in protocol.steps:
        assert set(step.slots) <= protocol.content_spec.slot_types
    for relation in protocol.compatible_with + protocol.exclusive_with:
        assert relation.protocol_id in LOADED, f"{path.stem}: {relation.protocol_id} 없음"


def test_compositions_follow_the_sheet():
    by_composition = {}
    for p in LOADED.values():
        by_composition.setdefault(p.composition, []).append(p.protocol_id)
    assert by_composition[Composition.VALIDATED_MULTIMODAL] == ["PR-02", "PR-12"]
    assert by_composition[Composition.EXPERIMENTAL] == ["PR-10", "PR-15", "PR-21", "PR-22"]
    assert by_composition[Composition.STAGE_MODULE] == ["PR-16"]
    assert len(by_composition[Composition.SINGLE]) == 14


def test_stage_module_is_notice_only_and_never_a_candidate():
    module = LOADED["PR-16"]
    assert module.record_type == "stage_module" and module.media == []
    assert [s.type for s in module.steps] == [StepType.NOTICE]
    for goal in CareGoal:
        assert "PR-16" not in {p.protocol_id for p in protocols.candidates(goal)}


def test_every_goal_has_candidates_with_primary_goal_first():
    """주 목표가 앞, 같은 쪽은 목표 1차 지표 등급 순. 등급은 담당이 고치므로 순서의 규칙만 본다."""
    for goal in CareGoal:
        found = protocols.candidates(goal)
        assert found, goal
        keys = [(p.goal is not goal, protocols.goal_grade_rank(p, goal)) for p in found]
        assert keys == sorted(keys), goal
        assert any(p.goal is goal for p in found), goal
    s2 = [p.protocol_id for p in protocols.candidates(CareGoal.S2)]
    assert s2[-2:] == ["PR-10", "PR-11"]  # S3 주 목표 · S2 부 목표가 뒤


def test_goal_grade_rank_orders_primary_outcome_grades():
    # S2 1차 지표 SUBJECTIVE_STRESS · AFFECT. 좋은 등급이 앞, —(근거 없음)이 끝
    assert protocols.goal_grade_rank(LOADED["PR-06"], CareGoal.S2) == (0, 0)  # A · A
    assert protocols.goal_grade_rank(LOADED["PR-22"], CareGoal.S2) == (5, 5)  # — · —
    assert protocols.goal_grade_rank(LOADED["PR-09"], CareGoal.S1) == (1,)  # RUMINATION B


def test_candidates_filter_by_order_and_minutes():
    pre_stress = protocols.candidates(CareGoal.S2, order=InterventionOrder.PRE_STRESS)
    ids = {p.protocol_id for p in pre_stress}
    assert "PR-03" in ids and "PR-06" in ids  # PRE_STRESS 전용 · 무관(일상)
    assert "PR-01" not in ids and "PR-02" not in ids  # POST_* 전용
    short = {p.protocol_id for p in protocols.candidates(CareGoal.S2, minutes=5)}
    assert "PR-06" in short and "PR-07" not in short  # 20분 PMR은 5분에 안 맞는다
    without = [p.protocol_id for p in protocols.candidates(CareGoal.S1, exclude=["PR-09"])]
    assert "PR-09" not in without and without[0] == "PR-15"  # 직전 라운드에서 바꾸기로 한 활동
    # 특별한 계기가 없는 사용자(NO_INDUCTION)는 어느 프로토콜이든 쓴다 — POST_* 전용 PR-01도
    anyone = protocols.candidates(CareGoal.S3, order=InterventionOrder.NO_INDUCTION, minutes=10)
    assert [p.protocol_id for p in anyone] == ["PR-01", "PR-04"]
    assert LOADED["PR-01"].allows_order(InterventionOrder.NO_INDUCTION)
    assert not LOADED["PR-01"].allows_order(InterventionOrder.PRE_STRESS)


def test_candidates_filter_by_environment_and_relax_in_order():
    cafe = environment_for(Setting.OFFICE_CAFE, False)  # 이어폰 없는 카페 — 소리 X · 조용함 X
    assert protocols.environment_gaps(LOADED["PR-09"], cafe) == ["AUDIO", "QUIET"]
    home = environment_for(Setting.HOME, None)
    assert protocols.environment_gaps(LOADED["PR-11"], home) == ["OUTDOORS"]  # 실외 걷기
    assert protocols.environment_gaps(LOADED["PR-09"], None) == []  # 모르면 거르지 않는다
    s1 = protocols.candidates(CareGoal.S1, minutes=5, environment=cafe)
    assert [p.protocol_id for p in s1] == ["PR-15"]

    # S3 + 사건 전(시험 앞두고 오래 공부함): 상태가 맞는 S3 활동이 없어 상태를 풀고 환경은 지킨다
    search = protocols.plan_candidates(
        CareGoal.S3, order=InterventionOrder.PRE_STRESS, minutes=10, environment=home
    )
    assert search.relaxed == ("order",)
    assert [p.protocol_id for p in search.protocols] == ["PR-01", "PR-04"]
    # 어떤 환경 조건도 안 되면 환경까지 푼다
    nothing = Environment(audio=False, screen=False, quiet=False, outdoor=False)
    search = protocols.plan_candidates(CareGoal.S1, minutes=5, environment=nothing)
    assert search.relaxed == ("environment",) and search.protocols
    # 시간은 맨 마지막에만 푼다 — 3분 칩인데 S3에 그만큼 짧은 활동이 없다(PR-01 6분)
    search = protocols.plan_candidates(
        CareGoal.S3, order=InterventionOrder.POST_FATIGUE, minutes=3, environment=home
    )
    assert search.relaxed == ("minutes",)
    assert search.protocols[0].protocol_id == "PR-01"
    # 3분에 맞는 활동이 있으면 시간을 풀지 않는다(S2 → 미술 감상 1–2분)
    search = protocols.plan_candidates(CareGoal.S2, minutes=3, environment=home)
    assert search.relaxed == () and [p.protocol_id for p in search.protocols] == ["PR-20"]


def test_compact_is_what_the_planner_reads():
    compact = protocols.compact(LOADED["PR-06"])
    assert compact["protocol_id"] == "PR-06" and compact["goal"] == "S2"
    assert compact["duration_minutes"] == {"default": 5.0, "min": 5.0, "max": 5.0}
    assert compact["grades"]["SUBJECTIVE_STRESS"] == "A"
    assert "evidence" not in compact and "content_spec" not in compact


def test_delivered_media_follows_the_image_preference():
    curated, generated = ContentType.CURATED_PHOTO, ContentType.AI_IMAGE
    # PR-01은 대안(STATIC_IMAGE · AI_IMAGE)을 함께 적었다 → 선호에 맞는 하나만
    assert protocols.delivered_media(LOADED["PR-01"], curated) == [Medium.STATIC_IMAGE]
    assert protocols.delivered_media(LOADED["PR-01"], generated) == [Medium.AI_IMAGE]
    # 정지 이미지 슬롯이 생성을 허용하면 AI 생성 이미지로 바꾼다
    assert protocols.delivered_media(LOADED["PR-21"], generated) == [
        Medium.AI_IMAGE,
        Medium.MINDFULNESS,
    ]
    assert protocols.delivered_media(LOADED["PR-21"], curated) == LOADED["PR-21"].media
    assert protocols.delivered_media(LOADED["PR-06"], generated) == [Medium.BREATHING]
    # 생성을 막은 슬롯이면 그대로 둔다
    forbidden = LOADED["PR-03"].model_copy(deep=True)
    for slot in forbidden.content_spec.slots:
        slot.source_policy["generated"] = "FORBIDDEN"
    assert protocols.delivered_media(forbidden, generated) == [Medium.STATIC_IMAGE]


def test_body_invariants():
    doc = json.loads((ROOT / "data" / "protocols" / "PR-06.json").read_text(encoding="utf-8"))
    with pytest.raises(ValidationError, match="content_spec.slots에 없다"):
        CareProtocol.model_validate({**doc, "content_spec": {"slots": []}})
    with pytest.raises(ValidationError, match="steps.order"):
        CareProtocol.model_validate({**doc, "steps": [{**doc["steps"][0], "order": 2}]})
    with pytest.raises(ValidationError, match="record_type"):
        CareProtocol.model_validate({**doc, "composition": "STAGE_MODULE"})
    with pytest.raises(ValidationError, match="default가 max"):
        CareProtocol.model_validate(
            {**doc, "duration_minutes": {"default": 30, "min": 5, "max": 20}}
        )


def test_every_protocol_carries_an_execution_spec():
    """FE 플레이어가 읽을 실행 명세 — 화면 명세(session_ui)나 슬롯별 재생 · 선택 · 대본 중 하나.

    월요일 초안(시트에서 만든 뼈대)만 남은 활동이 없어야 한다. C 파트는 scratch/prNN의 실행 계약을
    content_spec.session_ui로 옮겼다.
    """
    for p in LOADED.values():
        extra = p.content_spec.model_extra or {}
        slot_specs = [
            key
            for slot in p.content_spec.slots
            for key in ("render", "selection", "scripts", "text")
            if key in (slot.model_extra or {})
        ]
        assert "session_ui" in extra or slot_specs, p.protocol_id


def test_writing_activities_tell_what_to_write():
    """글쓰기 슬롯에는 지시문이 있다 — 슬롯 scripts(PR-12 · 18)나 화면 명세의 entry.prompt(PR-19).

    PR-18은 대조 조건도 같은 시간 글을 썼고 지시문만 달랐다(E14) — 지시문이 없으면 처치가 아니다.
    """
    for p in LOADED.values():
        session_ui = (p.content_spec.model_extra or {}).get("session_ui", {})
        for slot in p.content_spec.slots:
            if slot.slot is not SlotType.WRITING_PROMPT:
                continue
            scripts = (slot.model_extra or {}).get("scripts", [])
            prompts = [s["text"] for s in scripts if s.get("required") and s.get("text")]
            assert prompts or session_ui.get("entry", {}).get("prompt"), p.protocol_id


def test_guide_audio_is_a_recording_or_read_by_the_browser():
    """안내 음성은 녹음하지 않는다 — 녹음 음원(PR-21 검수 자산)이 없으면 FE가 대본을
    speechSynthesis로 읽는다."""
    for p in LOADED.values():
        for slot in p.content_spec.slots:
            if slot.slot is not SlotType.GUIDE_AUDIO:
                continue
            spec = slot.model_extra or {}
            recorded = spec.get("selection", {}).get("provider") == "CURATED_ASSET_LIBRARY"
            browser = spec.get("generation", {}).get("provider") == "BROWSER_SPEECH_SYNTHESIS"
            assert recorded or browser, p.protocol_id


def test_drafts_are_marked_for_review():
    for p in LOADED.values():
        assert p.review.status == "DRAFT"
        assert p.guide.intro_text or p.guide.intro_text_ref, p.protocol_id


def test_pr22_photo_timing_fits_five_minutes():
    doc = json.loads((ROOT / "data/protocols/PR-22.json").read_text(encoding="utf-8"))
    visual = next(s for s in doc["content_spec"]["slots"] if s["slot"] == "VISUAL_STILL")
    render = visual["render"]
    background = doc["content_spec"]["session_ui"]["background"]
    assert visual["count"] * visual["seconds_each"] == render["duration_seconds"] == 300
    assert (render["count"], render["seconds_each"]) == (10, 30)
    assert (background["photo_count"], background["seconds_each"]) == (10, 30)
    assert render["transition"] == background["transition"] == "CROSSFADE"
    assert render["fade_out_ms"] == render["fade_in_ms"] == render["transition_ms"] == 2000
