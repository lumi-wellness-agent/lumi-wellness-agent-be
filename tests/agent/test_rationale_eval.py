"""scripts/rationale_eval.py — 추천 이유 규칙(코드) · Judge 집계 · 사람 라벨 일치. 실제
모델 없이 구조화 출력 대역(tests/agent/fakes.py)으로 돌린다. 실제 Judge는
`make rationale-eval`로 따로 돌린다.
"""

import asyncio
import json
import unicodedata

import pytest

from scripts import rationale_eval
from scripts.rationale_eval import ItemJudgment, RationaleJudgment
from tests.agent import fakes

EVIDENCE = {
    "id": 54,
    "key": "P14",
    "authors": "Berman MG, Jonides J, Kaplan S",
    "year": 2008,
    "supports_goal": True,
    "outcomes": ["RUMINATION"],
    "flags": [],
}
EXPLAIN_INPUT = (
    "## 사용자 입력\n같은 생각이 계속 돌아요\n\n"
    "## 검증된 근거 (1건)\n- [54] Berman MG (2008) — 자연 이미지 · P14 RESULTS\n  발췌: …\n\n"
    '## 콘텐츠 Factor (고를 칸)\n[{"field": "scenery"}]'
)
GOOD = (
    "같은 생각이 계속 맴도는 지금은 잠시 주의를 다른 곳에 두는 활동이 도움이 될 수 있어요. "
    "Berman 등(2008)의 연구에서 자연을 보는 활동 뒤 반추가 줄었어요."
)


def case(
    rationale: str,
    *,
    evidence=(EVIDENCE,),
    composition: str = "SINGLE",
    grades: dict[str, str] | None = None,
    badges=(),
    factors: dict | None = None,
    case_id: str = "r1/S1-pre-a",
) -> rationale_eval.Case:
    return rationale_eval.Case(
        id=case_id,
        scenario=case_id.split("/")[1],
        round=0,
        protocol_id="PR-09",
        rationale=rationale,
        explain_input=EXPLAIN_INPUT,
        evidence=tuple(evidence),
        composition=composition,
        grades=grades or {"RUMINATION": "B"},
        badges=tuple(badges),
        factors=factors,
    )


def judgment(**fails: str) -> RationaleJudgment:
    """fails에 준 항목만 FAIL(값이 이유), disclosure는 NA, 나머지는 PASS."""

    def item(name: str) -> ItemJudgment:
        if name in fails:
            return ItemJudgment(why=fails[name], quote="반드시 좋아져요", verdict="FAIL")
        return ItemJudgment(why="-", quote="", verdict="NA" if name == "disclosure" else "PASS")

    return RationaleJudgment(**{name: item(name) for name in rationale_eval.ITEMS})


