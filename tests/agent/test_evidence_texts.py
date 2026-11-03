"""카드에 보이는 근거 메타데이터의 언어판(data/evidence_texts/en.json) — 대상 · 결과의 척도 · 비교 ·
통계를 문구 사전으로 옮긴다. 적재한 근거 전체는 lumi-pg로 본다(test_evidence_texts_db.py)."""

import re

from app.agent.card_texts import evidence_glossary, evidence_text
from app.schemas.card import CardEvidence
from app.schemas.care import CareGoal
from app.schemas.evidence import InterventionOrder, Medium
from tests.agent import fakes
from tests.agent.test_reference_card import S3, card, cited

HANGUL = re.compile(r"[가-힣]")


def shown(evidence: CardEvidence) -> list[str]:
    """카드에 보이는 근거 메타데이터 칸(FE reference-card)."""
    values = [evidence.population]
    for r in evidence.results:
        values += [r.measure, r.comparison, r.stat]
    return [v for v in values if v]


def test_the_glossary_maps_korean_to_english():
    glossary = evidence_glossary("en")
    assert glossary and all(HANGUL.search(k) for k in glossary)
    assert all(v.strip() and not HANGUL.search(v) for v in glossary.values())
    assert evidence_glossary("ko") == {}
    assert evidence_text("성인(영국)", "en") == "Adults (UK)"
    assert evidence_text("SART", "en") == "SART"  # 사전에 없는 원문(영어 · 약어)은 그대로
    assert evidence_text(None, "en") is None


def test_every_fixture_value_shown_on_the_card_has_an_english_version():
    glossary = evidence_glossary("en")
    korean = set()
    for hit in fakes.load_hits():
        meta = hit.metadata
        korean |= {v for v in [meta.population_detail] if v and HANGUL.search(v)}
        for r in meta.results:
            korean |= {v for v in (r.measure, r.comparison, r.stat) if v and HANGUL.search(v)}
    assert korean and korean <= set(glossary)


def test_an_english_card_shows_english_evidence_metadata():
    evidence = cited(
        [122, 121, 142, 141],
        goal=CareGoal.S3,
        media=[Medium.STATIC_IMAGE],
        order=InterventionOrder.POST_FATIGUE,
    )
    ko = card("PR-01", S3, evidence, delivered=[Medium.STATIC_IMAGE])
    en = card("PR-01", S3, evidence, delivered=[Medium.STATIC_IMAGE], lang="en")
    assert any(HANGUL.search(v) for e in ko.evidence for v in shown(e))
    assert not [v for e in en.evidence for v in shown(e) if HANGUL.search(v)]
    # 지표 · 효과 · 근거 묶음은 언어와 상관없다
    assert [[(r.outcome, r.effect) for r in e.results] for e in en.evidence] == [
        [(r.outcome, r.effect) for r in e.results] for e in ko.evidence
    ]
