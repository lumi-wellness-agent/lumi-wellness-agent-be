"""추천 이유 평가 — plan eval 결과(scripts/plan_eval.py --out)의 추천 이유를 규칙(코드)과
LLM Judge로 채점한다. 같은 시나리오를 여러 번 돌린 결과 파일을 함께 넣으면 각각 한 건으로
센다 — 같은 입력에도 모델 출력이 흔들린다(2026-10-28 S1 6회 중 4회만 플래그 이름이 샜다).

    .venv/bin/python scripts/rationale_eval.py check r1.json r2.json r3.json  # LLM 없음
    .venv/bin/python scripts/rationale_eval.py judge r1.json r2.json r3.json --out judged.json
    .venv/bin/python scripts/rationale_eval.py report judged_r1.json judged_r23.json  # 합쳐 집계
    .venv/bin/python scripts/rationale_eval.py report judged.json \
        --md docs/rag-eval/2026-10-29-rationale-baseline.md --title "기준선"         # 실행 기록
    .venv/bin/python scripts/rationale_eval.py sheet judged.json --out review.csv    # 사람 확인용
    .venv/bin/python scripts/rationale_eval.py agree judged.json review.csv

- 규칙(코드) — 설명 프롬프트(EXPLAIN_SYSTEM_PROMPT)의 지시 중 글자로 판정되는 것 8개
  (app/agent/rationale_rules.py — plan 노드가 같은 규칙으로 후검증하고 어기면 한 번 다시
  묻는다). sentences · script(한글 · 라틴 문자뿐) · codes(enum · ID) · stats · factors(고른
  콘텐츠 Factor 이름) · experimental · years(인용 연도가 근거 목록에) · no_evidence.
- Judge(LLM) — 뜻을 읽어야 하는 것. faithful(근거 · 발췌에 없는 연구 · 결과를 말하지
  않는다) · outcomes(개선 지표에 있는 것만 개선으로 말한다) · hedged(효과를 단정하거나
  임상으로 넓히지 않는다) · disclosure(플래그 · 근거 없음을 뜻으로 밝힌다) ·
  personal(사용자 상태 → 고른 이유 → 근거). 항목마다 PASS · FAIL(disclosure만 NA도)과
  문제 구절. Judge는 생성 모델(LLM_MODEL)과 다른 모델이다 — 같은 모델은 자기 글에 후하다.
  Judge가 보는 입력은 설명 호출이 본 사용자 메시지 그대로다(콘텐츠 Factor 칸은 뺀다 —
  추천 이유에는 Factor를 쓰지 않는다).
- sheet · agree — sheet가 Judge FAIL 전부와 항목마다 PASS 표본을 사람이 확인할 CSV로 뽑고,
  agree가 그 human 칸(PASS · FAIL · NA 또는 O · X · -)과 Judge 판정의 항목별 일치를 낸다.
  일치를 확인한 항목만 숫자로 인용한다.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import random
import sys
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from langchain_core.language_models import BaseChatModel  # noqa: E402
from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from app.agent import rationale_rules  # noqa: E402
from app.agent.rationale_rules import RationaleFacts  # noqa: E402
from app.config import Settings, get_settings  # noqa: E402
from scripts.plan_trial import Usage  # noqa: E402

CHECKS = rationale_rules.CHECKS  # 규칙은 운영 후검증과 같은 것(app/agent/rationale_rules.py)
ITEMS = ("faithful", "outcomes", "hedged", "disclosure", "personal")
JUDGE_MODEL = "gpt-5.5"  # 생성(gpt-5.4-mini)과 다른, 더 큰 모델
JUDGE_EFFORT = "medium"
_FACTOR_PART = "\n\n## 콘텐츠 Factor"


@dataclass(frozen=True)
class Case:
    """채점할 추천 이유 하나 — plan eval 결과의 계획 하나."""

    id: str  # <결과 파일 이름>/<시나리오>[+change]
    scenario: str
    round: int
    protocol_id: str
    rationale: str
    explain_input: str
    evidence: tuple[Mapping[str, Any], ...]
    composition: str | None
    grades: Mapping[str, str]
    badges: tuple[str, ...]
    factors: Mapping[str, Any] | None = None  # 같은 호출이 고른 콘텐츠 Factor

    @property
    def facts(self) -> RationaleFacts:
        return RationaleFacts(
            self.evidence, self.composition, self.grades, self.badges, self.factors
        )


def load_cases(paths: Sequence[Path]) -> list[Case]:
    """plan eval 결과 파일들 → 추천 이유가 있는 계획만. 계획이 실패한 라운드는 뺀다."""
    cases = []
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        for run in payload["runs"]:
            if not run.get("rationale"):
                continue
            cases.append(
                Case(
                    id=f"{path.stem}/{run['scenario']}{'+change' if run['round'] else ''}",
                    scenario=run["scenario"],
                    round=run["round"],
                    protocol_id=run["protocol_id"],
                    rationale=run["rationale"],
                    explain_input=run["explain_input"] or "",
                    evidence=tuple(run["evidence"]),
                    composition=run["composition"],
                    grades=run["grades"],
                    badges=tuple(run["badges"]),
                    factors=run.get("factors"),
                )
            )
    return cases


# --- 규칙(코드) ---------------------------------------------------------------------------------


def run_checks(case: Case) -> dict[str, dict[str, Any]]:
    """규칙마다 {"ok": True · False · None(해당 없음), "detail": 실패 내용}."""
    return {
        name: {"ok": ok, "detail": detail if ok is False else ""}
        for name, (ok, detail) in rationale_rules.check(case.rationale, case.facts).items()
    }


# --- Judge(LLM) ---------------------------------------------------------------------------------

# 2026-10-29 첫 채점(r1 36개)에서 고친 두 곳: faithful이 발췌만 보고 '개선 지표' 칸을 무시해
# 방법 발췌뿐인 근거(Toussaint 2021)의 인용을 모두 FAIL로 봤고, disclosure가 이미지를 쓰지 않는
# 활동(호흡 · 이완)에도 '이미지 제공: AI 생성' 줄 때문에 공개를 요구했다
JUDGE_SYSTEM_PROMPT = """\
너는 LUMI 추천 이유의 채점자다. 플래너는 '플래너가 받은 입력'만 보고 사용자에게 보여 줄
추천 이유를 썼다. 입력이 사실의 전부다 — 네가 아는 논문 지식으로 보태거나 깎지 않는다.
다섯 항목을 따로 판정하고, 규칙을 분명히 어겼을 때만 FAIL이다. FAIL이면 quote에 추천
이유의 문제 구절을 그대로 옮기고 why에 이유를 쓴다. 빠뜨린 것이 문제면 quote는 비우고
why에 무엇이 빠졌는지 쓴다. PASS · NA면 quote와 why를 비운다.

