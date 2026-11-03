"""app/agent/rationale_rules.py — 추천 이유 글자 규칙. plan 노드의 후검증(어기면 한 번 다시
묻는다)과 추천 이유 평가(scripts/rationale_eval.py check)가 같이 쓴다.
"""

import unicodedata

import pytest

from app import protocols
from app.agent import rationale_rules
from app.agent.rationale_rules import RationaleFacts
from app.schemas.care import CareGoal
from app.schemas.retrieval import GOAL_OUTCOMES

EVIDENCE = {
    "id": 54,
    "key": "P14",
    "authors": "Berman MG, Jonides J, Kaplan S",
    "year": 2008,
    "supports_goal": True,
    "outcomes": ["RUMINATION"],
    "flags": [],
}
GOOD = (
    "같은 생각이 계속 맴도는 지금은 잠시 주의를 다른 곳에 두는 활동이 도움이 될 수 있어요. "
    "Berman 등(2008)의 연구에서 자연을 보는 활동 뒤 반추가 줄었어요."
)


def facts(
    *,
    evidence=(EVIDENCE,),
    composition: str = "SINGLE",
    grades: dict[str, str] | None = None,
    badges=(),
    factors: dict | None = None,
    lang="ko",
    disclose=(),
) -> RationaleFacts:
    return RationaleFacts(
        tuple(evidence),
        composition,
        grades or {"RUMINATION": "B"},
        tuple(badges),
        factors,
        lang,
        tuple(disclose),
    )


def test_a_clean_rationale_passes_every_rule():
    assert {name: ok for name, (ok, _) in rationale_rules.check(GOOD, facts()).items()} == {
        "sentences": True,
        "script": True,
        "codes": True,
        "stats": True,
        "factors": True,
        "experimental": True,
        "years": True,
        "no_evidence": None,  # 근거가 있으면 해당 없음
        "disclosure": None,  # 꼭 밝힐 것이 없으면 해당 없음
    }


@pytest.mark.parametrize(
    "text, foreign",
    [
        ("깊고 천천히 호흡하는 완화 अभ्यास도 같은 지표를 다뤘습니다.", "अभ्यास"),
        ("반복 생각에서 잠시 فاص하는 데 맞습니다.", "فاص"),
        ("(محمد Khir et al., 2024)", "محمد"),
    ],
)
def test_script_catches_letters_outside_hangul_and_latin(text, foreign):
    """2026-10-29 plan eval 108개에서 실제로 나온 문장들."""
    ok, detail = rationale_rules.check_script(text, facts())
    letters = {ch for ch in foreign if unicodedata.category(ch).startswith("L")}  # 모음 부호 빼고
    assert ok is False and set(detail) == letters


def test_script_allows_accented_author_names_and_symbols():
    text = "Bolzenkötter, Neubauer & Koval(2025)은 반추가 줄었다고 봤어요 — 불안·스트레스 ↓."
    assert rationale_rules.check_script(text, facts())[0] is True


def test_factors_catch_the_chosen_content_in_the_rationale():
    forest = rationale_rules.factor_labels({"scenery": ["forest"]})[0]
    chosen = {"scenery": ["forest", "sea"], "soundscape": None}
    text = f"큐레이션 사진에서 {forest} 한 장을 고르면 도움이 돼요. 지금 해 보세요."
    ok, detail = rationale_rules.check_factors(text, facts(factors=chosen))
    assert ok is False and detail == forest
    assert rationale_rules.check_factors(GOOD, facts(factors=chosen))[0] is True
    assert rationale_rules.check_factors(text, facts())[0] is True  # 고른 Factor가 없다


@pytest.mark.parametrize(
    "text, leaked",
    [
        ("ADAPTED_STIMULUS가 있어 효과가 다를 수 있어요.", "ADAPTED_STIMULUS"),
        ("POST_FATIGUE 상태라서 골랐어요.", "POST_FATIGUE"),
        ("S1 목표에 맞춰 골랐어요.", "S1"),
        ("PR-09를 골랐어요.", "PR-09"),
        ("P32-E1 연구에서 줄었어요.", "P32-E1"),
        ("[54] 근거에 따르면 줄었어요.", "[54]"),
    ],
)
def test_codes_catch_enum_names_and_ids_even_before_a_korean_particle(text, leaked):
    ok, detail = rationale_rules.check_codes(text, facts())
    assert ok is False and leaked in detail


def test_codes_allow_author_names_and_measure_names():
    text = "Berman 등(2008)의 연구에서 STAI 점수가 줄었어요."
    assert rationale_rules.check_codes(text, facts())[0] is True


@pytest.mark.parametrize(
    "text",
    [
        "p < .05로 유의했어요.",
        "효과 크기 g = 0.4였어요.",
        "SMD로 보면 줄었어요.",
        "(95% CI 0.2–0.6)",
    ],
)
def test_stats_catch_statistics(text):
    assert rationale_rules.check_stats(text, facts())[0] is False


