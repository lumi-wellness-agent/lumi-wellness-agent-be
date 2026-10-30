"""RAG 사용 vs LLM 단독 vs LLM + 웹 검색 — 추천 이유의 근거 제시를 같은 자로 비교한다.

계획 평가 결과(scripts/plan_eval.py --out)의 계획마다 활동은 LUMI가 고른 것으로 고정하고, "이
사용자에게 이 활동을 왜 권하는지 연구 근거를 들어" 쓰게 한다. LUMI에서 RAG가 맡은 일이 이 근거
부분이라 이 부분만 비교한다. 생성 모델(LLM_MODEL)은 같다 — 차이는 근거를 어디서 가져오는지뿐이다.

    .venv/bin/python scripts/rag_compare.py import-rag a.json --out rag.json --database-url <URL>
    .venv/bin/python scripts/rag_compare.py generate a.json --mode llm --repeat 3 --out llm.json
    .venv/bin/python scripts/rag_compare.py generate a.json --mode web --out web.json
    .venv/bin/python scripts/rag_compare.py verify rag.json llm.json web.json   # LLM 없음
    .venv/bin/python scripts/rag_compare.py judge rag.json --out judged_rag.json  # gpt-5.5
    .venv/bin/python scripts/rag_compare.py report judged_rag.json judged_llm.json \\
        judged_web.json --md docs/rag-eval/2026-10-30-rag-compare.md
    .venv/bin/python scripts/rag_compare.py sheet judged_rag.json judged_llm.json --out review.csv
    .venv/bin/python scripts/rag_compare.py agree judged_llm.json review.csv

- 비교군. rag — plan eval의 추천 이유 그대로(근거는 검증된 레코드). llm — 같은 입력에서 근거 목록만
  빼고 모델이 아는 연구를 인용한다. web — 같은 입력에 OpenAI web_search 도구(Responses API)를 준다.
  llm · web의 입력은 설명 호출이 본 사용자 입력 · 상태 · 고른 활동 이름뿐이다(근거 · 꼭 밝힐 것 ·
  콘텐츠 Factor 칸은 뺀다).
- 인용(mention). 추천 이유 문장의 '저자(연도)'를 코드가 찾는다 — 세 비교군에 같은 정규식이다. rag는
  근거 레코드(papers 표)의 제목 · DOI에, llm · web은 모델이 구조화 출력으로 함께 낸 인용 목록(제목 ·
  URL)에 잇는다. '저자(연도)' 없이 '연구에 따르면'만 있는 문장은 출처 없는 연구 주장으로 따로 센다.
- 실존(verify). OpenAlex(DOI → 제목 검색, 없으면 Crossref)로 찾아 FOUND(제목 · 연도 · 1저자
  일치) · MISMATCH(논문은 있으나 연도 · 저자가 다름 — 서지 오류) · NOT_FOUND로 가른다. 초록은
  OpenAlex, 없으면 Europe PMC. web은 URL도 받아 응답 · 도메인 유형(학술 · 공공 · 언론 · 블로그 ·
  상업) · 본문을 남기고, PubMed · DOI 링크는 OpenAlex로 초록을 받는다.
- Judge(gpt-5.5, 생성 모델과 다른 모델). 인용마다 주장 문장이 출처 텍스트(초록 · 웹은 본문)로
  뒷받침되는지(SUPPORTED · NOT_SUPPORTED · UNVERIFIABLE)와 연구 조건이 사용자 상태(사건 전 · 후 ·
  피로 후)에 맞는지(MATCH · MISMATCH · UNKNOWN), 추천 이유 단위로 hedged · personal(추천 이유
  평가와 같은 정의). rag도 운영이 보는 발췌가 아니라 같은 초록 기준이라 유리하지 않다. 찾지 못한
  인용은 Judge에 보내지 않고 UNVERIFIABLE이다. Judge 숫자는 sheet · agree로 사람 라벨과 맞춘 뒤
  쓴다.
- 비용. OpenAI 공식 가격(아래 상수, 2026-10-30)으로 추천 이유 한 건의 달러를 센다. rag는 plan eval의
  토큰(선택 + 설명 두 호출)이라 설명 한 호출인 llm · web보다 크게 잡힌다.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import difflib
import html
import json
import re
import statistics
import sys
import time
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402

MODES = ("rag", "llm", "web")
JUDGE_MODEL = "gpt-5.5"
JUDGE_EFFORT = "medium"
# USD / 1M 토큰 (입력, 캐시 입력, 출력) — https://developers.openai.com/api/docs/pricing 2026-10-30
PRICES = {
    "gpt-5.4-mini": (0.75, 0.075, 4.50),
    "gpt-5.4": (2.50, 0.25, 15.00),
    "gpt-5.5": (5.00, 0.50, 30.00),
}
WEB_SEARCH_USD_PER_CALL = 0.01  # $10 / 1k calls
OPENALEX = "https://api.openalex.org"
CROSSREF = "https://api.crossref.org/works"
EUROPEPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
SEMANTIC_SCHOLAR = "https://api.semanticscholar.org/graph/v1/paper"
USER_AGENT = "lumi-rag-compare/1 (research evaluation script)"
TITLE_MATCH = 0.75
SOURCE_TEXT_LIMIT = 2500  # Judge에 보내는 출처 텍스트 글자 수
PAGE_TEXT_LIMIT = 5000

SupportVerdict = Literal["SUPPORTED", "NOT_SUPPORTED", "UNVERIFIABLE"]
ConditionVerdict = Literal["MATCH", "MISMATCH", "UNKNOWN"]
PassFail = Literal["PASS", "FAIL"]


# --- 입력(계획 평가 결과) ------------------------------------------------------------------------

_SECTION = re.compile(r"^## (.+)$", re.MULTILINE)


def sections(explain_input: str) -> dict[str, str]:
    """설명 호출의 사용자 메시지를 '## 제목' 단위로 나눈다."""
    out: dict[str, str] = {}
    marks = list(_SECTION.finditer(explain_input))
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(explain_input)
        out[mark.group(1).strip()] = explain_input[mark.end() : end].strip()
    return out


def compare_input(run: Mapping[str, Any]) -> str:
    """llm · web의 사용자 메시지 — 설명 호출이 본 사용자 입력 · 상태 · 고른 활동(이름만).
    '이미지 제공' 줄(자극 변형 고지 — LUMI만 아는 정보)과 '고른 이유'(선택 호출의 출력)는 뺀다."""
    parts = sections(run["explain_input"] or "")
    state = [
        line
        for line in parts.get("사용자 상태", "").splitlines()
        if not line.startswith("- 이미지 제공") and not line.startswith("- 요약")
    ]
    activity = parts.get("고른 활동", "").splitlines()[:1]
    activity = [re.sub(r" — 구성 .*$", "", line) for line in activity]
    return "\n\n".join(
        [
            "## 사용자 입력\n" + parts.get("사용자 입력", ""),
            "## 사용자 상태\n" + "\n".join(state),
            "## 고른 활동\n" + "\n".join(activity),
        ]
    )


def case_id(run: Mapping[str, Any]) -> str:
    return f"{run['scenario']}{'+change' if run.get('round') else ''}"


def load_plan_eval(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [r for r in payload["runs"] if r.get("rationale")]


# --- 생성 (llm · web) ----------------------------------------------------------------------------

GENERATE_SYSTEM_PROMPT = """\
너는 웰니스 앱의 케어 세션 플래너다. 이미 고른 활동을 사용자에게 권하는 이유를 한국어 2~4문장으로
쓴다. 순서는 사용자 상태 → 이 활동을 고른 이유 → 연구 근거(저자(연도)와 개선이 확인된 지표).

- 연구 근거를 반드시 든다. 본문에는 '저자(연도)' 형식으로 인용하고(예: Kim & Lee (2019)), 인용한
  연구마다 citations에 저자 표기 · 연도 · 원제를 적는다. 본문에 인용한 연구와 citations는 일치해야
  한다.
