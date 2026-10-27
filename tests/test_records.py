"""세션 기록의 행 — 분석 테이블에 글이 새지 않는지, SAFETY 세션이 끝난 사실만 남기는지."""

import re
from pathlib import Path

from app import records
from app.agent.state import IntakeResult, NextStep, SessionEnd
from app.api.sessions import Step
from app.schemas.care import CareGoal, CareRequest, Setting, UserProfile

MIGRATION = Path(__file__).parents[1] / "supabase/migrations/20261027000000_care_records.sql"


def allowed(column: str) -> set[str]:
    """마이그레이션의 check (column in (...)) 값."""
    sql = MIGRATION.read_text()
    match = re.search(rf"check \({column} in \(([^)]*)\)\)", sql)
    assert match, column
    return set(re.findall(r"'([A-Z_0-9]+)'", match.group(1)))


def test_the_migration_checks_match_the_code_values():
    # 값을 더하면 새 마이그레이션도 만든다 — 아니면 그 세션의 기록 쓰기가 제약에 걸린다
    assert allowed("step") == {step.value for step in Step}
    assert allowed("goal") == {goal.value for goal in CareGoal}
    assert allowed("end_reason") == {"DONE"} | {end.value for end in SessionEnd}
    assert allowed("recommended") == allowed("next_step") == {s.value for s in NextStep}


def test_every_intake_field_is_either_a_record_or_a_text():
    # IntakeResult에 칸이 생기면 분석 기록(INTAKE_FIELDS)인지 원문(INTAKE_TEXT_FIELDS)인지 정한다
    fields, texts = set(records.INTAKE_FIELDS), set(records.INTAKE_TEXT_FIELDS)
    assert fields | texts == set(IntakeResult.model_fields)
    assert not fields & texts


def test_the_request_row_keeps_the_chips_but_not_the_text_or_the_profile():
    request = CareRequest(
        text="발표 앞두고 긴장돼요",
        setting=Setting.OFFICE_CAFE,
        earphones=True,
        minutes_available=10,
        profile=UserProfile(accessibility_note="빨강·초록을 잘 구별하지 못해요"),
    )
    assert records.request_row(request) == {
        "setting": "OFFICE_CAFE",
        "earphones": True,
        "minutes_available": 10,
        "goal": None,
        "place": None,
        "content_type": "CURATED_PHOTO",
    }


def test_a_safety_end_keeps_only_the_fact():
    values = {
        "request": CareRequest(text="다 끝내고 싶어요", setting=Setting.HOME),
        "intake": IntakeResult(goal=CareGoal.S2, summary="…", cues=["다 끝내고 싶어요"]),
        "ended": SessionEnd.SAFETY,
    }
    view = {"step": "END", "node": None, "payload": {"reason": "SAFETY"}}
    assert records.session_columns(values, view) == {
        "step": "END",
        "node": None,
        "end_reason": "SAFETY",
        "end_summary": {"reason": "SAFETY"},
        "ended": True,
        "goal": None,
        "request": {},
        "intake": None,
        "turns": [],
        "pre": None,
        "final": None,
    }


def test_intake_records_carry_definition_papers_not_sentences():
    intake = IntakeResult.model_validate(
        {
            "goal": "S1",
            "goal_evidence": [{"paper_id": "P12", "sentence": "Repetitive negative thinking …"}],
            "summary": "발표가 끝났는데 같은 장면이 떠오른다",
            "cues": ["계속 생각나요"],
        }
    )
    row = records.intake_row(intake)
    assert row["goal_evidence"] == ["P12"]
    assert "summary" not in row and "cues" not in row
