"""scripts/rationale_eval.py — 케이스 읽기 · Judge 집계 · 사람 확인 시트 · 일치. 실제 모델 없이
구조화 출력 대역(tests/agent/fakes.py)으로 돌린다. 실제 Judge는 `make rationale-eval`로 따로
돌린다. 규칙 하나하나는 tests/agent/test_rationale_rules.py가 본다.
"""

import asyncio
import json

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


def test_record_markdown_lists_the_setup_tables_and_what_broke():
    passing = {name: {"ok": True, "detail": ""} for name in rationale_eval.CHECKS}
    rows = [
        {
            "case": "w1/S2-post-b",
            "protocol_id": "PR-01",
            "rationale": GOOD,
            "checks": passing | {"factors": {"ok": False, "detail": "숲, 바다"}},
            "judgment": judgment(faithful="발췌에 | 없는 결과").model_dump(),
            "error": None,
        },
        {
            "case": "w1/S1-pre-a",
            "protocol_id": "PR-09",
            "rationale": GOOD,
            "checks": passing,
            "judgment": None,
            "error": "RuntimeError: boom",
        },
    ]
    judges = [{"judge": "gpt-5.5", "effort": "medium", "tokens": [87089, 25406], "at": "T1"}]
    runs = [
        {
            "meta": {"model": "gpt-5.4-mini", "seed": "plan-eval", "db": "lumi-pg", "at": "T0"},
            "summary": {"runs": 36, "scenarios": 30, "change_rounds": 6},
        }
    ]
    summary = rationale_eval.summarize(rows)
    text = rationale_eval.record_markdown("기준선", rows, summary, judges, runs, rechecked=True)
    assert text.startswith("# 추천 이유 평가 — 기준선\n")
    assert "`gpt-5.4-mini` · 후보 순서 시드 `plan-eval`" in text
    assert "계획 36개(시나리오 30 + 활동 변경 6)" in text
    assert "`gpt-5.5`(추론 medium) · 토큰 87,089/25,406" in text
    assert "| factors | 1 | 2 |" in text and "| faithful | 0 | 1 | 0 |" in text
    assert "## 어긴 것 — 2건" in text
    assert "| w1/S2-post-b | PR-01 | factors | 숲, 바다 | 규칙 |" in text
    assert "| faithful | 반드시 좋아져요 | 발췌에 \\| 없는 결과 |" in text  # 세로줄을 막는다
    assert "## Judge 실패 — 1건" in text and "- w1/S1-pre-a: RuntimeError: boom" in text


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