- 한국어로만 쓰고 저자 이름만 원문(라틴 문자)으로 둔다. 통계값(p · g · SMD)은 쓰지 않는다.
- 치료 효과를 단정하지 않고, 건강한 성인 대상 결과를 임상 효과로 넓히지 않는다.
- 활동 ID나 내부 코드는 쓰지 않는다."""

WEB_EXTRA = """
- 웹 검색으로 근거를 찾아 인용한다. citations의 url에는 그 근거를 본 페이지 주소를 적는다."""


class Citation(BaseModel):
    authors: str = Field(description="본문에 쓴 저자 표기 그대로(예: Kim & Lee, Smith et al.)")
    year: int
    title: str = Field(description="연구의 원제(영문 원제가 있으면 영문)")
    url: str = Field(default="", description="근거를 본 페이지 주소. 없으면 빈 문자열")
    doi: str = Field(default="", description="알면 DOI, 모르면 빈 문자열")


class Generated(BaseModel):
    rationale: str
    citations: list[Citation]


def price_usd(model: str, usage: Mapping[str, int], web_calls: int = 0) -> float:
    inp, cached, out = PRICES.get(model, PRICES["gpt-5.4-mini"])
    paid_in = usage.get("input", 0) - usage.get("cached", 0)
    return (
        paid_in * inp + usage.get("cached", 0) * cached + usage.get("output", 0) * out
    ) / 1_000_000 + web_calls * WEB_SEARCH_USD_PER_CALL


def usage_of(response: Any) -> dict[str, int]:
    u = response.usage
    details_in = getattr(u, "input_tokens_details", None)
    details_out = getattr(u, "output_tokens_details", None)
    return {
        "input": u.input_tokens,
        "cached": getattr(details_in, "cached_tokens", 0) or 0,
        "output": u.output_tokens,
        "reasoning": getattr(details_out, "reasoning_tokens", 0) or 0,
    }


async def generate_one(
    client: Any, model: str, mode: str, run: Mapping[str, Any]
) -> dict[str, Any]:
    """추천 이유 한 건 — Responses API 구조화 출력. web은 web_search 도구를 붙인다."""
    user = compare_input(run)
    instructions = GENERATE_SYSTEM_PROMPT + (WEB_EXTRA if mode == "web" else "")
    kwargs: dict[str, Any] = {}
    if mode == "web":
        kwargs["tools"] = [{"type": "web_search"}]
    started = time.perf_counter()
    response = await client.responses.parse(
        model=model, instructions=instructions, input=user, text_format=Generated, **kwargs
    )
    seconds = time.perf_counter() - started
    parsed: Generated = response.output_parsed
    searches, annotations, web_calls = [], [], 0
    for item in response.output:
        if item.type == "web_search_call":
            web_calls += 1
            action = getattr(item, "action", None)
            queries = getattr(action, "queries", None) or [getattr(action, "query", None)]
            searches += [q for q in queries if q]
        elif item.type == "message":
            for part in item.content:
                for a in getattr(part, "annotations", None) or []:
                    if getattr(a, "type", "") == "url_citation":
                        annotations.append({"url": a.url, "title": getattr(a, "title", "")})
    usage = usage_of(response)
    return {
        "input": user,
        "rationale": parsed.rationale,
        "citations": [c.model_dump() for c in parsed.citations],
        "searches": searches,
        "annotations": _dedupe(annotations),
        "web_calls": web_calls,
        "tokens": usage,
        "seconds": seconds,
        "cost_usd": price_usd(model, usage, web_calls),
    }


def _dedupe(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen, out = set(), []
    for row in rows:
        key = row.get("url") or row.get("doi") or row.get("title")
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(row))
    return out


async def generate(args: argparse.Namespace) -> int:
    from openai import AsyncOpenAI  # 생성할 때만 읽는다

    settings = get_settings()
    if not settings.openai_api_key:
        print("OPENAI_API_KEY가 없다", file=sys.stderr)
        return 2
    model = args.model or settings.llm_model
    client = AsyncOpenAI(api_key=settings.openai_api_key, max_retries=2, timeout=180)
    plans = [r for r in load_plan_eval(args.plan_eval) if not args.only or case_id(r) in args.only]
    gate = asyncio.Semaphore(args.concurrency)
    rows: list[dict[str, Any]] = []

    async def one(run: Mapping[str, Any], repeat: int) -> None:
        row = {
            "case": case_id(run),
            "scenario": run["scenario"],
            "round": run.get("round", 0),
            "protocol_id": run["protocol_id"],
            "repeat": repeat,
            "error": None,
        }
        async with gate:
            try:
                row.update(await generate_one(client, model, args.mode, run))
            except Exception as exc:  # noqa: BLE001 — 한 건 실패로 전체를 잃지 않는다
                row["error"] = f"{type(exc).__name__}: {exc}"
                row["input"] = compare_input(run)
        rows.append(row)
        mark = "✗" if row["error"] else f"{len(row.get('citations', []))}건 · {row['seconds']:.1f}s"
        print(f"{row['case']:22s} r{repeat} {row['protocol_id']} {mark}", flush=True)

    await asyncio.gather(*(one(r, k) for r in plans for k in range(args.repeat)))
    rows.sort(key=lambda r: (r["scenario"], r["round"], r["repeat"]))
    ok = [r for r in rows if not r["error"]]
    cost = sum(r["cost_usd"] for r in ok)
    tokens = (sum(r["tokens"]["input"] for r in ok), sum(r["tokens"]["output"] for r in ok))
    payload = {
        "meta": {
            "mode": args.mode,
            "model": model,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "plan_eval": str(args.plan_eval),
            "repeat": args.repeat,
            "web_calls": sum(r.get("web_calls", 0) for r in ok),
            "tokens": tokens,
            "cost_usd": cost,
        },
        "runs": rows,
    }
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(
        f"\n{args.mode} · {model} · {len(ok)}/{len(rows)}건 · 토큰 {tokens[0]:,}/{tokens[1]:,}"
        f" · 웹 검색 {payload['meta']['web_calls']}회 · ${cost:.3f} → {args.out}"
    )
    return 0


# --- rag 가져오기 --------------------------------------------------------------------------------


async def import_rag(args: argparse.Namespace) -> int:
    """plan eval 결과 → generate와 같은 모양. 인용 목록은 설명 호출이 받은 근거의 논문(papers
    표)이다."""
    from app.db import create_pool  # DB를 쓸 때만 읽는다

    settings = get_settings()
    url = args.database_url or settings.database_url
    if not url:
        print("DB가 없다 — --database-url 또는 .env의 DATABASE_URL", file=sys.stderr)
        return 2
    payload = json.loads(args.plan_eval.read_text(encoding="utf-8"))
    plans = [r for r in payload["runs"] if r.get("rationale")]
    paper_ids = sorted({e["key"].split("-")[0] for r in plans for e in r["evidence"]})
    pool = create_pool(url)
    await pool.open()
    try:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "select id, doi, title, authors, year from papers where id = any(%s)", (paper_ids,)
            )
            papers = {row[0]: row for row in await cur.fetchall()}
    finally:
        await pool.close()
    model = payload["meta"]["model"]
    rows = []
    for run in plans:
        seen, citations = set(), []
        for e in run["evidence"]:
            paper_id = e["key"].split("-")[0]
            if paper_id in seen or paper_id not in papers:
                continue
            seen.add(paper_id)
            _, doi, title, authors, year = papers[paper_id]
            citations.append(
                {"authors": authors or paper_id, "year": year, "title": title, "url": "",
                 "doi": doi or "", "paper_id": paper_id}
            )
        tokens = {"input": run["tokens"][0], "cached": 0, "output": run["tokens"][1]}
        rows.append(
            {
                "case": case_id(run),
                "scenario": run["scenario"],
                "round": run.get("round", 0),
                "protocol_id": run["protocol_id"],
                "repeat": 0,
                "error": None,
                "input": compare_input(run),
                "rationale": run["rationale"],
                "citations": citations,
                "searches": [],
                "annotations": [],
                "web_calls": 0,
                "tokens": tokens,
                "seconds": run["seconds"],
                "cost_usd": price_usd(model, tokens),
                "badges": run.get("badges", []),
            }
        )
    out = {
        "meta": {
            "mode": "rag",
            "model": model,
            "at": payload["meta"]["at"],
            "plan_eval": str(args.plan_eval),
            "repeat": 1,
            "web_calls": 0,
            "tokens": [
                sum(r["tokens"]["input"] for r in rows),
                sum(r["tokens"]["output"] for r in rows),
            ],
            "cost_usd": sum(r["cost_usd"] for r in rows),
            "note": "토큰 · 비용은 plan 노드 한 번(선택 + 설명 두 호출)이다",
        },
        "runs": rows,
    }
    args.out.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"rag · {len(rows)}건 · 논문 {len(papers)}개 → {args.out}")
    return 0


# --- 인용 찾기(코드) ------------------------------------------------------------------------------

_PAREN = re.compile(r"\(([^()]*)\)")
_YEAR_ONLY = re.compile(r"^\s*((?:19|20)\d{2})[a-z]?\s*$")
_YEAR_IN = re.compile(r"^(.*?),?\s*((?:19|20)\d{2})[a-z]?\b")
_LATIN_WORD = re.compile(r"[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ'’\-]*")
# '(연도)' 바로 앞의 저자 목록 — 라틴 낱말을 쉼표 · & · and · 과 · 와 · 등으로 이은 꼬리
_AUTHORS_TAIL = re.compile(
    r"((?:[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ'’\-\.]*)"
    r"(?:(?:\s*,\s*|\s*&\s*|\s+and\s+|\s+|과\s*|와\s*|\s*등\s*)[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ'’\-\.]*)*)"
    r"\s*(?:등|과|와)?\s*$"
)
_AUTHOR_STOP = {"et", "al", "and", "etal"}
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[가-힣A-Za-zÀ-ɏ\"'‘“])")
_STUDY_WORDS = re.compile(r"연구|메타분석|메타 분석|실험|리뷰|논문|체계적 고찰|보고(?:됐|되었|했)")
# 근거 없음 · 한계 고지 — 주장이 아니다. '다만' 없이 쓴 자극 · AI 이미지 고지("AI 생성 이미지는
# 연구에서 사용한 사진과는 다릅니다")도 뺀다(2026-10-30 재측정 S1-daily-b)
_DISCLOSURE = re.compile(r"^다만|찾지 못했|확인되지 않았|연구에서 (?:쓴|사용한) (?:사진|자극)")


def fold(text: str) -> str:
    """악센트를 떼고 소문자로 — 저자 이름 대조용."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    ).lower()


