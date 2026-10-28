"""prepare — 활동 노드 JSON을 FE 플레이어가 실행할 ActivityContent로 옮긴다.

자산 해석(외부 공급자 · 검수 자산)은 tests/content가 본다. 여기서는 공급자를 부르지 않게 바꿔
끼우고, 활동 노드 담당이 쓴 실행 명세(content_spec)와 NOTICE 단계의 단계 모듈이 빠짐없이 실리는지만
본다.
"""

import pytest

from app import protocols
from app.agent.nodes import executor
from app.agent.state import CarePlan, PlannedActivity
from app.content import ContentResolution
from app.schemas.care import CareRequest
from app.schemas.protocol import Composition, StepType

ACTIVITIES = [
    p for p in protocols.load_protocols().values() if p.composition is not Composition.STAGE_MODULE
]
NOTICE_HOSTS = ["PR-01", "PR-09", "PR-15", "PR-21"]


@pytest.fixture(autouse=True)
def no_providers(monkeypatch):
    monkeypatch.setattr(
        executor,
        "resolve_activity_assets",
        lambda protocol, request, **_: ContentResolution([], []),
    )


def prepare(protocol_id: str, minutes: int):
    plan = CarePlan(
        activity=PlannedActivity(protocol_id=protocol_id, minutes=minutes), rationale="-"
    )
    state = {"plan": plan, "request": CareRequest(text="-")}
    return executor.prepare_session(state)["content"]


@pytest.mark.parametrize("protocol", ACTIVITIES, ids=[p.protocol_id for p in ACTIVITIES])
def test_every_activity_hands_its_execution_spec_to_the_player(protocol):
    content = prepare(protocol.protocol_id, int(protocol.duration_minutes.default))
    assert content.content_spec == protocol.content_spec.model_dump(mode="json")
    has_notice = any(s.type is StepType.NOTICE for s in protocol.steps)
    assert (content.notice_module is not None) == has_notice


def test_notice_steps_are_drawn_with_the_stage_module():
    hosts = [p.protocol_id for p in ACTIVITIES if any(s.type is StepType.NOTICE for s in p.steps)]
    assert hosts == NOTICE_HOSTS
    module = protocols.stage_module()
    assert module is not None and module.protocol_id == "PR-16"
    ui = module.content_spec.model_extra["session_ui"]
    assert ui["host_integration"]["host_protocol_ids"] == hosts  # PR-16 JSON이 적어 둔 호스트

    content = prepare("PR-09", 3)
    notice = next(s for s in protocols.get_protocol("PR-09").steps if s.type is StepType.NOTICE)
    assert content.notice_prompt == notice.label
    assert content.notice_module == ui
    assert content.notice_module["input"]["slot_count"] == 3


def test_scripts_and_screen_spec_reach_the_player():
    # B: 호흡 안내 — 남은 시간에 맞춰 재생할 대본과 화면 명세
    breathing = prepare("PR-06", 5).content_spec
    audio = breathing["slots"][0]
    cues = ["REMAINING_180", "REMAINING_60", "END"]
    assert [c["script_id"] for c in audio["render"]["cues"]] == cues
    assert {s["id"] for s in audio["scripts"]} >= set(cues)
    assert breathing["session_ui"]["component"] == "CIRCULAR_SESSION_TIMER"
    # C: 야외 보행 — 단계별 고정 문구와 시작 전 안전 확인
    walk = prepare("PR-11", 50).content_spec
    assert [s["phase"] for s in walk["slots"][0]["scripts"]] == [
        "PRE_ACTIVITY_CHECK",
        "RUNNING",
        "TARGET_REACHED",
        "STOP_CONFIRMATION",
        "RESULT",
    ]
    assert walk["session_ui"]["safety_check"]["items"] == ["walking_environment", "body_condition"]
