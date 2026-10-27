"""근거 추출 — 파싱한 논문에서 레코드마다 METHODS · RESULTS 원문 발췌를 고른다.

지금 구현한 범위는 '발췌 치환'이다. 레코드(arm)와 메타데이터는 근거 arm 시트 + overrides가
정하고(scripts/seed_evidence.build_records), 이 모듈은 레코드마다 파싱한 논문
(parse.ParsedPaper)에서 content로 쓸 원문 구간만 고른다. 요약하지 않고 원문을 그대로
저장해 Reference 카드에서 출처를 확인할 수 있게 한다(EvidenceRecordIn.content 규칙).

- 고르는 쪽은 LLM(설정의 llm_model, 구조화 출력 1회). 입력은 시트가 적은 arm 사실
  (라벨 · 매체 · 지표 · N · 시트 요약)과 그 논문의 METHODS · RESULTS · TABLE 텍스트다.
  METHODS · RESULTS가 거의 비면(제목 인식 실패) 초록 · 서론 · 논의를 더한다.
- 환각 방지(verbatim): 받은 발췌를 문장으로 나눠 논문 본문에 그대로 있는지 본다.
  없으면 가장 가까운 본문 문장으로 바꾸고(snap, difflib ≥ SNAP_CUTOFF), 못 바꾸면
  버린다. 그래서 저장되는 발췌는 늘 본문에 있는 문장이다.
- 채택: confidence가 LOW가 아니고 남은 비율(coverage)이 MIN_COVERAGE 이상, 길이가
  MIN_KEEP 이상인 섹션만 JSON에 쓴다. 나머지는 시트 요약이 남고 검토 목록에 오른다.
- 글리프 복원: 유니코드 매핑이 없는 PDF 글꼴은 기호를 U+FFFD(�)로 낸다(poppler · pdfium도
  같다). 저장할 때만 문맥으로 되살린다 — 낱말 안은 합자(ff · fi · fl, 코퍼스에 있는 낱말로만),
  통계 기호 뒤는 '=', '= ' 뒤는 음수 부호, 숫자 사이는 범위 대시. 옛 Elsevier 글꼴이 괄호 ·
  등호 · 소수점을 'ð Þ ¼ :'로 내는 것('tð15Þ ¼ :92')도 되살린다. 고친 수는 repaired에 남는다.
- 결과는 data/evidence_excerpts.json — 키는 overrides와 같다(ARM은 experiment_id,
  메타 · 관찰은 paper_id). seed가 섹션마다 content로 쓰고, 없으면 시트 요약을 둔다.
- 임베딩: 설정의 embedding_model(기본 text-embedding-3-small, 1536차원 = 마이그레이션의
  vector(1536))로 seed가 만들고, 모델 이름을 행의 embedding_model에 같이 남긴다.

키·모델은 `app/config.py`에 있다 — OPENAI_API_KEY 하나로 LLM_MODEL · EMBEDDING_MODEL을
모두 부른다.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from app.ingestion.parse import ParsedPaper
from app.schemas.evidence import EvidenceMetadata, Section

MAX_CHARS = 1000  # 섹션 발췌 상한 — Reference 카드와 추천 이유 프롬프트(hit 5건)에 들어간다
MIN_CHARS = 150  # 이보다 짧으면 검토 대상
MIN_KEEP = 60  # 이보다 짧은 발췌는 쓰지 않는다
MIN_COVERAGE = 0.6  # 본문에서 확인된 문장 비율이 이보다 낮으면 쓰지 않는다
SNAP_CUTOFF = 0.85  # 본문 문장으로 바꿀 때의 difflib 유사도
CONTAIN_CUTOFF = 0.9  # 모델 문장 글자 중 이 비율 이상이 순서대로 든 원문 문장이면 그 문장으로
INPUT_BUDGET = 60_000  # LLM 입력 글자 상한. gpt-4o-mini 128k 토큰 안에서 넉넉히
FALLBACK_MIN = 3_000  # METHODS + RESULTS가 이보다 짧으면 다른 섹션을 더한다
EXCERPT_SECTIONS = (Section.METHODS, Section.RESULTS)
OTHER_SECTIONS = "ABSTRACT · INTRODUCTION · DISCUSSION"

_LIGATURES = {"\ufb01": "fi", "\ufb02": "fl", "\ufb00": "ff", "\ufb03": "ffi", "\ufb04": "ffl"}
_QUOTES = {
    "“": '"', "”": '"', "„": '"', "‘": "'", "’": "'", "‚": "'",
    "´": "'", "`": "'",
}
_DASHES = {"\u2013": "-", "\u2014": "-", "\u2010": "-", "\u2011": "-", "\u2212": "-", "\u00ad": ""}
# 옛 Elsevier 수식 글꼴은 통계값 뒤 마침표를 ':'로 낸다('po0:001: As Fig. 3').
# 그래서 숫자 뒤 ': 대문자'도 문장 끝으로 본다
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[\"'0-9])|(?<=\d:)\s+(?=[A-Z])|\n+")
# loose 비교에서 같은 자리표로 보는 것 — U+FFFD, 기호, 합자(모델이 원문의 �를 고쳐 옮긴 경우)
_LOOSE = re.compile(r"[\ufffd=<>\u2264\u2265\u00b1\u00d7\u00bc:.\-]|ffi|ffl|ff|fi|fl")
# 소문자화 뒤라 Þ(U+00DE)는 þ(U+00FE)로 온다
_FONT_PARENS_LOOSE = str.maketrans({"\u00f0": "(", "\u00de": ")", "\u00fe": ")"})


def normalize(text: str, *, loose: bool = False) -> str:
    """비교용 정규화 — 소문자, 리가처 · 따옴표 · 대시 통일, 줄 끝 하이픈 연결, 공백 하나로.

    loose는 기호(= < > ± −)와 합자(ff · fi · fl)를 U+FFFD와 같은 자리표로 본다 — 모델이 원문의
    U+FFFD를 스스로 고쳐 옮겨도 원문 문장을 찾는다(저장은 원문 문장 + repair_glyphs)."""
    for table in (_LIGATURES, _QUOTES, _DASHES):
        for src, dst in table.items():
            text = text.replace(src, dst)
    text = text.replace("\u00a0", " ").lower()
    text = re.sub(r"(\w)-\s+(\w)", r"\1\2", text)  # 'respi- ration' → 'respiration'
    if loose:
        text = _LOOSE.sub("~", text.translate(_FONT_PARENS_LOOSE))
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# 문장 끝으로 보지 않는 약어 — 'Fig. 3' · 'et al. (2019)'에서 끊지 않는다
_ABBREVIATIONS = frozenset(
    "fig. figs. tab. eq. al. e.g. i.e. vs. cf. no. nos. approx. ref. refs. pp. vol. dr. ca.".split()
)


def split_sentences(text: str, *, min_chars: int = 20) -> list[str]:
    """문장 단위로 나눈다. 약어 뒤에서는 끊지 않고, 짧은 조각(표 셀 · 번호)은 뺀다."""
    pieces: list[str] = []
    for piece in (s.strip() for s in _SENTENCE_END.split(text) if s and s.strip()):
        last = pieces[-1].rsplit(maxsplit=1)[-1].lower() if pieces else ""
        if last in _ABBREVIATIONS:
            pieces[-1] = f"{pieces[-1]} {piece}"
        else:
            pieces.append(piece)
    return [s for s in pieces if len(s) >= min_chars]


GLYPH = "\ufffd"
_LIGATURE_CANDIDATES = ("ff", "fi", "fl", "ffi", "ffl")
_IN_WORD = re.compile(r"([A-Za-z]*)\ufffd([A-Za-z]+)")
_STAT_EQUALS = re.compile(
    r"(\b(?:F|p|ps|t|r|d|g|z|Z|η\s?2|χ\s?2|difference|SMD|CI|M|SD|OR|RR|β|b)\s?)\ufffd"
    r"(?=\s?[-\d.])"
)
_MINUS = re.compile(r"(?<=[=(]\s)\ufffd|(?<=[=(])\ufffd")  # '= �4.18' · '(�0.3'
_SPACE_MINUS = re.compile(r"(?<=[\s(\[])\ufffd(?=\d)")  # 'was �3.95' — 공백 뒤 숫자 앞
_RANGE = re.compile(r"(?<=\d)\ufffd(?=\s?\d)")  # '18� 28 years'


_FONT_PARENS = re.compile(r"\u00f0([^\u00f0\u00de]{0,20})\u00de")  # 'tð15Þ'
_FONT_EQUALS = re.compile(
    r"(?<=[\w)\].]\s)\u00bc(?=\s?[-\d.:\ufffd\u2212])|(?<=[\w)\].])\u00bc(?=[-\d.:\ufffd\u2212])"
)
_FONT_DECIMAL = re.compile(r"(?<=[\s(=\u2212\-\ufffd]):(?=\d)")  # 'p ¼ :92' → 'p = .92'
_BROKEN = (GLYPH, "\u00f0", "\u00de", "\u00bc")


def broken_count(text: str) -> int:
    """되살릴 글자 수 — U+FFFD와 옛 글꼴의 ð Þ ¼."""
    return sum(text.count(ch) for ch in _BROKEN)


def repair_glyphs(text: str, vocab: Mapping[str, int] | None = None) -> str:
    """U+FFFD와 옛 글꼴 매핑을 문맥으로 되살린다. 확정할 수 없는 것은 그대로 둔다."""
    if "\u00f0" in text or "\u00bc" in text:
        text = _FONT_PARENS.sub(r"(\1)", text)
        text = _FONT_EQUALS.sub("=", text)
        text = _FONT_DECIMAL.sub(".", text)
    if GLYPH not in text:
        return text
    vocab = vocab or {}

    def ligature(m: re.Match[str]) -> str:
        head, tail = m.group(1), m.group(2)
        best = max(_LIGATURE_CANDIDATES, key=lambda c: vocab.get(f"{head}{c}{tail}".lower(), 0))
        word = f"{head}{best}{tail}"
        return word if vocab.get(word.lower(), 0) else m.group(0)

    text = _IN_WORD.sub(ligature, text)
    text = _STAT_EQUALS.sub(lambda m: f"{m.group(1)}=", text)
    text = _MINUS.sub("\u2212", text)
    text = _RANGE.sub("\u2013", text)
    return _SPACE_MINUS.sub("\u2212", text)


# PDF가 기호를 제어 문자(NUL 등)로 내는 경우도 U+FFFD처럼 다룬다
_CONTROLS = frozenset(chr(c) for c in range(32)) - frozenset("\n\t\r")


def clean_controls(text: str) -> str:
    """제어 문자(줄바꿈 · 탭 제외) → U+FFFD. 글리프 복원 규칙이 같은 방식으로 본다."""
    if not _CONTROLS & set(text):
        return text
    return "".join(GLYPH if ch in _CONTROLS else ch for ch in text)


@dataclass
class Snapped:
    text: str
    coverage: float  # 본문에서 확인된 문장 비율(그대로 있던 것 + 바꾼 것)
    snapped: int = 0
    dropped: int = 0
    page: int | None = None
    repaired: int = 0

    @property
    def ok(self) -> bool:
        return self.coverage >= 0.8 and len(self.text) >= MIN_CHARS


class SourceIndex:
    """논문 본문의 문장 색인 — 발췌 문장이 본문에 있는지, 없으면 어느 문장이 가장 가까운지."""

    def __init__(self, paper: ParsedPaper) -> None:
        self.body = normalize(clean_controls(paper.body_text()))
        self.sentences: list[str] = []
        self.pages: list[int | None] = []
        for block in paper.blocks:
            if block.section is Section.OTHER:
                continue
            for sentence in split_sentences(clean_controls(block.text)):
                self.sentences.append(sentence)
                self.pages.append(block.page)
        self._normalized = [normalize(s) for s in self.sentences]
        self._loose = [normalize(s, loose=True) for s in self.sentences]
        self._words = [_words(s) for s in self._loose]

    def contains(self, sentence: str) -> bool:
        return normalize(sentence) in self.body

    def closest(self, sentence: str) -> str | None:
        """가장 가까운 원문 문장. 기호 · 합자 차이는 무시하고(loose) 비교한다."""
        key = normalize(sentence, loose=True)
        found = difflib.get_close_matches(key, self._loose, n=1, cutoff=SNAP_CUTOFF)
        if found:
            return self.sentences[self._loose.index(found[0])]
        return self._containing(key)

    def _containing(self, key: str) -> str | None:
        """모델이 원문 문장의 일부를 덜어 내고 옮긴 경우 — 모델 문장 글자의 CONTAIN_CUTOFF 이상이
        순서대로 들어 있는 원문 문장(4배 길이까지). 저장은 그 원문 문장 전체다."""
        if not key:
            return None
        words = _words(key)
        best, best_score = None, 0.0
        for i, source in enumerate(self._loose):
            if not len(key) * 0.9 <= len(source) <= len(key) * 4:
                continue
            if len(words & self._words[i]) < 0.7 * len(words):
                continue
            matcher = difflib.SequenceMatcher(None, key, source, autojunk=False)
            score = sum(b.size for b in matcher.get_matching_blocks()) / len(key)
            if score > best_score:
                best, best_score = i, score
        if best is None or best_score < CONTAIN_CUTOFF:
            return None
        return self.sentences[best]

    def page_of(self, sentence: str) -> int | None:
        key = normalize(sentence)
        for normalized, page in zip(self._normalized, self.pages, strict=True):
            if key == normalized or key in normalized or normalized in key:
                return page
        return None


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9~]+", text))


def snap_to_source(
    excerpt: str,
    index: SourceIndex,
    *,
    max_chars: int = MAX_CHARS,
    repair: Callable[[str], str] | None = None,
) -> Snapped:
    """발췌의 문장마다 본문에 있는지 보고, 없으면 가장 가까운 본문 문장으로 바꾸거나 버린다.
    repair는 남긴 문장에 저장 전에 적용한다(글리프 복원)."""
    kept: list[str] = []
    snapped = dropped = 0
    for sentence in split_sentences(clean_controls(excerpt), min_chars=1):
        if index.contains(sentence):
            kept.append(sentence)
            continue
        closest = index.closest(sentence)
        if closest is None:
            dropped += 1
            continue
        snapped += 1
        kept.append(closest)
    total = len(kept) + dropped
    page = index.page_of(kept[0]) if kept else None
    broken = sum(broken_count(s) for s in kept)
    if repair:
        kept = [repair(s) for s in kept]
    repaired = broken - sum(broken_count(s) for s in kept)
    text = ""
    for sentence in dict.fromkeys(kept):  # 문장 단위로 상한까지(첫 문장이 넘으면 자른다)
        candidate = f"{text} {sentence}".strip()
        if text and len(candidate) > max_chars:
            break
        text = candidate
    text = text[:max_chars]
    coverage = 0.0 if total == 0 else (total - dropped) / total
    return Snapped(
        text=text,
        coverage=coverage,
        snapped=snapped,
        dropped=dropped,
        page=page,
        repaired=repaired,
    )


# ── LLM 입력 · 출력 ──────────────────────────────────────────────────────────────────────────


class ExcerptPick(BaseModel):
    """LLM 구조화 출력. 필드 설명이 모델에 그대로 보인다."""

    methods: str = Field(
        description="METHODS 발췌 — 참가자(수 · 특성) · 조건과 자극 · 노출 시간 · 절차"
        "(스트레스 유도 전후 등)가 드러나는 문장을 논문에서 글자 그대로 복사한다. 실험이 "
        "여럿이면 지정한 arm의 실험만. 공백 포함 1000자 이내. 못 찾으면 빈 문자열"
    )
    results: str = Field(
        description="RESULTS 발췌 — 지정한 지표의 결과 문장(방향 · 통계 p · d · g · F · CI)을 "
        "논문에서 글자 그대로 복사한다. 공백 포함 1000자 이내. 못 찾으면 빈 문자열"
    )
    confidence: Literal["HIGH", "MEDIUM", "LOW"] = Field(
        description="HIGH: 지정한 arm · 지표의 구간을 정확히 찾았다. MEDIUM: 일부만 찾았다. "
        "LOW: 못 찾았거나 다른 실험의 구간일 수 있다"
    )
    note: str | None = Field(
        default=None, description="못 찾은 것 · 주의할 점(한국어 한 줄). 없으면 null"
    )


@dataclass
class RecordFacts:
    """시트가 적은 레코드 사실 — LLM이 어느 arm을 찾을지 안다."""

    key: str
    paper_id: str
    experiment_id: str | None
    title: str
    metadata: EvidenceMetadata
    summaries: dict[Section, str] = field(default_factory=dict)  # 시트 요약(METHODS · RESULTS)


SYSTEM_PROMPT = """너는 LUMI 근거 레이어의 발췌 담당이다. 논문 텍스트에서 지정한 레코드
(실험 arm 또는 메타분석)에 해당하는 원문 구간을 **글자 그대로** 복사한다.