def surnames_in(text: str) -> list[str]:
    return [
        w.strip(".")
        for w in _LATIN_WORD.findall(text)
        if w[0].isupper() and fold(w.strip(".")) not in _AUTHOR_STOP
    ]


def sentences_of(text: str) -> list[tuple[int, int, str]]:
    spans, start = [], 0
    for m in _SENT_SPLIT.finditer(text):
        spans.append((start, m.start(), text[start : m.start()].strip()))
        start = m.end()
    spans.append((start, len(text), text[start:].strip()))
    return [s for s in spans if s[2]]


def find_mentions(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """'저자 (연도)'와 괄호 안 '(저자, 연도; 저자, 연도)' 인용, 그리고 인용 없이 연구를 말한 문장
    (한계 · 근거 없음 고지는 뺀다)."""
    spans = sentences_of(text)
    mentions = []
    for paren in _PAREN.finditer(text):
        sentence = next((s[2] for s in spans if s[0] <= paren.start() < s[1]), text)
        inner = paren.group(1)
        only = _YEAR_ONLY.match(inner)
        if only:
            window = text[max(0, paren.start() - 90) : paren.start()]
            tail = _AUTHORS_TAIL.search(window)
            found = [(tail.group(1).strip() if tail else "", int(only.group(1)))]
        else:
            found = []
            for segment in inner.split(";"):
                hit = _YEAR_IN.match(segment.strip())
                if hit:
                    found.append((hit.group(1).strip(" ,"), int(hit.group(2))))
        for authors, year in found:
            mentions.append(
                {
                    "authors": authors,
                    "surnames": surnames_in(authors),
                    "year": year,
                    "sentence": sentence,
                }
            )
    cited_sentences = {m["sentence"] for m in mentions}
    unattributed = [
        s[2]
        for s in spans
        if s[2] not in cited_sentences and _STUDY_WORDS.search(s[2])
        and not _DISCLOSURE.search(s[2])
    ]
    return mentions, unattributed


def resolve(mention: Mapping[str, Any], citations: Sequence[Mapping[str, Any]]) -> dict | None:
    """인용을 모델이 낸(또는 근거 레코드의) 인용 목록에 잇는다 — 연도가 같고 성이 저자 표기에
    있는 것, 없으면 그 연도의 인용이 하나뿐일 때 그것."""
    same_year = [c for c in citations if c.get("year") == mention["year"]]
    for c in same_year:
        folded = fold(c.get("authors") or "")
        if any(fold(s) in folded for s in mention["surnames"]):
            return dict(c)
    if len(same_year) == 1 and not mention["surnames"]:
        return dict(same_year[0])
    if len(same_year) == 1 and len(citations) == 1:
        return dict(same_year[0])
    return None


# --- 실존 확인 (OpenAlex · Crossref · Europe PMC · 웹) --------------------------------------------


def norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9가-힣 ]", " ", fold(title or "")).strip()


def title_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, norm_title(a), norm_title(b)).ratio()


# 제목 낱말 겹침에서 빼는 말 — 어떤 논문 제목에나 흔해 겹쳐도 같은 논문이라는 증거가 아니다
_GENERIC = {
    "the", "a", "an", "of", "on", "in", "and", "or", "for", "to", "with", "by", "from", "at",
    "among", "between", "versus", "vs", "effect", "effects", "impact", "role", "study", "studies",
    "review", "systematic", "meta", "analysis", "randomized", "randomised", "controlled", "trial",
    "trials", "evidence", "based", "intervention", "interventions", "health", "benefits", "benefit",
    "psychological", "does", "do", "can", "is", "are", "its", "their", "using", "use", "toward",
    "towards", "new", "two", "brief", "short", "long", "term", "adults", "adult", "people",
    "participants", "healthy", "effectiveness", "efficacy", "outcomes", "outcome", "measures",
    "physiological", "responses", "response", "behavioural", "behavioral", "pilot",
}


def title_overlap(a: str, b: str) -> tuple[float, int]:
    """두 제목의 내용 낱말(앞 6글자로 어간 비슷하게) 겹침 — (겹친 수 / 짧은 쪽 낱말 수, 겹친 수).
    저자 · 연도만으로 찾은 논문이 인용과 같은 논문인지 가르는 데 쓴다 — 문자열 유사도는 'The effect
    of … : A meta-analysis' 같은 틀만 같아도 높게 나온다."""
    def stems(title: str) -> set[str]:
        return {
            w[:6] for w in norm_title(title).split() if len(w) >= 3 and w not in _GENERIC
        }

    sa, sb = stems(a), stems(b)
    if not sa or not sb:
        return 0.0, 0
    shared = len(sa & sb)
    return shared / min(len(sa), len(sb)), shared


# 저자 · 연도로 찾은 논문을 서지 오류로 인정하는 제목 겹침(겹친 낱말 2개 이상)
AUTHOR_TITLE_OVERLAP = 0.4


def abstract_from_index(index: Mapping[str, Sequence[int]] | None) -> str | None:
    if not index:
        return None
    words: dict[int, str] = {}
    for word, positions in index.items():
        for p in positions:
            words[p] = word
    return " ".join(words[i] for i in sorted(words))


def _openalex_record(work: Mapping[str, Any]) -> dict[str, Any]:
    source = (work.get("primary_location") or {}).get("source") or {}
    return {
        "id": work.get("id"),
        "doi": (work.get("doi") or "").replace("https://doi.org/", ""),
        "title": work.get("display_name") or "",
        "year": work.get("publication_year"),
        "authors": [a["author"]["display_name"] for a in work.get("authorships", [])],
        "venue": source.get("display_name"),
        "abstract": abstract_from_index(work.get("abstract_inverted_index")),
    }


_OA_SELECT = (
    "id,doi,display_name,publication_year,authorships,abstract_inverted_index,primary_location"
)