1. faithful — 연구에 대한 말이 '검증된 근거'로 뒷받침된다. 근거 한 건의 저자 · 연도 ·
   제목, '목표 지지' · '개선 지표' 칸, 발췌 중 어디에든 있으면 뒷받침이다 — 발췌가 방법
   부분이어도 '개선 지표'에 있는 지표가 좋아졌다고 말할 수 있다. 목록에 없는 연구 · 저자 ·
   연도를 말하거나, 어디에도 없는 결과 · 수치 · 조건(시간 · 방법)을 지어내거나, 발췌와
   반대로 말하면 FAIL. 뜻이 같은 바꿔 말하기와 상위어(학부생 → 성인)는 지어낸 것이 아니다.
   검증된 근거가 없는데 연구를 인용해도 FAIL.
2. outcomes — 개선됐다고 말한 지표가 그 근거의 '개선 지표'에 있다. 정서(기분) 결과를
   스트레스 · 불안 감소로 바꾸는 등 다른 지표를 목표 지표처럼 말하거나, '목표 지지:
   아니오'인 근거를 효과의 근거처럼 말하면 FAIL. 개선을 말하지 않았으면 PASS.
3. hedged — 효과를 단정하거나 약속하지 않는다(반드시 · 확실히 · 바로 낫는다). 치료 ·
   진단 · 임상 효과를 말하거나, 건강한 성인 연구를 질환 · 환자로 넓히면 FAIL.
