"""활동 노드의 언어판 — 화면에 나가는 한국어 문구(protocols.TEXT_PATHS)마다 영어판이 있고,
원문이 바뀌면 잡는다. 영어 세션은 그 문구만 바꾼 사본을 쓰고, 한국어로만 할 수 있는 활동은
후보에서 뺀다."""

import logging
import re

import pytest

from app import protocols
from app.agent.nodes.executor import prepare_session
from app.agent.nodes.planner import check_plan
from app.agent.state import CarePlan, IntakeResult, PlannedActivity
from app.schemas.care import CareGoal, CareRequest
from app.schemas.evidence import InterventionOrder

HANGUL = re.compile(r"[가-힣]")
PROTOCOL_IDS = sorted(protocols.load_protocols())
ENGLISH = protocols.load_texts("en")


@pytest.mark.parametrize("protocol_id", PROTOCOL_IDS)
def test_every_user_facing_text_has_an_english_version(protocol_id):
    """원문을 고치면 data/protocols/en/<활동>.json의 ko · en도 함께 고친다."""
    source = protocols.user_texts(protocols.get_protocol(protocol_id))
    texts = ENGLISH[protocol_id].texts
    assert [(t.path, t.ko) for t in texts] == source
    assert all(t.en.strip() and not HANGUL.search(t.en) for t in texts)


@pytest.mark.parametrize("protocol_id", PROTOCOL_IDS)
def test_english_copy_changes_only_the_user_facing_text(protocol_id):
    original = protocols.get_protocol(protocol_id)
    english = protocols.localized(original, "en")
    assert protocols.user_texts(english) == []  # 화면에 나가는 자리에 한글이 남지 않는다
    # 나머지(근거 · 등급 · 검수 메모 · 생성 설정 …)는 그대로
    changed = {t.path for t in ENGLISH[protocol_id].texts}
    before = dict(protocols._walk(original.model_dump(mode="json")))
    after = dict(protocols._walk(english.model_dump(mode="json")))
    assert set(before) == set(after)
    assert {k for k in before if before[k] != after[k]} <= changed
    assert protocols.localized(original, "ko") is original


def test_a_stale_translation_keeps_the_korean_text(caplog):
    protocol = protocols.get_protocol("PR-01")
    texts = ENGLISH["PR-01"].model_copy(deep=True)
    texts.texts[0].ko = "예전 이름"  # 원본이 바뀐 뒤 언어판을 고치지 않았다
    with caplog.at_level(logging.WARNING):
        out = protocols.apply_texts(protocol, texts, "en")
    assert out.name == protocol.name  # 그 자리만 한국어로 둔다
    assert out.guide.intro_text == texts.texts[1].en
    assert "언어판 원문이 원본과 달라" in caplog.text


def test_korean_only_activities_are_left_out_of_other_languages():
    def ids(lang):
        return {p.protocol_id for p in protocols.candidates(CareGoal.S1, lang=lang)}

    assert "PR-21" in ids("ko") and "PR-21" not in ids("en")
    assert ids("ko") - ids("en") == protocols.KOREAN_ONLY
    english = {
        "request": CareRequest(text="-", lang="en"),
        "intake": IntakeResult(goal=CareGoal.S1),
    }
    problems = check_plan(PlannedActivity(protocol_id="PR-21", minutes=16), english)
    assert problems == ["PR-21는 한국어로만 할 수 있어 en 세션에서 쓸 수 없다"]


def test_an_english_session_gets_english_activity_content():
    plan = CarePlan(activity=PlannedActivity(protocol_id="PR-01", minutes=6), rationale="-")
    state = {
        "request": CareRequest(text="-", lang="en"),
        "intake": IntakeResult(goal=CareGoal.S3, order_condition=InterventionOrder.POST_FATIGUE),
        "plan": plan,
    }
    content = prepare_session(state)["content"]
    assert content.name == "Nature photo viewing"
    assert content.guide_text.startswith("For about 6 minutes")
    assert [s.label for s in content.steps] == [
        "Nature photo slideshow",
        "Noticing how you feel now",
    ]
    assert content.notice_prompt == "Noticing how you feel now"
    # NOTICE 팝업의 시작 문구(단계 모듈 PR-16)도 영어
    assert content.notice_module["popup"]["start_text"].startswith("Enter your emotion")
    korean = prepare_session({**state, "request": CareRequest(text="-")})["content"]
    assert korean.name == "자연 사진 감상"