def test_experimental_needs_the_composition_d_only_grades_or_the_card_badge():
    text = "실험적인 구성이라 효과는 더 지켜봐야 해요."
    check = rationale_rules.check_experimental
    assert check(text, facts())[0] is False  # SINGLE · 반추 B
    assert check(text, facts(composition="EXPERIMENTAL"))[0] is True
    assert check(text, facts(grades={"RUMINATION": "D"}))[0] is True
    assert check(text, facts(badges=["EXPERIMENTAL"]))[0] is True


def test_cited_years_must_come_from_the_evidence():
    assert rationale_rules.check_years(GOOD, facts())[0] is True
    ok, detail = rationale_rules.check_years("Kaplan(1995)에 따르면 주의가 회복돼요.", facts())
    assert ok is False and detail == "1995"
    assert rationale_rules.check_years(GOOD, facts(evidence=()))[0] is False  # 근거 없이 인용


def test_an_empty_card_needs_an_honest_sentence():
    honest = "지금 상황에 맞춰 컬러링을 골랐어요. 다만 이 조건에 맞는 연구 근거는 찾지 못했어요."
    assert rationale_rules.check_no_evidence(honest, facts(evidence=()))[0] is True
    silent = "컬러링으로 마음을 가라앉혀 보세요. 도움이 될 거예요."
    assert rationale_rules.check_no_evidence(silent, facts(evidence=()))[0] is False


def test_sentences_do_not_split_at_et_al():
    text = "Berman et al. (2008)에서 줄었어요. 지금 해 보세요. 천천히 하면 돼요. 끝까지 해 보세요."
    assert len(rationale_rules.sentences(text)) == 4
    assert rationale_rules.check_sentences(text, facts())[0] is False
    assert rationale_rules.check_sentences(GOOD, facts())[0] is True


def test_a_limit_sentence_starting_with_daman_allows_a_fourth_sentence():
    limit = "다만 같은 방식의 연구 중에는 효과가 없거나 반대인 결과도 있습니다."
    adapted = (
        "연구에서 쓴 자극을 LUMI에 맞게 바꾼 콘텐츠라 같은 효과가 나는지는 확인되지 않았습니다."
    )
    assert rationale_rules.check_sentences(f"{GOOD} {limit}", facts())[0] is True
    assert rationale_rules.check_sentences(f"{GOOD} {limit} {adapted}", facts())[0] is True
    too_long = f"{GOOD} {limit} {adapted} 천천히 해 보세요."
    assert rationale_rules.check_sentences(too_long, facts()) == (False, "5문장")


def test_problems_say_what_to_fix_but_the_plan_node_skips_the_sentence_count():
    text = "PR-09를 골랐어요. 지금 해 보세요. 천천히 하면 돼요. 끝까지 해 보세요."
    found = rationale_rules.problems(text, facts())
    assert found == ["코드 · ID(PR-09)를 그대로 썼다 — 뜻으로 풀어 쓴다"]
    everything = rationale_rules.problems(text, facts(), names=rationale_rules.CHECKS)
    assert (
        "추천 이유가 2~3문장(한계를 밝히는 '다만' 문장이 있으면 4문장)이 아니다(4문장)"
        in everything
    )
    assert rationale_rules.problems(GOOD, facts()) == []


def test_facts_for_reads_the_activity_and_the_goal_grades():
    protocol = protocols.get_protocol("PR-09")
    found = rationale_rules.facts_for(protocol, CareGoal.S1, [], ["PROVISIONAL_MEASURE"])
    assert found.composition == protocol.composition.value
    assert set(found.grades) == {o.value for o in GOAL_OUTCOMES[CareGoal.S1].primary}
    assert found.evidence == () and found.badges == ("PROVISIONAL_MEASURE",)


# ── 영어 세션 ──────────────────────────────────────────────────────────────────────────────

GOOD_EN = (
    "When the same thoughts keep circling, shifting your attention elsewhere for a while can help. "
    "In a study by Berman and colleagues (2008), rumination decreased after viewing nature."
)


def test_a_clean_english_rationale_passes_every_rule():
    results = rationale_rules.check(GOOD_EN, facts(lang="en"))
    assert {name: ok for name, (ok, _) in results.items()} == {
        "sentences": True,
        "script": True,
        "codes": True,
        "stats": True,
        "factors": True,
        "experimental": True,
        "years": True,
        "no_evidence": None,
        "disclosure": None,
    }