def test_a_clean_rationale_passes_every_rule():
    checks = rationale_eval.run_checks(case(GOOD))
    assert {name: c["ok"] for name, c in checks.items()} == {
        "sentences": True,
        "script": True,
        "codes": True,
        "stats": True,
        "factors": True,
        "experimental": True,
        "years": True,
        "no_evidence": None,  # 근거가 있으면 해당 없음
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
    ok, detail = rationale_eval.check_script(case(text))
    letters = {ch for ch in foreign if unicodedata.category(ch).startswith("L")}  # 모음 부호 빼고
    assert ok is False and set(detail) == letters


def test_script_allows_accented_author_names_and_symbols():
    text = "Bolzenkötter, Neubauer & Koval(2025)은 반추가 줄었다고 봤어요 — 불안·스트레스 ↓."
    assert rationale_eval.check_script(case(text))[0] is True


def test_factors_catch_the_chosen_content_in_the_rationale():
    forest = rationale_eval.factor_labels({"scenery": ["forest"]})[0]
    chosen = {"scenery": ["forest", "sea"], "soundscape": None}
    text = f"큐레이션 사진에서 {forest} 한 장을 고르면 도움이 돼요. 지금 해 보세요."
    ok, detail = rationale_eval.check_factors(case(text, factors=chosen))
    assert ok is False and detail == forest
    assert rationale_eval.check_factors(case(GOOD, factors=chosen))[0] is True
    assert rationale_eval.check_factors(case(text))[0] is True  # 고른 Factor가 없다


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
    ok, detail = rationale_eval.check_codes(case(text))
    assert ok is False and leaked in detail


def test_codes_allow_author_names_and_measure_names():
    ok, _ = rationale_eval.check_codes(case("Berman 등(2008)의 연구에서 STAI 점수가 줄었어요."))
    assert ok


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
    assert rationale_eval.check_stats(case(text))[0] is False


def test_experimental_needs_the_composition_d_only_grades_or_the_card_badge():
    text = "실험적인 구성이라 효과는 더 지켜봐야 해요."
    assert rationale_eval.check_experimental(case(text))[0] is False  # SINGLE · 반추 B
    assert rationale_eval.check_experimental(case(text, composition="EXPERIMENTAL"))[0] is True
    assert rationale_eval.check_experimental(case(text, grades={"RUMINATION": "D"}))[0] is True
    assert rationale_eval.check_experimental(case(text, badges=["EXPERIMENTAL"]))[0] is True


def test_cited_years_must_come_from_the_evidence():
    assert rationale_eval.check_years(case(GOOD))[0] is True
    ok, detail = rationale_eval.check_years(case("Kaplan(1995)에 따르면 주의가 회복돼요."))
    assert ok is False and detail == "1995"
    assert rationale_eval.check_years(case(GOOD, evidence=()))[0] is False  # 근거 없이 인용


def test_an_empty_card_needs_an_honest_sentence():
    honest = "지금 상황에 맞춰 컬러링을 골랐어요. 다만 이 조건에 맞는 연구 근거는 찾지 못했어요."
    assert rationale_eval.check_no_evidence(case(honest, evidence=()))[0] is True
    silent = "컬러링으로 마음을 가라앉혀 보세요. 도움이 될 거예요."
    assert rationale_eval.check_no_evidence(case(silent, evidence=()))[0] is False


def test_sentences_do_not_split_at_et_al():
    text = "Berman et al. (2008)에서 줄었어요. 지금 해 보세요. 천천히 하면 돼요. 끝까지 해 보세요."
    assert len(rationale_eval.sentences(text)) == 4
    assert rationale_eval.check_sentences(case(text))[0] is False
    assert rationale_eval.check_sentences(case(GOOD))[0] is True


def test_the_judge_sees_the_explainer_input_without_the_factor_menu():
    text = rationale_eval.judge_input(case(GOOD))
    assert "## 검증된 근거 (1건)" in text and "콘텐츠 Factor" not in text
    assert text.endswith(GOOD)


def test_summary_leaves_out_na_items_and_failed_judge_calls():
    model = fakes.FakeStructuredModel(
        judgment(), judgment(hedged="효과를 단정했다"), RuntimeError("boom")
    )
    cases = [case(GOOD, case_id=f"r1/S1-{n}") for n in ("a", "b", "c")]
    judge = rationale_eval.make_judge(model)
    rows = asyncio.run(rationale_eval.judge_cases(cases, judge, concurrency=1))
    summary = rationale_eval.summarize(rows)
    assert summary["judged"] == 2 and summary["errors"] == ["r1/S1-c"]
    assert summary["items"]["hedged"] == [1, 2]
    assert summary["items"]["disclosure"] == [0, 0]  # NA는 세지 않는다
    assert summary["clean"] == [1, 2]
    assert summary["checks"]["sentences"] == [3, 3]  # 규칙은 Judge 실패와 상관없이 센다
    report = rationale_eval.format_report(rows, summary)
    assert "r1/S1-b PR-09: hedged “반드시 좋아져요” — 효과를 단정했다" in report


def test_load_cases_skips_rejected_rounds_and_marks_change_rounds(tmp_path):
    run = {
        "scenario": "S1-pre-a",
        "round": 0,
        "protocol_id": "PR-09",
        "rationale": GOOD,
        "explain_input": EXPLAIN_INPUT,
        "evidence": [EVIDENCE],
        "composition": "SINGLE",
        "grades": {"RUMINATION": "B"},
        "badges": [],
    }
    runs = [run, run | {"round": 1, "protocol_id": "PR-15"}, run | {"rationale": None}]
    path = tmp_path / "r1.json"
    path.write_text(json.dumps({"meta": {}, "runs": runs}, ensure_ascii=False), encoding="utf-8")
    cases = rationale_eval.load_cases([path])
    assert [c.id for c in cases] == ["r1/S1-pre-a", "r1/S1-pre-a+change"]


def test_judged_files_merge_cases_and_tokens(tmp_path):
    paths = []
    for name, tokens in (("a", [100, 10]), ("b", [200, 20])):
        path = tmp_path / f"judged_{name}.json"
        payload = {"meta": {"tokens": tokens}, "cases": [{"case": f"{name}/S1-a"}]}
        path.write_text(json.dumps(payload), encoding="utf-8")
        paths.append(path)
    rows, tokens = rationale_eval.load_judged(paths)
    assert [r["case"] for r in rows] == ["a/S1-a", "b/S1-a"] and tokens == [300, 30]


def test_review_sheet_takes_every_judge_fail_and_a_pass_sample():
    rows = [
        {"case": "r1/S1-a", "rationale": GOOD, "judgment": judgment(hedged="단정").model_dump()},
        {"case": "r1/S1-b", "rationale": GOOD, "judgment": judgment().model_dump()},
        {"case": "r1/S1-c", "rationale": GOOD, "judgment": None},  # Judge 실패는 뺀다
    ]
    review = rationale_eval.review_rows(rows, {"r1/S1-a": case(GOOD)}, pass_sample=1)
    hedged = [r for r in review if r["item"] == "hedged"]
    assert [(r["case"], r["judge"]) for r in hedged] == [("r1/S1-a", "FAIL"), ("r1/S1-b", "PASS")]
    assert [r["judge"] for r in review if r["item"] == "disclosure"] == []  # NA는 표본이 아니다
    assert (
        hedged[0]["input"].startswith("## 사용자 입력")
        and "콘텐츠 Factor" not in hedged[0]["input"]
    )
    assert all(r["human"] == "" for r in review)


def test_agreement_reads_o_x_labels_and_skips_blank_ones(tmp_path):
    rows = [{"case": "r1/S1-a", "judgment": judgment(hedged="단정").model_dump()}]
    labels = tmp_path / "labels.csv"
    labels.write_text(
        "case,item,human\n"
        "r1/S1-a,hedged,X\n"
        "r1/S1-a,faithful,O\n"
        "r1/S1-a,personal,X\n"
        "r1/S1-a,outcomes,\n",
        encoding="utf-8",
    )
    counts, mismatches = rationale_eval.agreement(rows, rationale_eval.load_labels(labels))
    assert counts["hedged"] == [1, 1] and counts["faithful"] == [1, 1]
    assert counts["personal"] == [0, 1] and counts["outcomes"] == [0, 0]
    assert mismatches == ["- r1/S1-a personal: Judge PASS · 사람 FAIL"]
