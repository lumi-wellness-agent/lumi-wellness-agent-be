"""세션 기록의 행 — 분석 테이블에 글이 새지 않는지, SAFETY 세션이 끝난 사실만 남기는지."""

import asyncio
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app import records
from app.agent.state import (
    CarePlan,
    IntakeResult,
    NextStep,
    OutcomeEvaluation,
    PlannedActivity,
    ReplanProposal,
    Round,
    Selection,
    SelectionMethod,
    SessionEnd,
    SessionReport,
)
from app.api.sessions import Step
from app.schemas.care import CareGoal, CareRequest, Setting, UserProfile

MIGRATIONS = sorted((Path(__file__).parents[1] / "supabase/migrations").glob("*.sql"))


def allowed(column: str) -> set[str]:
    """마이그레이션의 check (column in (...)) 값."""
    sql = "\n".join(path.read_text() for path in MIGRATIONS)
    match = re.search(rf"check \({column} in \(([^)]*)\)\)", sql)
    assert match, column
    return set(re.findall(r"'([A-Z_0-9]+)'", match.group(1)))


def test_the_migration_checks_match_the_code_values():
    # 값을 더하면 새 마이그레이션도 만든다 — 아니면 그 세션의 기록 쓰기가 제약에 걸린다
    assert allowed("step") == {step.value for step in Step}
    assert allowed("goal") == {goal.value for goal in CareGoal}
    assert allowed("end_reason") == {"DONE"} | {end.value for end in SessionEnd}
    assert allowed("recommended") == allowed("next_step") == {s.value for s in NextStep}
    assert allowed("rule_recommended") == {s.value for s in NextStep}


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


def test_a_guest_session_keeps_only_its_progress():
    values = {
        "request": CareRequest(
            text="발표 앞두고 긴장돼요", setting=Setting.HOME, minutes_available=10
        ),
        "intake": IntakeResult(goal=CareGoal.S2, summary="발표 전 긴장"),
    }
    view = {"step": "MEASURE", "node": "measure_pre", "payload": {"measure": "PRE"}}
    columns = records.session_columns(values, view, guest=True)
    assert columns == {
        "step": "MEASURE",
        "node": "measure_pre",
        "end_reason": None,
        "end_summary": None,
        "ended": False,
        **records.NO_CONTENT,
    }


def finished_round(protocol_id: str, next_step: NextStep, proposal: ReplanProposal) -> Round:
    plan = CarePlan(
        activity=PlannedActivity(protocol_id=protocol_id, minutes=5),
        rationale="-",
        selection=Selection(
            method=SelectionMethod.WEIGHTED,
            order=["PR-07", protocol_id],
            weights={"PR-07": 4.0, protocol_id: 4.0},
            probability=0.5,
            position=1,
        ),
    )
    return Round(
        plan=plan,
        report=SessionReport(completed=True, notice_words=["아직 떨림"]),
        pre=[],
        post=[],
        evaluation=OutcomeEvaluation(
            goal=CareGoal.S2, round=1, deltas={}, improved=True, completed=True
        ),
        next_step=next_step,
        proposal=proposal,
    )


def test_rounds_keep_how_the_activity_and_the_next_step_were_picked():
    by_llm = ReplanProposal(
        recommended=NextStep.ADDITIONAL_SESSION,
        reason="아직 떨림이 남았다면 한 번 더 해 볼까요?",
        options=list(NextStep),
        rule_recommended=NextStep.END,
        by_llm=True,
    )
    row = records.round_row(1, finished_round("PR-06", NextStep.ADDITIONAL_SESSION, by_llm))
    assert row["selection"] == {
        "method": "WEIGHTED",
        "order": ["PR-07", "PR-06"],
        "weights": {"PR-07": 4.0, "PR-06": 4.0},
        "probability": 0.5,
        "position": 1,
    }
    assert (row["recommended"], row["rule_recommended"], row["recommended_by_llm"]) == (
        "ADDITIONAL_SESSION",
        "END",
        True,
    )
    assert "reason" not in row  # LLM이 쓴 이유는 사용자 글에서 나온 글이라 원문으로만
    values = {
        "request": CareRequest(text="-"),
        "rounds": [finished_round("PR-06", NextStep.END, by_llm)],
    }
    assert records.text_row(values)["rounds"][0]["proposal_reason"] == by_llm.reason
    rule = by_llm.model_copy(
        update={"recommended": NextStep.END, "reason": "사전보다 나아졌다", "by_llm": False}
    )
    values["rounds"] = [finished_round("PR-06", NextStep.END, rule)]
    assert records.text_row(values)["rounds"][0]["proposal_reason"] is None


def test_recent_protocols_are_the_users_latest_rounds_without_repeats():
    clock = iter(datetime(2026, 10, 29, 9, tzinfo=UTC) + timedelta(hours=h) for h in range(10))
    store = records.MemoryStore(clock=lambda: next(clock))
    alice, bob = uuid.uuid4(), uuid.uuid4()
    rule = ReplanProposal(recommended=NextStep.END, reason="-", options=[NextStep.END])
    view = {"step": "CONFIRM", "node": "confirm", "payload": {}}

    async def session(user: uuid.UUID, *protocol_ids: str) -> None:
        sid = str(uuid.uuid4())
        await store.create(sid, user, CareRequest(text="-"))
        rounds = [finished_round(p, NextStep.CHANGE_ACTIVITY, rule) for p in protocol_ids]
        values = {"request": CareRequest(text="-"), "rounds": rounds}
        await store.save(records.SessionRecord(id=sid, user_id=user), values, view)

    async def scenario() -> None:
        await session(alice, "PR-06", "PR-07")
        await session(bob, "PR-19")
        await session(alice, "PR-20", "PR-06")
        # 최근 라운드부터 — 새 세션의 2라운드 · 1라운드, 그다음 지난 세션
        assert await store.recent_protocols(alice, 2) == ["PR-06", "PR-20"]
        assert await store.recent_protocols(alice, 5) == ["PR-06", "PR-20", "PR-07"]  # 중복 없이
        assert await store.recent_protocols(uuid.uuid4(), 5) == []

    asyncio.run(scenario())


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