def test_english_script_allows_only_latin_letters():
    ok, _ = rationale_rules.check_script(
        "Nature images helped Öhman (2020) — 10 min.", facts(lang="en")
    )
    assert ok
    ok, found = rationale_rules.check_script("This 활동 can help you rest.", facts(lang="en"))
    assert not ok and found == "활동"
    # 한국어 세션은 한글이 맞다
    assert rationale_rules.check_script("이 활동이 도움이 돼요.", facts())[0]


def test_english_limit_sentence_starts_with_that_said_or_however():
    four = (
        "You seem tense before the presentation. Slow breathing suits a short break. "
        "A study by Balban and colleagues (2023) found less anxiety after breathing practice. "
        "That said, the guidance was adapted for LUMI, so the same effect isn't confirmed."
    )
    assert rationale_rules.check_sentences(four, facts(lang="en"))[0]
    assert rationale_rules.check_sentences(
        four.replace("That said,", "However,"), facts(lang="en")
    )[0]
    assert not rationale_rules.check_sentences(
        four.replace("That said, the", "The"), facts(lang="en")
    )[0]


def test_english_experimental_follows_the_same_condition():
    text = "This is an experimental combination, so its combined effect hasn't been tested."
    assert not rationale_rules.check_experimental(text, facts(lang="en"))[0]
    assert rationale_rules.check_experimental(text, facts(lang="en", composition="EXPERIMENTAL"))[0]
    # 연구를 'Experimental study'라고 불러도 조건 밖이면 걸린다 — 프롬프트가 'study'로 쓰게 한다
    assert not rationale_rules.check_experimental(
        "An Experimental study found …", facts(lang="en")
    )[0]


@pytest.mark.parametrize(
    "text",
    [
        "We haven't found verified research evidence for this activity yet.",
        "We haven’t found a verified study for this activity yet.",  # 곡선 아포스트로피
        "There is no verified study behind this activity yet.",
        "Research on this activity is still limited, so treat it as a gentle try.",
        "Evidence for this exact activity has not been found yet.",
    ],
)
def test_an_empty_english_card_needs_an_honest_sentence(text):
    assert rationale_rules.check_no_evidence(text, facts(evidence=(), lang="en"))[0] is True


def test_an_empty_english_card_without_the_sentence_fails():
    text = "This activity can help you settle down. Give it a few minutes."
    assert rationale_rules.check_no_evidence(text, facts(evidence=(), lang="en"))[0] is False


def test_english_factors_catch_the_chosen_content_by_word():
    chosen = {
        "scenery": ["sea", "lake river"],
        "soundscape": None,
        "art_subject": None,
        "music": None,
    }
    ok, found = rationale_rules.check_factors(
        "Pictures of lakes can calm you, as research shows.", facts(lang="en", factors=chosen)
    )
    assert not ok and found == "lake"
    # 'research' 안의 'sea'는 단어가 아니다
    assert rationale_rules.check_factors(
        "As research shows, it helps.", facts(lang="en", factors=chosen)
    )[0]


def test_every_catalog_value_has_english_factor_terms():
    from app.content import factors as content_factors

    catalog = {
        (axis, key)
        for axis in rationale_rules.FACTOR_AXES
        for key in content_factors.load_catalog().axes[axis].values
    }
    assert set(rationale_rules.FACTOR_TERMS_EN) == catalog


def test_english_problems_name_the_english_rule():
    text = "This 활동 is experimental. It may help."
    reasons = rationale_rules.problems(text, facts(lang="en"))
    assert any(r.startswith("영어 문장에 다른 문자") for r in reasons)
    assert any("'experimental'" in r for r in reasons)


ADAPTED = (
    "연구에서 쓴 자극을 LUMI에 맞게 바꾼 콘텐츠라 같은 효과가 나는지는 확인되지 않았습니다.",
)


def test_a_rationale_must_disclose_what_the_card_flags():
    # 2026-11-03 운영 영어 세션 — PR-06의 각색 고지를 빠뜨렸다
    told = GOOD + " 다만 연구 안내를 LUMI에 맞게 바꿔 같은 효과는 확인되지 않았어요."
    assert rationale_rules.check_disclosure(told, facts(disclose=ADAPTED))[0] is True
    assert rationale_rules.check_disclosure(GOOD, facts(disclose=ADAPTED))[0] is False
    assert rationale_rules.check_disclosure(GOOD, facts())[0] is None  # 밝힐 것이 없다
    english = GOOD_EN + " That said, the guidance was adapted for LUMI."
    assert rationale_rules.check_disclosure(english, facts(lang="en", disclose=ADAPTED))[0]
    assert not rationale_rules.check_disclosure(GOOD_EN, facts(lang="en", disclose=ADAPTED))[0]
    # 다시 물을 때의 사유에 밝힐 것을 싣는다
    (reason,) = rationale_rules.problems(GOOD_EN, facts(lang="en", disclose=ADAPTED))
    assert "'That said, …'" in reason and ADAPTED[0] in reason