class Resolver:
    """OpenAlex · Crossref · Europe PMC 조회(결과를 기억한다)와 웹 페이지 가져오기."""

    def __init__(self, client: httpx.AsyncClient, concurrency: int = 4):
        self.client = client
        self.gate = asyncio.Semaphore(concurrency)
        self._s2_gate = asyncio.Semaphore(1)
        self.memo: dict[str, Any] = {}
        self.exhausted: set[str] = set()  # 하루 할당량이 끝난 호스트

    async def _get_json(self, url: str, params: Mapping[str, Any] | None = None) -> Any:
        key = url + json.dumps(params or {}, sort_keys=True)
        if key in self.memo:
            return self.memo[key]
        host = urlparse(url).hostname or ""
        if host in self.exhausted:
            return None
        async with self.gate:
            for attempt in range(3):
                try:
                    r = await self.client.get(url, params=params)
                    if r.status_code == 404:
                        self.memo[key] = None
                        return None
                    if r.status_code == 429 and "budget" in r.text.lower():
                        # OpenAlex 무료 할당량(IP당 하루 예산) 소진 — 자정 UTC까지 다시 묻지 않는다
                        self.exhausted.add(host)
                        print(f"  ! {host} 할당량 소진 — 이 호스트는 건너뛴다", file=sys.stderr)
                        return None
                    if r.status_code == 429 or r.status_code >= 500:
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    r.raise_for_status()
                    self.memo[key] = r.json()
                    return self.memo[key]
                except httpx.HTTPError:
                    await asyncio.sleep(1.0 * (attempt + 1))
        self.memo[key] = None
        return None

    async def openalex_by_id(self, ident: str) -> dict[str, Any] | None:
        """ident: 'https://doi.org/…' · 'pmid:…' · 'pmcid:…' · OpenAlex id."""
        work = await self._get_json(f"{OPENALEX}/works/{ident}", {"select": _OA_SELECT})
        return _openalex_record(work) if work else None

    async def openalex_search(self, title: str) -> list[dict[str, Any]]:
        if not title.strip():
            return []
        data = await self._get_json(
            f"{OPENALEX}/works", {"search": title[:300], "per-page": 5, "select": _OA_SELECT}
        )
        return [_openalex_record(w) for w in (data or {}).get("results", [])]

    async def openalex_author_year(
        self, surname: str, year: int, title: str
    ) -> list[dict[str, Any]]:
        """제목으로 못 찾을 때 — 그 성 · 연도의 논문 중 제목 낱말이 겹치는 것. 모델이 저자 · 연도는
        맞고 제목을 틀리게 낸 인용(서지 오류)을 '없음'과 가른다."""
        base = {
            "filter": f"raw_author_name.search:{surname},publication_year:{year}",
            "per-page": 5,
            "select": _OA_SELECT,
        }
        data = None
        if title.strip():
            data = await self._get_json(f"{OPENALEX}/works", base | {"search": title[:300]})
        if not (data or {}).get("results"):
            data = await self._get_json(f"{OPENALEX}/works", base)
        return [_openalex_record(w) for w in (data or {}).get("results", [])]

    async def crossref_search(self, title: str) -> list[dict[str, Any]]:
        if not title.strip():
            return []
        data = await self._get_json(CROSSREF, {"query.bibliographic": title[:300], "rows": 3})
        return self._crossref_records(data)

    async def crossref_author_year(
        self, surname: str, year: int, title: str
    ) -> list[dict[str, Any]]:
        """openalex_author_year와 같은 뜻 — OpenAlex 무료 할당량(IP당 하루 예산)이 끝났을 때도
        돈다."""
        params: dict[str, Any] = {
            "query.author": surname,
            "filter": f"from-pub-date:{year - 1},until-pub-date:{year + 1}",
            "rows": 5,
        }
        if title.strip():
            params["query.bibliographic"] = title[:300]
        return self._crossref_records(await self._get_json(CROSSREF, params))

    @staticmethod
    def _crossref_records(data: Any) -> list[dict[str, Any]]:
        out = []
        for item in ((data or {}).get("message") or {}).get("items", []):
            year = None
            for key in ("published-print", "published-online", "issued", "created"):
                parts = (item.get(key) or {}).get("date-parts") or [[None]]
                if parts and parts[0] and parts[0][0]:
                    year = parts[0][0]
                    break
            out.append(
                {
                    "id": item.get("URL"),
                    "doi": item.get("DOI", ""),
                    "title": " ".join(item.get("title") or []),
                    "year": year,
                    "authors": [
                        " ".join(filter(None, [a.get("given"), a.get("family")]))
                        for a in item.get("author", [])
                    ],
                    "venue": " ".join(item.get("container-title") or []) or None,
                    "abstract": re.sub(r"<[^>]+>", " ", item.get("abstract", "")) or None,
                }
            )
        return out

    async def europepmc_abstract(self, doi: str) -> str | None:
        data = await self._get_json(
            EUROPEPMC, {"query": f"DOI:{doi}", "resultType": "core", "format": "json"}
        )
        results = ((data or {}).get("resultList") or {}).get("result") or []
        return (results[0].get("abstractText") or None) if results else None

    async def semantic_scholar_abstract(self, doi: str) -> str | None:
        """OpenAlex · Europe PMC에 초록이 없는 논문(환경심리 저널의 옛 논문 등). 키 없는 호출은
        초당 1회 안팎이라 한 번에 하나씩 보낸다."""
        async with self._s2_gate:
            data = await self._get_json(f"{SEMANTIC_SCHOLAR}/DOI:{doi}", {"fields": "abstract"})
            await asyncio.sleep(1.1)
        return (data or {}).get("abstract") or None

    async def abstract_for(self, record: Mapping[str, Any]) -> str | None:
        text = record.get("abstract")
        if not text and record.get("doi"):
            text = await self.europepmc_abstract(record["doi"])
        if not text and record.get("doi"):
            text = await self.semantic_scholar_abstract(record["doi"])
        return text

    async def fetch_page(self, url: str) -> dict[str, Any]:
        key = "page:" + url
        if key in self.memo:
            return self.memo[key]
        row: dict[str, Any] = {"url": url, "status": None, "final_url": None, "text": None}
        async with self.gate:
            try:
                r = await self.client.get(url, follow_redirects=True, timeout=25)
                row["status"] = r.status_code
                row["final_url"] = str(r.url)
                if r.status_code < 400 and "html" in r.headers.get("content-type", "html"):
                    row["text"] = page_text(r.text)
            except httpx.HTTPError as exc:
                row["error"] = f"{type(exc).__name__}"
        self.memo[key] = row
        return row


_META_CONTENT = re.compile(
    r"(?is)<meta[^>]+(?:name|property)=[\"'](?:description|og:description|citation_abstract|"
    r"dc\.description|dcterms\.abstract)[\"'][^>]*content=[\"']([^\"']+)"
)
_META_CONTENT_FIRST = re.compile(
    r"(?is)<meta[^>]+content=[\"']([^\"']+)[\"'][^>]*(?:name|property)=[\"'](?:description|"
    r"og:description|citation_abstract|dc\.description|dcterms\.abstract)[\"']"
)


def page_text(raw: str) -> str:
    """HTML → 본문 글자. 메타 설명 · 초록을 앞에 두고 스크립트 · 메뉴를 뺀다."""
    metas = _META_CONTENT.findall(raw) + _META_CONTENT_FIRST.findall(raw)
    body = re.sub(r"(?is)<(script|style|nav|header|footer|noscript|svg|form).*?</\1>", " ", raw)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    text = html.unescape(" ".join(metas) + " " + body)
    return re.sub(r"\s+", " ", text).strip()[:PAGE_TEXT_LIMIT]


ACADEMIC = (
    "doi.org", "pubmed.ncbi.nlm.nih.gov", "pmc.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov",
    "springer.com", "link.springer.com", "nature.com", "sciencedirect.com", "wiley.com",
    "tandfonline.com", "sagepub.com", "frontiersin.org", "mdpi.com", "plos.org", "psycnet.apa.org",
    "academic.oup.com",
    "cambridge.org", "jamanetwork.com", "bmj.com", "thelancet.com", "researchgate.net",
    "semanticscholar.org", "scholar.google.com", "arxiv.org", "psyarxiv.com", "osf.io", "kci.go.kr",
    "dbpia.co.kr", "koreascience.kr", "koreascience.or.kr", "riss.kr", "scienceon.kisti.re.kr",
    "jstor.org", "hindawi.com", "biomedcentral.com", "cell.com", "science.org", "pnas.org",
    "liebertpub.com", "karger.com", "europepmc.org", "scielo.br", "sciencedirect", "journals.",
    "jkan.or.kr", "e-jkpr.org", "kjpa.org", "jstage.jst.go.jp", "ebsco", "proquest.com",
    "psychiatryonline.org", "nejm.org", "annualreviews.org", "apa.org/pubs", "elsevier.com",
    "degruyter.com", "iospress", "ieee.org", "acm.org", "jmir.org", "cochranelibrary.com",
    "ovid.com", "scitation.org", "bpsjournals", "hogrefe.com", "oup.com", "journal",
    "crossref.org", "openalex.org",
)
PUBLIC = (
    ".gov", ".go.kr", "who.int", "nhs.uk", "mayoclinic.org", "clevelandclinic.org",
    "health.harvard.edu", ".edu", ".ac.kr", ".ac.uk", "hopkinsmedicine.org", "mind.org.uk",
    "apa.org", "nami.org", "or.kr", "re.kr", "mentalhealth.org.uk", "helpguide.org",
)
NEWS = (
    "nytimes.com", "bbc.", "cnn.com", "theguardian.com", "washingtonpost.com", "forbes.com",
    "time.com", "healthline.com", "verywellmind.com", "psychologytoday.com",
    "medicalnewstoday.com", "webmd.com", "chosun.com", "joongang.co.kr", "donga.com", "hani.co.kr",
    "khan.co.kr", "yna.co.kr", "kbs.co.kr", "imbc.com", "sbs.co.kr", "hankyung.com", "mk.co.kr",
    "ytn.co.kr", "newsis.com", "news1.kr", "edaily.co.kr", "seoul.co.kr", "kormedi.com",
    "hidoc.co.kr", "sciencetimes.co.kr", "dongascience.com", "medigatenews.com", "docdocdoc.co.kr",
    "health.chosun.com", "theconversation.com", "scientificamerican.com", "sciencedaily.com",
    "news.", "newsweek.com", "huffpost.com", "vox.com", "economist.com", "ft.com", "wsj.com",
    "reuters.com", "apnews.com", "npr.org", "psypost.org", "neurosciencenews.com",
    "mindbodygreen.com", "everydayhealth.com", "health.com", "self.com", "wellandgood.com",
)
BLOG = (
    "blog.naver.com", "m.blog.naver.com", "post.naver.com", "tistory.com", "brunch.co.kr",
    "medium.com", "velog.io", "wordpress.com", "blogspot.com", "reddit.com", "quora.com",
    "kin.naver.com", "namu.wiki", "wikipedia.org", "substack.com", "cafe.naver.com",
    "cafe.daum.net", "youtube.com", "youtu.be", "linkedin.com", "facebook.com", "instagram.com",
    "x.com", "twitter.com", "threads.net", "tumblr.com", "ghost.io", "hashnode", "note.com",
)
SOURCE_TYPES = ("학술", "공공", "언론", "블로그", "상업")