4. disclosure — 검증된 근거의 플래그를 뜻으로 밝힌다. ADAPTED_STIMULUS면 제공 콘텐츠가
   연구 자극과 다르다는 것, CONFLICTING_EVIDENCE면 반대 결과도 있다는 것,
   POPULATION_MISMATCH면 임상 표본 결과라는 것, 검증된 근거가 없으면 연구 근거를 찾지
   못했다는 것. 하나라도 빠지면 FAIL. 밝힐 것이 없으면 NA. 사용자 상태의 '이미지 제공'
   줄만으로는 판정하지 않는다 — 이미지를 쓰는 활동이면 근거 플래그에 이미 붙어 있다.
5. personal — 사용자 입력 · 상태에서 시작해 이 활동을 고른 이유를 말하고 근거로 잇는다.
   사용자 상황이 빠진 일반론이면 FAIL.

NA는 disclosure에만 쓴다."""


class ItemJudgment(BaseModel):
    """항목 하나의 판정. 이유는 FAIL일 때만 쓴다(출력 토큰이 Judge 비용의 대부분이다)."""

    verdict: Literal["PASS", "FAIL", "NA"]
    quote: str = Field(description="FAIL이면 추천 이유에서 문제 구절을 그대로, 아니면 빈 문자열")
    why: str = Field(description="FAIL이면 이유 한 문장(한국어), 아니면 빈 문자열")


class RationaleJudgment(BaseModel):
    """Judge의 구조화 출력 — 항목 다섯."""

    faithful: ItemJudgment
    outcomes: ItemJudgment
    hedged: ItemJudgment
    disclosure: ItemJudgment
    personal: ItemJudgment


def judge_input(case: Case) -> str:
    """Judge의 사용자 메시지 — 설명 호출이 본 입력(콘텐츠 Factor 칸 뺌)과 추천 이유."""
    seen = case.explain_input.split(_FACTOR_PART)[0]
    return f"# 플래너가 받은 입력\n\n{seen}\n\n# 추천 이유\n\n{case.rationale}"


def make_judge(model: BaseChatModel) -> Callable[[Case], Awaitable[RationaleJudgment]]:
    structured = model.with_structured_output(RationaleJudgment, method="json_schema")

    async def judge(case: Case) -> RationaleJudgment:
        messages = [SystemMessage(JUDGE_SYSTEM_PROMPT), HumanMessage(judge_input(case))]
        return await structured.ainvoke(messages)

    return judge


def judge_model(settings: Settings, name: str, effort: str) -> BaseChatModel:
    from langchain_openai import ChatOpenAI  # 채점할 때만 읽는다

    return ChatOpenAI(
        model=name,
        api_key=settings.openai_api_key,
        reasoning_effort=effort,
        max_retries=2,
        timeout=180,
    )


async def judge_cases(
    cases: Sequence[Case],
    judge: Callable[[Case], Awaitable[RationaleJudgment]],
    concurrency: int = 6,
) -> list[dict[str, Any]]:
    """케이스마다 규칙 + Judge. Judge 호출이 실패한 케이스는 error를 남기고 집계에서 뺀다."""
    gate = asyncio.Semaphore(concurrency)

    async def one(case: Case) -> dict[str, Any]:
        row = case_row(case)
        async with gate:
            try:
                row["judgment"] = (await judge(case)).model_dump()
            except Exception as exc:  # noqa: BLE001 — 한 건 실패로 전체를 잃지 않는다
                row["error"] = f"{type(exc).__name__}: {exc}"
        return row

    return list(await asyncio.gather(*(one(c) for c in cases)))


def case_row(case: Case) -> dict[str, Any]:
    return {
        "case": case.id,
        "scenario": case.scenario,
        "round": case.round,
        "protocol_id": case.protocol_id,
        "rationale": case.rationale,
        "checks": run_checks(case),
        "judgment": None,
        "error": None,
    }


# --- 집계 ---------------------------------------------------------------------------------------


def summarize(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """규칙 · 항목마다 [통과, 판정 수]. 해당 없음(None · NA)과 Judge 실패는 판정 수에서 뺀다."""
    checks = {}
    for name in CHECKS:
        judged = [r["checks"][name]["ok"] for r in rows if r["checks"][name]["ok"] is not None]
        checks[name] = [sum(judged), len(judged)]
    items = {}
    judged_rows = [r for r in rows if r.get("judgment")]
    for item in ITEMS:
        verdicts = [r["judgment"][item]["verdict"] for r in judged_rows]
        decided = [v for v in verdicts if v != "NA"]
        items[item] = [decided.count("PASS"), len(decided)]
    clean = [
        r
        for r in judged_rows
        if all(c["ok"] is not False for c in r["checks"].values())
        and all(r["judgment"][i]["verdict"] != "FAIL" for i in ITEMS)
    ]
    return {
        "cases": len(rows),
        "judged": len(judged_rows),
        "errors": [r["case"] for r in rows if r.get("error")],
        "checks": checks,
        "items": items,
        "clean": [len(clean), len(judged_rows)],
    }


def failures(row: Mapping[str, Any]) -> list[str]:
    out = [f"{name}({c['detail']})" for name, c in row["checks"].items() if c["ok"] is False]
    for item in ITEMS:
        judged = (row.get("judgment") or {}).get(item)
        if judged and judged["verdict"] == "FAIL":
            quote = f" “{judged['quote']}”" if judged["quote"] else ""
            out.append(f"{item}{quote} — {judged['why']}")
    return out


def format_report(rows: Sequence[Mapping[str, Any]], summary: Mapping[str, Any]) -> str:
    def rates(counts: Mapping[str, Sequence[int]]) -> str:
        return " · ".join(f"{name} {ok}/{n}" for name, (ok, n) in counts.items())

    lines = [f"추천 이유 {summary['cases']}개", f"규칙   {rates(summary['checks'])}"]
    if summary["judged"]:
        ok, n = summary["clean"]
        lines += [f"Judge  {rates(summary['items'])}", f"전부 통과 {ok}/{n}"]
    if summary["errors"]:
        lines.append(f"Judge 실패 {len(summary['errors'])}건: {', '.join(summary['errors'])}")
    failed = [(r, failures(r)) for r in rows]
    if any(f for _, f in failed):
        lines.append("\n어긴 것")
    for row, found in failed:
        for line in found:
            lines.append(f"- {row['case']} {row['protocol_id']}: {line}")
    return "\n".join(lines)


# --- 사람 라벨과의 일치 ---------------------------------------------------------------------------

_LABEL = {"PASS": "PASS", "O": "PASS", "FAIL": "FAIL", "X": "FAIL", "NA": "NA", "-": "NA"}
SHEET_COLUMNS = ("case", "item", "judge", "quote", "why", "rationale", "input", "human")


def review_rows(
    rows: Sequence[Mapping[str, Any]],
    cases: Mapping[str, Case],
    pass_sample: int = 3,
    seed: int = 0,
) -> list[dict[str, str]]:
    """사람이 확인할 줄 — Judge FAIL 전부와 항목마다 PASS 몇 개(무작위, seed 고정).

    FAIL을 다 보면 Judge가 문제를 잘못 잡는지(정밀도)를, PASS 표본은 놓치는지를 본다. input은
    플래너가 본 입력(Judge 입력과 같다)이고, human 칸을 비워 둔다.
    """
    rng = random.Random(seed)
    judged = [r for r in rows if r.get("judgment")]
    picked = []
    for item in ITEMS:
        fails = [r for r in judged if r["judgment"][item]["verdict"] == "FAIL"]
        passes = [r for r in judged if r["judgment"][item]["verdict"] == "PASS"]
        picked += [(r, item) for r in fails]
        picked += [(r, item) for r in rng.sample(passes, min(pass_sample, len(passes)))]
    out = []
    for row, item in picked:
        verdict = row["judgment"][item]
        case = cases.get(row["case"])
        seen = case.explain_input.split(_FACTOR_PART)[0] if case else ""
        out.append(
            {
                "case": row["case"],
                "item": item,
                "judge": verdict["verdict"],
                "quote": verdict["quote"],
                "why": verdict["why"],
                "rationale": row["rationale"],
                "input": seen,
                "human": "",
            }
        )
    return out


def load_labels(path: Path) -> dict[tuple[str, str], str]:
    """CSV(case,item,human) → {(case, item): PASS · FAIL · NA}. human이 빈 줄은 건너뛴다."""
    labels = {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            human = (row.get("human") or "").strip().upper()
            if not human:
                continue
            if human not in _LABEL or row["item"] not in ITEMS:
                raise ValueError(f"라벨을 읽을 수 없다: {row}")
            labels[(row["case"], row["item"])] = _LABEL[human]
    return labels


def agreement(
    rows: Iterable[Mapping[str, Any]], labels: Mapping[tuple[str, str], str]
) -> tuple[dict[str, list[int]], list[str]]:
    """항목마다 [일치, 라벨 수]와 어긋난 것 목록. Judge 판정이 없는 케이스의 라벨은 세지 않는다."""
    verdicts = {
        (r["case"], item): r["judgment"][item]["verdict"]
        for r in rows
        if r.get("judgment")
        for item in ITEMS
    }
    counts = {item: [0, 0] for item in ITEMS}
    mismatches = []
    for (case, item), human in sorted(labels.items()):
        judged = verdicts.get((case, item))
        if judged is None:
            continue
        counts[item][1] += 1
        if judged == human:
            counts[item][0] += 1
        else:
            mismatches.append(f"- {case} {item}: Judge {judged} · 사람 {human}")
    return counts, mismatches


# --- 실행 ---------------------------------------------------------------------------------------


def check(args: argparse.Namespace) -> int:
    rows = [case_row(c) for c in load_cases(args.results)]
    print(format_report(rows, summarize(rows)))
    return 0


async def judge(args: argparse.Namespace) -> int:
    settings = get_settings()
    if not settings.openai_api_key:
        print("LLM 키가 없다 — .env의 OPENAI_API_KEY", file=sys.stderr)
        return 2
    generators = {json.loads(p.read_text(encoding="utf-8"))["meta"]["model"] for p in args.results}
    if args.model in generators:
        print(f"경고: Judge {args.model}가 생성 모델과 같다 — 자기 글에 후하다", file=sys.stderr)
    cases = load_cases(args.results)
    model = judge_model(settings, args.model, args.effort)
    usage = Usage()
    model.callbacks = [usage]
    started = time.perf_counter()
    rows = await judge_cases(cases, make_judge(model), args.concurrency)
    summary = summarize(rows)
    print(format_report(rows, summary))
    print(
        f"\nJudge {args.model}({args.effort}) · 생성 {', '.join(sorted(generators))} · "
        f"{time.perf_counter() - started:.0f}s · 토큰 {usage.input:,}/{usage.output:,}"
    )
    if args.out:
        payload = {
            "meta": {
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "judge": args.model,
                "effort": args.effort,
                "generators": sorted(generators),
                "results": [str(p) for p in args.results],
                "tokens": [usage.input, usage.output],
            },
            "summary": summary,
            "cases": rows,
        }
        args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"결과: {args.out}")
    return 0


def load_judged(paths: Sequence[Path]) -> tuple[list[dict[str, Any]], list[int]]:
    """judge --out 결과 여러 개 → 케이스를 합치고 Judge 토큰(입력 · 출력)을 더한다."""
    rows, tokens = [], [0, 0]
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows += payload["cases"]
        tokens = [t + u for t, u in zip(tokens, payload["meta"]["tokens"], strict=True)]
    return rows, tokens


def sheet(args: argparse.Namespace) -> int:
    rows, _ = load_judged(args.judged)
    results = {
        Path(p)
        for path in args.judged
        for p in json.loads(path.read_text(encoding="utf-8"))["meta"]["results"]
    }
    cases = {c.id: c for c in load_cases(sorted(results))}
    review = review_rows(rows, cases, args.pass_sample)
    with args.out.open("w", encoding="utf-8-sig", newline="") as f:  # 엑셀에서 한글이 깨지지 않게
        writer = csv.DictWriter(f, fieldnames=SHEET_COLUMNS)
        writer.writeheader()
        writer.writerows(review)
    fails = sum(r["judge"] == "FAIL" for r in review)
    passes = len(review) - fails
    print(f"확인할 줄 {len(review)}개(Judge FAIL {fails} · PASS 표본 {passes}) → {args.out}")
    print("human 칸에 PASS · FAIL · NA(또는 O · X · -)를 적고 agree로 일치를 본다")
    return 0


def _cell(text: str) -> str:
    """마크다운 표 칸 — 세로줄 · 줄바꿈을 막는다."""
    return text.replace("|", "\\|").replace("\n", " ") or "—"


def record_markdown(
    title: str,
    rows: Sequence[Mapping[str, Any]],
    summary: Mapping[str, Any],
    judges: Sequence[Mapping[str, Any]],
    runs: Sequence[Mapping[str, Any]],
    rechecked: bool,
) -> str:
    """실행 기록(docs/rag-eval/YYYY-MM-DD-<주제>.md) — 구성 · 규칙 · Judge 항목 분포 · 전부 통과 ·
    어긴 것 · Judge 실패. judges는 judge --out의 meta, runs는 plan eval 결과의 meta · summary다.
    해석은 같은 디렉터리의 판정 메모(-verdict.md)에 사람이 쓴다."""
    lines = [f"# 추천 이유 평가 — {title}", ""]
    for run in runs:
        meta, counts = run["meta"], run["summary"]
        seed = meta.get("seed") or "등급 순"
        lines.append(
            f"- 생성: `{meta['model']}` · 후보 순서 시드 `{seed}` · 근거 DB `{meta['db']}` · "
            f"계획 {counts['runs']}개(시나리오 {counts['scenarios']} + 활동 변경 "
            f"{counts['change_rounds']}) · {meta['at']}"
        )
    for meta in judges:
        lines.append(
            f"- 심판: `{meta['judge']}`(추론 {meta['effort']}) · 토큰 "
            f"{meta['tokens'][0]:,}/{meta['tokens'][1]:,} · {meta['at']}"
        )
    if rechecked:
        lines.append(
            "- 규칙은 이 기록을 만든 코드의 규칙(`app/agent/rationale_rules.py`)으로 다시 쟀다"
        )
    lines += ["", "## 규칙 (코드)", "", "| 규칙 | 통과 | 판정 |", "| --- | --- | --- |"]
    lines += [f"| {name} | {ok} | {n} |" for name, (ok, n) in summary["checks"].items()]
    judged = [r for r in rows if r.get("judgment")]
    lines += ["", "## Judge 항목", "", "| 항목 | PASS | FAIL | NA |", "| --- | --- | --- | --- |"]
    for item in ITEMS:
        verdicts = [r["judgment"][item]["verdict"] for r in judged]
        counts = " | ".join(str(verdicts.count(v)) for v in ("PASS", "FAIL", "NA"))
        lines.append(f"| {item} | {counts} |")
    ok, n = summary["clean"]
    lines += ["", "## 전부 통과", "", f"규칙과 Judge 항목을 모두 통과한 추천 이유 {ok}/{n}", ""]
    broken = []
    for row in rows:
        for name, result in row["checks"].items():
            if result["ok"] is False:
                broken.append((row, name, result["detail"], "규칙"))
        for item in ITEMS:
            verdict = (row.get("judgment") or {}).get(item)
            if verdict and verdict["verdict"] == "FAIL":
                broken.append((row, item, verdict["quote"], verdict["why"]))
    lines += [f"## 어긴 것 — {len(broken)}건", ""]
    if broken:
        lines += ["| 계획 | 활동 | 항목 | 문제 구절 | 이유 |", "| --- | --- | --- | --- | --- |"]
        for row, name, quote, why in broken:
            lines.append(
                f"| {row['case']} | {row['protocol_id']} | {name} | {_cell(quote)} | {_cell(why)} |"
            )
    else:
        lines.append("없음.")
    errors = [r for r in rows if r.get("error")]
    lines += ["", f"## Judge 실패 — {len(errors)}건", ""]
    lines += [f"- {r['case']}: {_cell(r['error'])}" for r in errors] or ["없음."]
    return "\n".join(lines) + "\n"


def report(args: argparse.Namespace) -> int:
    """judge 결과를 합쳐 집계한다. plan eval 결과 파일이 남아 있으면 규칙은 지금 코드로 다시 잰다
    — 규칙을 고친 뒤에도 같은 추천 이유를 새 규칙으로 볼 수 있다."""
    rows, tokens = load_judged(args.judged)
    judges = [json.loads(p.read_text(encoding="utf-8"))["meta"] for p in args.judged]
    results = [Path(p) for meta in judges for p in meta["results"]]
    rechecked = bool(results) and all(p.exists() for p in results)
    runs = []
    if rechecked:
        cases = {c.id: c for c in load_cases(results)}
        for row in rows:
            if row["case"] in cases:
                row["checks"] = run_checks(cases[row["case"]])
        runs = [json.loads(p.read_text(encoding="utf-8")) for p in results]
    summary = summarize(rows)
    print(format_report(rows, summary))
    print(f"\nJudge 토큰 {tokens[0]:,}/{tokens[1]:,}")
    if args.md:
        title = args.title or args.md.stem
        record = record_markdown(title, rows, summary, judges, runs, rechecked)
        args.md.write_text(record, encoding="utf-8")
        print(f"실행 기록: {args.md}")
    return 0


def agree(args: argparse.Namespace) -> int:
    rows, _ = load_judged(args.judged)
    counts, mismatches = agreement(rows, load_labels(args.labels))
    print("일치  " + " · ".join(f"{item} {ok}/{n}" for item, (ok, n) in counts.items()))
    total = [sum(c[i] for c in counts.values()) for i in (0, 1)]
    print(f"전체  {total[0]}/{total[1]}")
    if mismatches:
        print("\n어긋난 것\n" + "\n".join(mismatches))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check", help="규칙(코드)만 채점한다(LLM 없음)")
    c.add_argument("results", type=Path, nargs="+", help="plan_eval.py --out 결과")
    j = sub.add_parser("judge", help="규칙 + LLM Judge로 채점한다(LLM 비용)")
    j.add_argument("results", type=Path, nargs="+", help="plan_eval.py --out 결과")
    j.add_argument("--model", default=JUDGE_MODEL)
    j.add_argument("--effort", default=JUDGE_EFFORT, choices=["low", "medium", "high"])
    j.add_argument("--concurrency", type=int, default=6)
    j.add_argument("--out", type=Path, help="결과 JSON(report · agree용)")
    r = sub.add_parser("report", help="judge --out 결과 여러 개를 합쳐 집계한다")
    r.add_argument("judged", type=Path, nargs="+", help="judge --out 결과")
    r.add_argument("--md", type=Path, help="실행 기록 마크다운(docs/rag-eval/YYYY-MM-DD-<주제>.md)")
    r.add_argument("--title", help="실행 기록 제목. 없으면 파일 이름")
    s = sub.add_parser("sheet", help="사람이 확인할 CSV — Judge FAIL 전부 + 항목마다 PASS 표본")
    s.add_argument("judged", type=Path, nargs="+", help="judge --out 결과")
    s.add_argument("--out", type=Path, required=True, help="CSV(엑셀용 UTF-8 BOM)")
    s.add_argument("--pass-sample", type=int, default=3, help="항목마다 PASS 표본 수")
    a = sub.add_parser("agree", help="사람 라벨과 Judge 판정의 일치")
    a.add_argument("judged", type=Path, nargs="+", help="judge --out 결과")
    a.add_argument("labels", type=Path, help="CSV: case,item,human")
    args = parser.parse_args()

    if args.command == "check":
        return check(args)
    if args.command == "report":
        return report(args)
    if args.command == "sheet":
        return sheet(args)
    if args.command == "agree":
        return agree(args)
    return asyncio.run(judge(args))


if __name__ == "__main__":
    raise SystemExit(main())