규칙
1. 요약 · 번역 · 바꿔 쓰기 금지. 논문에 있는 문장을 그대로 옮긴다. 떨어진 문장을 이어
   붙여도 되지만 각 문장은 원문 그대로여야 한다. 문장 중간에서 끊지 않는다.
2. methods: 참가자(수 · 특성), 조건 · 자극(무엇을 얼마나 오래), 절차(스트레스 · 피로
   유도가 있었는지, 전/후)가 드러나는 문장. 레코드가 가리키는 실험 · 조건의 것만 고른다
   (논문에 실험이 여럿이면 라벨과 N을 보고 맞춘다).
3. results: 레코드의 지표에 해당하는 결과 문장. 효과 방향과 통계(p, d, g, F, CI)가
   있는 문장을 우선한다. 본문 문장이 없으면 표(TABLES)의 행을 그대로 옮겨도 된다.
4. 메타분석 · 체계적 고찰(SYNTHESIS)이면 methods는 포함 기준 · 검색 · 포함 연구 수,
   results는 지표별 통합 효과와 조절변수다.
5. 각 발췌는 공백 포함 1000자 이내. 핵심 문장 2~5개가 알맞다.
6. 해당 구간을 못 찾으면 빈 문자열을 주고 note에 이유를 적는다. 추측해서 만들지 않는다.
"""


def _budget(sections: dict[str, str], budget: int = INPUT_BUDGET) -> dict[str, str]:
    """입력이 길면 덜 중요한 섹션(다른 섹션 → 표 → 결과 → 방법)부터 뒤를 자른다."""
    out = dict(sections)
    for name in (OTHER_SECTIONS, "TABLES", "RESULTS", "METHODS"):
        excess = sum(len(t) for t in out.values()) - budget
        if excess <= 0:
            break
        if name in out:
            out[name] = out[name][: max(0, len(out[name]) - excess)]
    return out


def paper_sections(paper: ParsedPaper) -> dict[str, str]:
    """LLM에 줄 논문 텍스트. METHODS · RESULTS가 거의 비면 초록 · 서론 · 논의를 더한다."""
    sections = {
        "METHODS": paper.section_text(Section.METHODS),
        "RESULTS": paper.section_text(Section.RESULTS),
        "TABLES": paper.section_text(Section.TABLE),
    }
    if len(sections["METHODS"]) + len(sections["RESULTS"]) < FALLBACK_MIN:
        sections[OTHER_SECTIONS] = paper.section_text(
            Section.ABSTRACT, Section.INTRODUCTION, Section.DISCUSSION
        )
    return _budget(sections)


def build_user_prompt(facts: RecordFacts, paper: ParsedPaper) -> str:
    """LLM 사용자 메시지 — 레코드 사실 + 논문의 METHODS · RESULTS · TABLES 텍스트."""
    meta = facts.metadata
    outcomes = ", ".join(dict.fromkeys(r.outcome.value for r in meta.results)) or "—"
    media = ", ".join(m.value for m in meta.media) or "—"
    orders = ", ".join(o.value for o in meta.intervention_orders) or "미상"
    n = meta.n_detail or meta.n or "—"
    lines = [
        "## 레코드",
        f"- key: {facts.key} · 유형: {meta.record_type.value} · 라벨: {meta.arm_label}",
        f"- 매체: {media} · 지표: {outcomes} · 연구 조건: {orders} · N: {n}",
    ]
    for section in EXCERPT_SECTIONS:
        if facts.summaries.get(section):
            lines.append(f"- 시트 요약({section.value}): {facts.summaries[section]}")
    lines.append(f"\n## 논문: {facts.title} ({facts.paper_id}, {paper.pages}p)")
    for name, text in paper_sections(paper).items():
        lines.append(f"\n### {name}\n{text or '(없음)'}")
    return "\n".join(lines)


class Excerpt(BaseModel):
    """data/evidence_excerpts.json의 섹션 하나."""

    text: str
    page: int | None = None
    coverage: float
    snapped: int = 0
    dropped: int = 0
    repaired: int = 0  # 글리프 복원 수(repair_glyphs)


class RecordExcerpts(BaseModel):
    key: str
    paper_id: str
    experiment_id: str | None
    sections: dict[Section, Excerpt]  # 채택한 섹션
    rejected: dict[Section, str] = {}  # 쓰지 않은 섹션과 이유
    confidence: str
    note: str | None = None
    source: str  # 'docling P16_….pdf · gpt-4o-mini · 2026-10-27'

    def to_json(self) -> dict:
        payload: dict = {"_source": self.source, "_confidence": self.confidence}
        if self.note:
            payload["_note"] = self.note
        if self.rejected:
            payload["_rejected"] = {s.value: why for s, why in self.rejected.items()}
        for section, excerpt in self.sections.items():
            payload[section.value] = excerpt.model_dump(mode="json")
        return payload

    @property
    def review_reasons(self) -> list[str]:
        reasons = []
        if self.confidence != "HIGH":
            reasons.append(f"confidence={self.confidence}")
        for section in EXCERPT_SECTIONS:
            excerpt = self.sections.get(section)
            if section in self.rejected:
                reasons.append(f"{section.value} 안 씀({self.rejected[section]})")
            elif excerpt is None:
                reasons.append(f"{section.value} 없음")
            elif excerpt.coverage < 0.8 or len(excerpt.text) < MIN_CHARS:
                reasons.append(
                    f"{section.value} coverage={excerpt.coverage:.2f} "
                    f"len={len(excerpt.text)} dropped={excerpt.dropped}"
                )
        return reasons


def rejection(excerpt: Excerpt, confidence: str) -> str | None:
    """이 섹션을 쓰지 않을 이유. 쓰면 None."""
    if confidence == "LOW":
        return "confidence=LOW"
    if excerpt.coverage < MIN_COVERAGE:
        return f"coverage={excerpt.coverage:.2f}"
    if len(excerpt.text) < MIN_KEEP:
        return f"len={len(excerpt.text)}"
    return None


Picker = Callable[[str, str], ExcerptPick]  # (system, user) → 구조화 출력


def make_picker(*, model: str, api_key: str, timeout: float = 120.0) -> Picker:
    """설정의 OpenAI 모델로 구조화 출력(json_schema) 한 번. 쓸 때만 langchain_openai를 읽는다."""
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_openai import ChatOpenAI

    chat = ChatOpenAI(
        model=model, api_key=api_key, temperature=0, max_tokens=3000, timeout=timeout
    )
    structured = chat.with_structured_output(ExcerptPick, method="json_schema")

    def pick(system: str, user: str) -> ExcerptPick:
        return structured.invoke([SystemMessage(content=system), HumanMessage(content=user)])

    return pick


def extract_record(
    facts: RecordFacts,
    paper: ParsedPaper,
    picker: Picker,
    *,
    source: str,
    vocab: Mapping[str, int] | None = None,
) -> RecordExcerpts:
    """레코드 하나 — LLM이 고른 발췌를 본문 문장으로 맞추고(snap) 글리프를 되살린다.
    vocab은 합자 복원에 쓰는 낱말 빈도(corpus_vocab)다."""
    pick = picker(SYSTEM_PROMPT, build_user_prompt(facts, paper))
    index = SourceIndex(paper)
    sections: dict[Section, Excerpt] = {}
    rejected: dict[Section, str] = {}
    for section, raw in ((Section.METHODS, pick.methods), (Section.RESULTS, pick.results)):
        if not raw.strip():
            continue
        snapped = snap_to_source(raw, index, repair=lambda t: repair_glyphs(t, vocab))
        excerpt = Excerpt(
            text=snapped.text,
            page=snapped.page,
            coverage=round(snapped.coverage, 3),
            snapped=snapped.snapped,
            dropped=snapped.dropped,
            repaired=snapped.repaired,
        )
        why = rejection(excerpt, pick.confidence)
        if why:
            rejected[section] = why
        else:
            sections[section] = excerpt
    return RecordExcerpts(
        key=facts.key,
        paper_id=facts.paper_id,
        experiment_id=facts.experiment_id,
        sections=sections,
        rejected=rejected,
        confidence=pick.confidence,
        note=pick.note,
        source=source,
    )


def load_excerpts(payload: dict) -> dict[str, dict[Section, str]]:
    """evidence_excerpts.json → {key: {Section: text}}. seed_evidence가 content로 쓴다."""
    out: dict[str, dict[Section, str]] = {}
    for key, entry in payload.items():
        texts = {
            Section(name): value["text"]
            for name, value in entry.items()
            if not name.startswith("_") and isinstance(value, dict) and value.get("text")
        }
        if texts:
            out[key] = texts
    return out


def facts_from_seed(record, *, title: str) -> RecordFacts:
    """scripts/seed_evidence.Record → RecordFacts. 시트 요약 content를 섹션별로 넘긴다."""
    summaries = {c.section: c.content for c in record.chunks if c.section in EXCERPT_SECTIONS}
    first = record.chunks[0]
    return RecordFacts(
        key=record.key,
        paper_id=first.paper_id,
        experiment_id=first.experiment_id,
        title=title,
        metadata=record.metadata,
        summaries=summaries,
    )


def corpus_vocab(papers: Iterable[ParsedPaper]) -> dict[str, int]:
    """파싱한 논문 본문의 영어 낱말 빈도 — 합자 복원(repair_glyphs)이 고를 낱말."""
    counts: dict[str, int] = {}
    for paper in papers:
        for word in re.findall(r"[a-z]+", paper.body_text().lower()):
            counts[word] = counts.get(word, 0) + 1
    return counts