def source_type(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    path = urlparse(url).path.lower()
    target = host + path
    if any(k in target for k in ACADEMIC) or re.search(r"/10\.\d{4,}/", path):
        return "학술"
    if any(host.endswith(k) or k in host for k in BLOG):
        return "블로그"
    if any(host.endswith(k) or k in host for k in NEWS):
        return "언론"
    if any(host.endswith(k) or k in host for k in PUBLIC):
        return "공공"
    return "상업"


def academic_ident(url: str) -> str | None:
    """OpenAlex로 바로 찾을 수 있는 주소 — PubMed · PMC · DOI."""
    m = re.search(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)", url)
    if m:
        return f"pmid:{m.group(1)}"
    m = re.search(r"/articles/(PMC\d+)", url)
    if m:
        return f"pmcid:{m.group(1)}"
    m = re.search(r"(10\.\d{4,}/[^\s?#]+)", url)
    if m:
        return "https://doi.org/" + m.group(1).rstrip("/.)")
    return None


def author_title_ok(claimed: str, found: str) -> bool:
    """저자 · 연도로 찾은 논문이 인용한 논문으로 보이는가 — 내용 낱말이 둘 이상, 짧은 쪽의 4할 이상
    겹친다."""
    ratio, shared = title_overlap(claimed, found)
    return shared >= 2 and ratio >= AUTHOR_TITLE_OVERLAP


def _check(record: Mapping[str, Any], mention: Mapping[str, Any], title: str) -> dict[str, Any]:
    """찾은 레코드가 인용과 맞는가 — 제목 · 연도(±1) · 1저자 성."""
    sim = title_similarity(title, record["title"]) if title else 1.0
    year_ok = record.get("year") is not None and abs(record["year"] - mention["year"]) <= 1
    known = [a for a in record.get("authors") or [] if a and "no authorship" not in a.lower()]
    tokens = set(re.findall(r"[a-z]+", fold(" ".join(known))))
    # 성은 낱말 단위로 맞춘다('La' 같은 조각이 다른 이름 안에 들어 있어도 맞다고 보지 않는다).
    # 'Palmer-Cooper' · 'O’Connell'은 낱말로 나눠 모두 있어야 한다. 저자가 없는 레코드(책 ·
    # 보고서)는 저자를 반증할 수 없으니 맞다고 본다
    wanted = [
        parts for s in mention["surnames"]
        if (parts := [t for t in re.findall(r"[a-z]+", fold(s)) if len(t) >= 3])
    ]
    author_ok = not wanted or not known or any(all(t in tokens for t in parts) for parts in wanted)
    problems = []
    if not year_ok:
        problems.append(f"연도 {record.get('year')} ≠ {mention['year']}")
    if not author_ok:
        found = ", ".join((record.get("authors") or [])[:3])
        problems.append(f"저자 {found} ≠ {mention['authors']}")
    return {"similarity": round(sim, 3), "year_ok": year_ok, "author_ok": author_ok,
            "problems": problems}


async def verify_mention(
    resolver: Resolver, mention: Mapping[str, Any], citation: Mapping[str, Any] | None, mode: str
) -> dict[str, Any]:
    """인용 하나의 실존 · 출처 텍스트. status: FOUND · MISMATCH · NOT_FOUND · UNDECLARED."""
    out: dict[str, Any] = {
        "status": "UNDECLARED", "record": None, "check": None, "source_text": None,
        "source_type": None, "page": None, "lookups": [],
    }
    if citation is None:
        return out
    title = citation.get("title") or ""
    candidates: list[tuple[str, dict[str, Any]]] = []
    doi = (citation.get("doi") or "").strip()
    if doi:
        bare = doi.replace("https://doi.org/", "")
        rec = await resolver.openalex_by_id("https://doi.org/" + bare)
        if rec:
            candidates.append(("doi", rec))
    url = citation.get("url") or ""
    if mode == "web" and url:
        page = await resolver.fetch_page(url)
        out["page"] = page
        out["source_type"] = source_type(page.get("final_url") or url)
        ident = academic_ident(page.get("final_url") or url)
        if ident:
            rec = await resolver.openalex_by_id(ident)
            if rec:
                candidates.append(("url", rec))
    if not candidates or all(_check(r, mention, title)["similarity"] < 0.5 for _, r in candidates):
        for rec in await resolver.openalex_search(title):
            candidates.append(("openalex", rec))
        if not any(title_similarity(title, r["title"]) >= TITLE_MATCH for _, r in candidates):
            for rec in await resolver.crossref_search(title):
                candidates.append(("crossref", rec))
        if (
            not any(title_similarity(title, r["title"]) >= TITLE_MATCH for _, r in candidates)
            and mention["surnames"]
        ):
            surname = max(mention["surnames"], key=len)  # 'La Torre' → Torre
            for rec in await resolver.openalex_author_year(surname, mention["year"], title):
                candidates.append(("author", rec))
            for rec in await resolver.crossref_author_year(surname, mention["year"], title):
                candidates.append(("author", rec))
    out["lookups"] = [f"{how}:{r['title'][:60]}" for how, r in candidates]
    checked = [(how, rec, _check(rec, mention, title)) for how, rec in candidates]
    # DOI · URL로 바로 찾은 것 → 제목이 맞는 것 → 저자 · 연도만 맞고 제목이 다른 것(서지 오류) 순
    direct = [c for c in checked if c[0] in ("doi", "url")]
    by_title = [c for c in checked if c[2]["similarity"] >= TITLE_MATCH]
    by_author = [
        c for c in checked
        if c[0] == "author" and c[2]["year_ok"] and c[2]["author_ok"]
        and author_title_ok(title, c[1]["title"])
    ]
    if direct or by_title:
        how, rec, check = max(direct or by_title, key=lambda c: c[2]["similarity"])
        out["record"], out["check"] = rec, check
        out["status"] = "FOUND" if check["year_ok"] and check["author_ok"] else "MISMATCH"
        out["source_text"] = await resolver.abstract_for(rec)
    elif by_author:
        how, rec, check = max(by_author, key=lambda c: title_overlap(title, c[1]["title"]))
        out["record"] = rec
        out["check"] = check | {"problems": [f"제목 다름: {rec['title'][:80]}"]}
        out["status"] = "MISMATCH"
        out["source_text"] = await resolver.abstract_for(rec)
    else:
        out["status"] = "NOT_FOUND"
    if mode == "web" and not out["source_text"] and out["page"] and out["page"].get("text"):
        out["source_text"] = out["page"]["text"]
    if mode != "web" and out["status"] in ("FOUND", "MISMATCH"):
        out["source_type"] = "학술"
    return out


async def verify(args: argparse.Namespace) -> int:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/html;q=0.9, */*;q=0.5"}
    async with httpx.AsyncClient(headers=headers, timeout=30) as client:
        resolver = Resolver(client)
        for path in args.files:
            payload = json.loads(path.read_text(encoding="utf-8"))
            mode = payload["meta"]["mode"]
            for run in payload["runs"]:
                if run.get("error"):
                    continue
                if args.resume and run.get("mentions"):
                    # 찾은 레코드는 두고 대조만 다시(코드가 바뀌었을 때), 못 찾은 인용만 다시 조회
                    mentions, unattributed = run["mentions"], run.get("unattributed", [])
                    for m in mentions:
                        title = (m.get("citation") or {}).get("title") or ""
                        problems = (m.get("check") or {}).get("problems") or []
                        by_author = problems and str(problems[0]).startswith("제목 다름")
                        if m.get("record") and by_author:
                            # 저자 · 연도로 찾은 것 — 제목 겹침 규칙으로 다시 가른다
                            check = _check(m["record"], m, title)
                            if check["year_ok"] and check["author_ok"] and author_title_ok(
                                title, m["record"]["title"]
                            ):
                                m["check"] = check | {"problems": problems}
                            else:
                                m.update(
                                    {"status": "NOT_FOUND", "record": None, "check": None,
                                     "source_text": None, "source_type": None}
                                )
                        elif m.get("record") and m["status"] in ("FOUND", "MISMATCH"):
                            check = _check(m["record"], m, title)
                            m["check"] = check
                            m["status"] = (
                                "FOUND" if check["year_ok"] and check["author_ok"] else "MISMATCH"
                            )
                        elif m["status"] == "NOT_FOUND":
                            m.update(await verify_mention(resolver, m, m.get("citation"), mode))
                else:
                    mentions, unattributed = find_mentions(run["rationale"])
                    run["unattributed"] = unattributed
                    tasks = []
                    for m in mentions:
                        cited = resolve(m, run["citations"])
                        m["citation"] = cited
                        tasks.append(verify_mention(resolver, m, cited, mode))
                    for m, result in zip(mentions, await asyncio.gather(*tasks), strict=True):
                        m.update(result)
                run["mentions"] = mentions
                statuses = Counter(m["status"] for m in mentions)
                print(
                    f"{mode:4s} {run['case']:22s} r{run['repeat']} 인용 {len(mentions)}"
                    f" {dict(statuses)} · 출처 없는 연구 문장 {len(unattributed)}",
                    flush=True,
                )
            payload["meta"]["verified_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"→ {path}")
    return 0


# --- Judge ---------------------------------------------------------------------------------------

JUDGE_SYSTEM_PROMPT = """\
너는 웰니스 앱 추천 이유의 채점자다. 플래너는 사용자 입력 · 상태를 보고 고른 활동을 권하는 추천
이유를 썼고, 그 안에서 '저자(연도)'로 연구를 인용했다. 코드가 인용마다 그 연구를 찾아 출처 텍스트
(논문 초록, 또는 웹 페이지 본문)를 붙였다. 출처 텍스트가 사실의 전부다 — 네가 아는 논문 지식으로
보태거나 깎지 않는다. 판정은 분명할 때만 부정으로 한다.

인용마다:
1. support — 주장 문장이 그 연구에 대해 말한 핵심(어떤 활동 · 자극이 어떤 지표를 좋게 했다)이
   출처 텍스트에 있으면 SUPPORTED. 출처가 그와 어긋나게 말하면 — 다른 지표를 잰 결과를 목표
   지표 개선으로 바꿈(정서 결과를 스트레스 · 불안 감소로), 효과 없음 · 반대 결과를 효과로, 다른
   활동 · 자극의 결과를 이 활동의 결과로, 출처에 없는 수치를 적음 — NOT_SUPPORTED. 출처
   텍스트가 그 점을 다루지 않아 맞다고도 틀리다고도 할 수 없으면 UNVERIFIABLE. 뜻이 같은 바꿔
   말하기와 상위어(학부생 → 성인)는 지지다. 출처 텍스트는 초록이라 세부(시간 · 횟수 · 대상 · 부차
   지표)가 빠져 있을 수 있다 — 주장의 핵심이 출처에 있고 세부만 확인되지 않으면 SUPPORTED,
   핵심이 출처에 없으면 UNVERIFIABLE이고, NOT_SUPPORTED는 출처가 어긋나게 말할 때뿐이다.
2. condition — 출처가 말하는 연구 상황(참가자가 활동을 한 시점 — 스트레스 과제 앞 · 뒤 · 피로
   유발 뒤 · 유발 없음, 대상 집단, 매체)이 사용자 상태(목표 · 사용자 상태 줄)에 맞으면 MATCH,
   분명히 어긋나면 MISMATCH(예: 스트레스 상황을 앞둔 사용자에게 스트레스 뒤 회복만 본 연구, 임상
   환자 표본, 다른 활동의 연구), 출처에 그 정보가 없으면 UNKNOWN.

추천 이유 전체:
3. hedged — 효과를 단정하거나 약속하지 않는다(반드시 · 확실히 · 바로 낫는다). 치료 · 진단 · 임상
   효과를 말하거나 건강한 성인 연구를 질환 · 환자로 넓히면 FAIL.
4. personal — 사용자 입력 · 상태에서 시작해 이 활동을 고른 이유를 말하고 근거로 잇는다. 사용자
   상황이 빠진 일반론이면 FAIL.

why는 SUPPORTED · MATCH · UNKNOWN · PASS면 빈 문자열, 아니면 한국어 한 문장이다. 인용은 번호
순서대로 모두 판정한다."""


class MentionJudgment(BaseModel):
    index: int = Field(description="인용 번호(1부터)")
    support: SupportVerdict
    support_why: str = ""
    condition: ConditionVerdict
    condition_why: str = ""


class RationaleJudgment(BaseModel):
    mentions: list[MentionJudgment]
    hedged: PassFail
    hedged_why: str = ""
    personal: PassFail
    personal_why: str = ""


def judge_input(run: Mapping[str, Any]) -> tuple[str, list[int]]:
    """Judge의 사용자 메시지와, 보낸 인용의 자리(출처 텍스트가 없는 인용은 보내지 않는다)."""
    lines = ["# 플래너가 받은 입력", run["input"], "", "# 추천 이유", run["rationale"], ""]
    sent: list[int] = []
    lines.append("# 인용과 출처 텍스트")
    for i, m in enumerate(run.get("mentions", [])):
        if not m.get("source_text"):
            continue
        sent.append(i)
        rec = m.get("record") or {}
        labels = {"FOUND": "실존 확인", "MISMATCH": "서지 일부 불일치"}
        status = labels.get(m["status"], m["status"])
        head = f"[{len(sent)}] {m['authors']} ({m['year']}) — {status}"
        if rec.get("title"):
            head += f" · 제목: {rec['title']}"
            if rec.get("year"):
                head += f" ({rec['year']})"
        if m.get("source_type"):
            head += f" · 출처 종류: {m['source_type']}"
        lines += [head, f"주장 문장: {m['sentence']}",
                  f"출처 텍스트: {m['source_text'][:SOURCE_TEXT_LIMIT]}", ""]
    return "\n".join(lines), sent


async def judge(args: argparse.Namespace) -> int:
    from openai import AsyncOpenAI

    settings = get_settings()
    if not settings.openai_api_key:
        print("OPENAI_API_KEY가 없다", file=sys.stderr)
        return 2
    client = AsyncOpenAI(api_key=settings.openai_api_key, max_retries=2, timeout=240)
    payload = json.loads(args.file.read_text(encoding="utf-8"))
    runs = [
        r for r in payload["runs"]
        if not r.get("error") and r.get("repeat", 0) < args.repeats
        and (not args.only or r["case"] in args.only)
    ]
    gate = asyncio.Semaphore(args.concurrency)
    total = {"input": 0, "cached": 0, "output": 0, "reasoning": 0}

    async def one(run: dict[str, Any]) -> None:
        text, sent = judge_input(run)
        async with gate:
            try:
                response = await client.responses.parse(
                    model=args.judge_model,
                    instructions=JUDGE_SYSTEM_PROMPT,
                    input=text,
                    text_format=RationaleJudgment,
                    reasoning={"effort": args.effort},
                )
            except Exception as exc:  # noqa: BLE001
                run["judge_error"] = f"{type(exc).__name__}: {exc}"
                print(f"{run['case']:22s} ✗ {run['judge_error']}", flush=True)
                return
        parsed: RationaleJudgment = response.output_parsed
        usage = usage_of(response)
        for k in total:
            total[k] += usage.get(k, 0)
        by_index = {j.index: j for j in parsed.mentions}
        for pos, i in enumerate(sent, start=1):
            j = by_index.get(pos)
            m = run["mentions"][i]
            if j is None:
                m["judge"] = None
                continue
            m["judge"] = {
                "support": j.support, "support_why": j.support_why,
                "condition": j.condition, "condition_why": j.condition_why,
            }
        for i, m in enumerate(run["mentions"]):
            if i not in sent:
                m["judge"] = {"support": "UNVERIFIABLE", "support_why": "출처 텍스트 없음",
                              "condition": "UNKNOWN", "condition_why": ""}
        run["judgment"] = {
            "hedged": parsed.hedged, "hedged_why": parsed.hedged_why,
            "personal": parsed.personal, "personal_why": parsed.personal_why,
            "tokens": usage, "cost_usd": price_usd(args.judge_model, usage),
        }
        supports = Counter(m["judge"]["support"] for m in run["mentions"] if m.get("judge"))
        print(f"{run['case']:22s} r{run['repeat']} {dict(supports)} hedged {parsed.hedged}"
              f" personal {parsed.personal}", flush=True)

    started = time.perf_counter()
    await asyncio.gather(*(one(r) for r in runs))
    cost = price_usd(args.judge_model, total)
    payload["meta"]["judge"] = {
        "model": args.judge_model, "effort": args.effort, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "tokens": total, "cost_usd": cost, "seconds": time.perf_counter() - started,
        "repeats": args.repeats,
    }
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nJudge {args.judge_model}({args.effort}) · {len(runs)}건 · 토큰 "
          f"{total['input']:,}/{total['output']:,} · ${cost:.3f} → {args.out}")
    return 0


# --- 집계 · 기록 ----------------------------------------------------------------------------------


def pct(n: int, d: int) -> str:
    return f"{n / d:.3f}" if d else "—"


def summarize(payload: Mapping[str, Any]) -> dict[str, Any]:
    """비교군 하나의 수치. 인용 수 · 실존 · 출처 유형은 1회차(repeat 0), Judge 지표는 채점한 run
    전부(기본은 1회차만 채점한다), 재현성은 반복 전체다."""
    runs = [r for r in payload["runs"] if not r.get("error")]
    first = [r for r in runs if r.get("repeat", 0) == 0]
    judged = [r for r in runs if r.get("judgment")]
    mentions_first = [m for r in first for m in r.get("mentions", [])]
    mentions_judged = [m for r in judged for m in r.get("mentions", []) if m.get("judge")]
    status = Counter(m["status"] for m in mentions_first)
    kinds = Counter(m.get("source_type") or "없음" for m in mentions_first
                    if m["status"] != "UNDECLARED")
    support = Counter(m["judge"]["support"] for m in mentions_judged)
    condition = Counter(m["judge"]["condition"] for m in mentions_judged)
    with_text = [m for m in mentions_judged if m.get("source_text")]
    web_ok = [m for m in mentions_first if m.get("page")]
    stability = None
    repeats = payload["meta"].get("repeat", 1)
    if repeats > 1:
        jaccards, kept = [], []
        for case in {r["case"] for r in runs}:
            sets = []
            for k in range(repeats):
                run = next((r for r in runs if r["case"] == case and r["repeat"] == k), None)
                if run is None:
                    break
                sets.append({_cite_key(m) for m in run.get("mentions", []) if _cite_key(m)})
            if len(sets) < 2:
                continue
            union = set().union(*sets)
            inter = set.intersection(*sets)
            jaccards.append(len(inter) / len(union) if union else 1.0)
            kept += [1 if key in inter else 0 for key in sets[0]]
        stability = {
            "jaccard": statistics.mean(jaccards) if jaccards else None,
            "kept": [sum(kept), len(kept)],
        }
    return {
        "mode": payload["meta"]["mode"],
        "model": payload["meta"].get("model"),
        "runs": len(first),
        "repeats": repeats,
        "errors": [r["case"] for r in payload["runs"] if r.get("error")],
        "mentions_per_run": len(mentions_first) / len(first) if first else 0,
        "no_citation_runs": sum(1 for r in first if not r.get("mentions")),
        "unattributed": [sum(len(r.get("unattributed", [])) for r in first), len(first)],
        "status": dict(status),
        "mentions": len(mentions_first),
        "source_types": dict(kinds),
        "web_ok": [sum(1 for m in web_ok if (m["page"].get("status") or 999) < 400), len(web_ok)],
        "judged_runs": len(judged),
        "judged_mentions": len(mentions_judged),
        "support": dict(support),
        "support_with_text": [sum(1 for m in with_text if m["judge"]["support"] == "SUPPORTED"),
                              len(with_text)],
        "condition": dict(condition),
        "hedged": [sum(1 for r in judged if r["judgment"]["hedged"] == "PASS"), len(judged)],
        "personal": [sum(1 for r in judged if r["judgment"]["personal"] == "PASS"), len(judged)],
        "stability": stability,
        "cost_per_run": statistics.mean(r["cost_usd"] for r in runs) if runs else 0,
        "seconds_per_run": statistics.mean(r["seconds"] for r in runs) if runs else 0,
        "tokens_per_run": [statistics.mean(r["tokens"]["input"] for r in runs) if runs else 0,
                           statistics.mean(r["tokens"]["output"] for r in runs) if runs else 0],
        "judge": payload["meta"].get("judge"),
        "stability_all": stability,
    }


def _cite_key(m: Mapping[str, Any]) -> str | None:
    rec = m.get("record") or {}
    if rec.get("id"):
        return rec["id"]
    c = m.get("citation") or {}
    if c.get("title"):
        return "t:" + norm_title(c["title"])
    return None


MODE_NAMES = {"rag": "A. LUMI RAG", "llm": "B. LLM 단독", "web": "C. LLM + 웹 검색"}


def format_table(summaries: Sequence[Mapping[str, Any]]) -> str:
    cols = [MODE_NAMES.get(s["mode"], s["mode"]) for s in summaries]
    rows: list[tuple[str, list[str]]] = []

    def row(label: str, fn) -> None:
        rows.append((label, [fn(s) for s in summaries]))

    row("추천 이유 수", lambda s: str(s["runs"]))
    row("인용 수 / 추천 이유", lambda s: f"{s['mentions_per_run']:.2f}")
    row("인용 없는 추천 이유", lambda s: f"{s['no_citation_runs']}/{s['runs']}")
    row("출처 없는 연구 주장 문장(건 / 추천 이유 수)",
        lambda s: f"{s['unattributed'][0]} / {s['unattributed'][1]}")
    row("인용 실존 — 일치(FOUND)", lambda s: _frac(s["status"].get("FOUND", 0), s["mentions"]))
    row("인용 실존 — 서지 오류(MISMATCH)",
        lambda s: _frac(s["status"].get("MISMATCH", 0), s["mentions"]))
    row("인용 실존 — 없음(NOT_FOUND)",
        lambda s: _frac(s["status"].get("NOT_FOUND", 0), s["mentions"]))
    row("인용 실존 — 인용 목록에 없음(UNDECLARED)",
        lambda s: _frac(s["status"].get("UNDECLARED", 0), s["mentions"]))
    row("학술 출처 비율", lambda s: _frac(s["source_types"].get("학술", 0),
                                       sum(s["source_types"].values())))
    row("출처 유형", lambda s: " · ".join(f"{k} {v}" for k, v in sorted(s["source_types"].items(),
                                                                 key=lambda kv: -kv[1])) or "—")
    row("URL 응답 OK(web)",
        lambda s: f"{s['web_ok'][0]}/{s['web_ok'][1]}" if s["web_ok"][1] else "—")
    row("주장 지지(SUPPORTED) / 인용 전체",
        lambda s: _frac(s["support"].get("SUPPORTED", 0), s["judged_mentions"]))
    row("주장 지지 / 출처 텍스트 있는 인용",
        lambda s: _frac(s["support_with_text"][0], s["support_with_text"][1]))
    row("주장 불일치(NOT_SUPPORTED)",
        lambda s: _frac(s["support"].get("NOT_SUPPORTED", 0), s["judged_mentions"]))
    row("연구 조건 — 사용자 상태와 맞음(MATCH)",
        lambda s: _frac(s["condition"].get("MATCH", 0), s["judged_mentions"]))
    row("연구 조건 — 어긋남(MISMATCH)",
        lambda s: _frac(s["condition"].get("MISMATCH", 0), s["judged_mentions"]))
    row("hedged PASS", lambda s: f"{s['hedged'][0]}/{s['hedged'][1]}")
    row("personal PASS", lambda s: f"{s['personal'][0]}/{s['personal'][1]}")
    row("재현성 — 반복 간 인용 집합 Jaccard",
        lambda s: f"{s['stability']['jaccard']:.3f}" if s.get("stability")
        and s["stability"]["jaccard"] is not None else "—")
    row("재현성 — 1회차 인용이 매번 다시 나옴",
        lambda s: f"{s['stability']['kept'][0]}/{s['stability']['kept'][1]}"
        if s.get("stability") else "—")
    row("비용 / 추천 이유(USD)", lambda s: f"${s['cost_per_run']:.4f}")
    row("토큰 / 추천 이유(입력/출력)",
        lambda s: f"{s['tokens_per_run'][0]:,.0f}/{s['tokens_per_run'][1]:,.0f}")
    row("초 / 추천 이유", lambda s: f"{s['seconds_per_run']:.1f}")
    out = ["| | " + " | ".join(cols) + " |", "| --- | " + " | ".join("---:" for _ in cols) + " |"]
    out += [f"| {label} | " + " | ".join(values) + " |" for label, values in rows]
    return "\n".join(out)


def _frac(n: int, d: int) -> str:
    return f"{n}/{d} ({n / d:.0%})" if d else "—"


def format_details(payload: Mapping[str, Any]) -> str:
    """비교군 하나의 어긴 것 — 없는 인용 · 서지 오류 · 불일치 주장 · 조건 어긋남 · hedged ·
    personal."""
    mode = payload["meta"]["mode"]
    runs = [r for r in payload["runs"] if not r.get("error") and r.get("repeat", 0) == 0]
    lines = [f"### {MODE_NAMES.get(mode, mode)}", ""]
    not_found = [(r, m) for r in runs for m in r.get("mentions", [])
                 if m["status"] in ("NOT_FOUND", "UNDECLARED")]
    if not_found:
        lines += [f"찾지 못한 인용 {len(not_found)}건", "",
                  "| 계획 | 활동 | 인용 | 모델이 낸 제목 |", "| --- | --- | --- | --- |"]
        for r, m in not_found:
            title = (m.get("citation") or {}).get("title") or "(인용 목록에 없음)"
            lines.append(f"| {r['case']} | {r['protocol_id']} | {m['authors']} ({m['year']}) |"
                         f" {_cell(title)} |")
        lines.append("")
    mismatch = [(r, m) for r in runs for m in r.get("mentions", []) if m["status"] == "MISMATCH"]
    if mismatch:
        lines += [f"서지 오류 {len(mismatch)}건", "", "| 계획 | 인용 | 찾은 논문 | 다른 점 |",
                  "| --- | --- | --- | --- |"]
        for r, m in mismatch:
            rec = m["record"]
            lines.append(f"| {r['case']} | {m['authors']} ({m['year']}) | {_cell(rec['title'])}"
                         f" ({rec.get('year')}) | {_cell(' · '.join(m['check']['problems']))} |")
        lines.append("")
    bad = [(r, m) for r in runs for m in r.get("mentions", [])
           if m.get("judge") and m["judge"]["support"] == "NOT_SUPPORTED"]
    if bad:
        lines += [f"출처가 뒷받침하지 않는 주장 {len(bad)}건", "",
                  "| 계획 | 인용 | 주장 문장 | 이유 |", "| --- | --- | --- | --- |"]
        for r, m in bad:
            lines.append(f"| {r['case']} | {m['authors']} ({m['year']}) | {_cell(m['sentence'])} |"
                         f" {_cell(m['judge']['support_why'])} |")
        lines.append("")
    off = [(r, m) for r in runs for m in r.get("mentions", [])
           if m.get("judge") and m["judge"]["condition"] == "MISMATCH"]
    if off:
        lines += [f"연구 조건이 사용자 상태와 어긋난 인용 {len(off)}건", "",
                  "| 계획 | 인용 | 이유 |", "| --- | --- | --- |"]
        for r, m in off:
            lines.append(f"| {r['case']} | {m['authors']} ({m['year']}) |"
                         f" {_cell(m['judge']['condition_why'])} |")
        lines.append("")
    for item in ("hedged", "personal"):
        fails = [r for r in runs if r.get("judgment") and r["judgment"][item] == "FAIL"]
        if fails:
            lines += [f"{item} FAIL {len(fails)}건", ""]
            lines += [f"- {r['case']} {r['protocol_id']}: {r['judgment'][item + '_why']}"
                      for r in fails]
            lines.append("")
    if mode == "web":
        domains = Counter(
            urlparse((m.get("page") or {}).get("final_url") or (m.get("citation") or {}).get("url")
                     or "").hostname or "?"
            for r in runs for m in r.get("mentions", []) if m.get("citation")
        )
        lines += ["웹 출처 도메인", ""]
        lines += [f"- {host} ×{n} ({source_type('https://' + host)})"
                  for host, n in domains.most_common()]
        lines.append("")
    unattributed = [(r, s) for r in runs for s in r.get("unattributed", [])]
    if unattributed:
        lines += [f"출처 없는 연구 주장 문장 {len(unattributed)}건", ""]
        lines += [f"- {r['case']}: {s}" for r, s in unattributed[:30]]
        if len(unattributed) > 30:
            lines.append(f"- … {len(unattributed) - 30}건 더")
        lines.append("")
    return "\n".join(lines)


def _cell(text: str) -> str:
    return (text or "").replace("|", "／").replace("\n", " ")


def format_report(payloads: Sequence[Mapping[str, Any]], title: str) -> str:
    summaries = [summarize(p) for p in payloads]
    lines = [f"# RAG 사용 vs LLM 단독 vs LLM + 웹 검색 — {title}", ""]
    for p, s in zip(payloads, summaries, strict=True):
        meta = p["meta"]
        judge_meta = meta.get("judge") or {}
        lines.append(
            f"- {MODE_NAMES.get(s['mode'], s['mode'])}: 생성 `{meta.get('model')}` · 추천 이유"
            f" {s['runs']}개 × 반복 {s['repeats']} · 생성 {meta.get('at')} · 생성 비용"
            f" ${meta.get('cost_usd', 0):.3f}"
            + (f" · 웹 검색 {meta.get('web_calls', 0)}회" if s["mode"] == "web" else "")
            + (f" · Judge `{judge_meta.get('model')}`({judge_meta.get('effort')}) 토큰"
               f" {judge_meta['tokens']['input']:,}/{judge_meta['tokens']['output']:,}"
               f" ${judge_meta['cost_usd']:.3f} · {judge_meta.get('at')}" if judge_meta else "")
            + (f" · {meta['note']}" if meta.get("note") else "")
        )
    lines += [
        "", "## 비교표", "", format_table(summaries), "",
        "- 인용은 추천 이유 문장의 '저자(연도)'를 코드가 찾은 것이다. 실존은 OpenAlex(DOI → 제목"
        " 검색, 없으면 Crossref)로 제목 · 연도(±1) · 1저자 성을 대조했다. 주장 지지 · 연구 조건"
        " · hedged · personal은 Judge 판정이다(출처 텍스트 = 초록, web은 페이지 본문).",
        "- rag의 비용 · 토큰은 plan 노드 한 번(활동 선택 + 설명 두 호출)이고 llm · web은 설명"
        " 한 호출이다.",
        "", "## 어긴 것", "",
    ]
    for p in payloads:
        lines += [format_details(p), ""]
    return "\n".join(lines)


def report(args: argparse.Namespace) -> int:
    payloads = [json.loads(p.read_text(encoding="utf-8")) for p in args.files]
    text = format_report(payloads, args.title)
    if args.md:
        args.md.write_text(text + "\n", encoding="utf-8")
        print(f"→ {args.md}")
    else:
        print(text)
    return 0


# --- 사람 확인 -----------------------------------------------------------------------------------

SHEET_COLUMNS = ("group", "case", "mention", "citation", "status", "source_type", "claim",
                 "source_text", "judge_support", "judge_condition", "why", "human_support",
                 "human_condition")
_SUPPORT_LABEL = {"S": "SUPPORTED", "N": "NOT_SUPPORTED", "U": "UNVERIFIABLE",
                  "SUPPORTED": "SUPPORTED", "NOT_SUPPORTED": "NOT_SUPPORTED",
                  "UNVERIFIABLE": "UNVERIFIABLE", "O": "SUPPORTED", "X": "NOT_SUPPORTED",
                  "-": "UNVERIFIABLE"}
_CONDITION_LABEL = {"M": "MATCH", "X": "MISMATCH", "U": "UNKNOWN", "MATCH": "MATCH",
                    "MISMATCH": "MISMATCH", "UNKNOWN": "UNKNOWN", "O": "MATCH", "-": "UNKNOWN"}


def sheet(args: argparse.Namespace) -> int:
    """사람이 확인할 줄 — 출처 텍스트가 있는 인용 전부(Judge 판정 칸은 가리고 본다)."""
    rows = []
    for path in args.files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        for run in payload["runs"]:
            if run.get("error") or not run.get("judgment"):
                continue
            for i, m in enumerate(run.get("mentions", [])):
                if not m.get("source_text") or not m.get("judge"):
                    continue
                rows.append(
                    {
                        "group": payload["meta"]["mode"], "case": run["case"], "mention": i,
                        "citation": f"{m['authors']} ({m['year']})", "status": m["status"],
                        "source_type": m.get("source_type") or "", "claim": m["sentence"],
                        "source_text": m["source_text"][:SOURCE_TEXT_LIMIT],
                        "judge_support": m["judge"]["support"],
                        "judge_condition": m["judge"]["condition"],
                        "why": " / ".join(filter(None, [m["judge"]["support_why"],
                                                        m["judge"]["condition_why"]])),
                        "human_support": "", "human_condition": "",
                    }
                )
    with args.out.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SHEET_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"{len(rows)}줄 → {args.out}")
    return 0


def agree(args: argparse.Namespace) -> int:
    """sheet의 human 칸(S · N · U / M · X · U)과 Judge 판정의 일치."""
    with args.sheet.open(encoding="utf-8", newline="") as f:
        rows = [r for r in csv.DictReader(f) if (r.get("human_support") or "").strip()]
    if not rows:
        print("human 칸이 비어 있다")
        return 1
    for item, labels, judge_col in (("support", _SUPPORT_LABEL, "judge_support"),
                                    ("condition", _CONDITION_LABEL, "judge_condition")):
        pairs = [(labels.get(r[f"human_{item}"].strip().upper()), r[judge_col]) for r in rows
                 if (r.get(f"human_{item}") or "").strip()]
        pairs = [(h, j) for h, j in pairs if h]
        same = sum(h == j for h, j in pairs)
        print(f"{item}: 일치 {same}/{len(pairs)}")
        for r, (h, j) in zip([r for r in rows if (r.get(f'human_{item}') or '').strip()], pairs,
                             strict=False):
            if h != j:
                print(f"  - {r['group']} {r['case']} {r['citation']}: 사람 {h} · Judge {j}")
    return 0


# --- CLI -----------------------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("import-rag", help="plan eval 결과 → rag 비교군")
    p.add_argument("plan_eval", type=Path)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--database-url", help="근거 DB(papers 표). 없으면 .env의 DATABASE_URL")
    p.set_defaults(func=lambda a: asyncio.run(import_rag(a)))

    p = sub.add_parser("generate", help="llm · web 비교군 생성(LLM 비용)")
    p.add_argument("plan_eval", type=Path)
    p.add_argument("--mode", choices=("llm", "web"), required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--only", nargs="*", help="이 계획만(예: S1-pre-a S2-post-b+change)")
    p.add_argument("--model", help="생성 모델. 없으면 .env의 LLM_MODEL")
    p.add_argument("--concurrency", type=int, default=6)
    p.set_defaults(func=lambda a: asyncio.run(generate(a)))

    p = sub.add_parser("verify", help="인용 실존 · 출처 텍스트(OpenAlex · Crossref · 웹, LLM 없음)")
    p.add_argument("files", nargs="+", type=Path)
    p.add_argument(
        "--resume", action="store_true",
        help="이미 확인한 파일 — 찾은 레코드는 대조만 다시 하고 못 찾은 인용만 다시 조회한다",
    )
    p.set_defaults(func=lambda a: asyncio.run(verify(a)))

    p = sub.add_parser("judge", help="주장 지지 · 연구 조건 · hedged · personal(LLM 비용)")
    p.add_argument("file", type=Path)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--judge-model", default=JUDGE_MODEL)
    p.add_argument("--effort", default=JUDGE_EFFORT)
    p.add_argument("--repeats", type=int, default=1, help="채점할 반복 수(기본 1회차만)")
    p.add_argument("--only", nargs="*")
    p.add_argument("--concurrency", type=int, default=6)
    p.set_defaults(func=lambda a: asyncio.run(judge(a)))

    p = sub.add_parser("report", help="비교표 · 어긴 것")
    p.add_argument("files", nargs="+", type=Path)
    p.add_argument("--md", type=Path)
    p.add_argument("--title", default=time.strftime("%Y-%m-%d"))
    p.set_defaults(func=report)

    p = sub.add_parser("sheet", help="사람 확인용 CSV")
    p.add_argument("files", nargs="+", type=Path)
    p.add_argument("--out", type=Path, required=True)
    p.set_defaults(func=sheet)

    p = sub.add_parser("agree", help="사람 라벨과 Judge의 일치")
    p.add_argument("judged", type=Path)
    p.add_argument("sheet", type=Path)
    p.set_defaults(func=agree)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
