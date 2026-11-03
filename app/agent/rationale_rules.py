"""추천 이유(CarePlan.rationale)의 글자 규칙 — 설명 프롬프트(EXPLAIN_SYSTEM_PROMPT)의 지시 중 코드로
판정되는 것. plan 노드가 설명 호출 뒤 후검증하고(sentences 말고 어기면 사유를 붙여 한 번 다시
묻는다 — nodes/planner.write_rationale), 추천 이유 평가(scripts/rationale_eval.py check)가 같은
규칙 전부로 채점한다.

- sentences: 2~3문장 — 한계를 밝히는 '다만 …' 문장이 있으면 4문장까지
- script: 글자는 한글 · 라틴 문자뿐 — 2026-10-29 plan eval 108개 중 3개에 아랍 · 데바나가리 문자가
  섞였다('अभ्यास' · 'فاص' · 저자 Mohd → 'محمد')
- codes: enum · 플래그 · 활동 ID · 근거 키 · 레코드 번호를 그대로 쓰지 않는다
- stats: p · g · SMD 같은 통계값을 쓰지 않는다
- factors: 고른 콘텐츠 Factor의 이름을 쓰지 않는다 — 화면이 따로 보인다
- experimental: '실험적'은 구성이 EXPERIMENTAL이거나 목표 지표 등급이 D뿐이거나 카드에 실험적
  배지가 있을 때만
- years: 인용한 연도가 근거 목록에 있다
- no_evidence: 근거가 없으면 찾지 못했다고 밝힌다(근거가 있으면 해당 없음 — None)
- disclosure: 꼭 밝힐 것(prompts.must_disclose — 카드 배지와 같은 판정)이 있으면 '다만 …' 문장이
  있다(없으면 해당 없음 — None). 내용을 빠짐없이 옮겼는지는 뜻을 읽어야 해서 추천 이유 평가의
  Judge(disclosure)가 본다 — 여기서는 고지 문장 자체를 빠뜨린 것만 잡는다(2026-11-03 운영 영어
  세션의 PR-06이 각색 고지를 빠뜨렸다)

영어 세션(RationaleFacts.lang = "en")은 같은 규칙을 영어 문장에 맞춰 본다 — 글자는 라틴 문자뿐,
한계 문장은 'That said' · 'However'로 시작, '실험적' 대신 'experimental', 근거 없음 · Factor 이름은
영어 표현으로(prompts.EXPLAIN_EN_NOTE).
"""

import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.content import factors as content_factors
from app.schemas.care import CareGoal, ContentType, Lang
from app.schemas.evidence import InterventionOrder, Outcome
from app.schemas.protocol import CareProtocol, Composition, Grade
from app.schemas.retrieval import GOAL_OUTCOMES, EvidenceHit, EvidenceVerdict, Flag

FACTOR_AXES = (
    content_factors.SCENERY,
    content_factors.SOUNDSCAPE,
    content_factors.ART_SUBJECT,
    content_factors.MUSIC,
)
# 설명 입력에 보이는 enum 값(목표 · 사용자 상태 · 지표 · 플래그 · 구성 · 이미지 제공).
# 매체(PMR · VR …)는 약어로 문장에 나올 수 있어 넣지 않는다
CODE_WORDS = frozenset(
    member.value
    for enum in (CareGoal, InterventionOrder, Outcome, Flag, Composition, ContentType)
    for member in enum
)
# 경계는 영문 · 숫자로만 본다 — 한글도 단어 문자라 \b는 'ADAPTED_STIMULUS가' ·
# 'PR-09를'의 끝을 못 잡는다
_UPPER_WORD = re.compile(r"(?<![A-Za-z0-9_])[A-Z][A-Z0-9_]+(?![A-Za-z0-9_])")
# 프로토콜 ID · 근거 키(P32-E1 · N26 · E11-E1) · 근거 레코드 번호([54])
_ID = re.compile(r"(?<![A-Za-z0-9])(?:PR-\d{2}|[PNE]\d{2}(?:-E\d+)?)(?!\d)|\[\d+\]")
_STATS = re.compile(
    r"(?<![A-Za-z])(?:p\s*[<=>≤≥]|[gdr]\s*=|SMD(?![A-Za-z])|CI(?![A-Za-z])|[Ft]\s*\(\s*\d)|η"
)
_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
# 마침표 뒤 공백에서 나눈다. et al. · 이니셜(J.)에서는 나누지 않는다
_SENTENCE_END = re.compile(r"(?<=[.!?])(?<!al\.)(?<!\b[A-Z]\.)\s+")
_NO_EVIDENCE = re.compile(
    r"(?:근거|연구)[^.!?]{0,20}(?:찾지 못|없|부족)|(?:찾지 못|없)[^.!?]{0,20}(?:근거|연구)"
)
# 부정어(no · not · haven't · haven’t …) 뒤의 근거 · 연구, 또는 근거 · 연구 뒤의 부족하다는 말
_NO_EVIDENCE_EN = re.compile(
    r"(?:\b(?:no|not|yet to|lack(?:s|ing)?)|n['’]t)\b[^.!?]{0,40}"
    r"\b(?:evidence|research|stud(?:y|ies))"
    r"|\b(?:evidence|research|stud(?:y|ies))\b[^.!?]{0,40}"
    r"\b(?:lacking|limited|scarce|missing|not (?:yet )?(?:been )?found)",
    re.I,
)
# 한계를 밝히는 마지막 문장의 머리말 — 한국어 '다만', 영어 'That said' · 'However'
DISCLOSE_PREFIXES: dict[Lang, tuple[str, ...]] = {"ko": ("다만",), "en": ("That said", "However")}
_EXPERIMENTAL_EN = re.compile(r"\bexperimental\b", re.I)
# 영어 추천 이유에서 고른 콘텐츠 Factor를 알아볼 말 — 카탈로그 값(키)마다. 카탈로그에 값을 더하면
# 여기도 더한다(tests/agent/test_rationale_rules.py가 지킨다). 단어 경계로 보고 복수형도 잡는다
FACTOR_TERMS_EN: dict[tuple[str, str], tuple[str, ...]] = {
    ("scenery", "forest"): ("forest",),
    ("scenery", "lake river"): ("lake", "river"),
    ("scenery", "sea"): ("sea", "ocean"),
    ("scenery", "mountain"): ("mountain", "hill"),
    ("scenery", "meadow"): ("meadow",),
    ("scenery", "flowers"): ("flower",),
    ("scenery", "sky"): ("sky", "skies"),
    ("soundscape", "birdsong"): ("birdsong", "bird"),
    ("soundscape", "stream"): ("stream",),
    ("soundscape", "rain"): ("rain",),
    ("soundscape", "ocean_waves"): ("wave",),
    ("soundscape", "forest_wind"): ("breeze", "forest wind"),
    ("art_subject", "waterscape"): ("waterscape",),
    ("art_subject", "landscape"): ("landscape",),
    ("music", "miserere"): ("Miserere", "Allegri"),
    ("music", "mozart_k448"): ("Mozart",),
    ("music", "vivaldi_spring"): ("Vivaldi", "Four Seasons"),
}


@dataclass(frozen=True)
class RationaleFacts:
    """추천 이유와 대조할 것 — 설명 호출이 받은 근거 · 활동 구성 · 목표 지표 등급 · 카드 배지 · 고른
    콘텐츠 Factor. 평가 결과 JSON(scripts/plan_eval.py)에도 같은 꼴로 남는다."""

    evidence: tuple[Mapping[str, Any], ...] = ()  # evidence_row 꼴
    composition: str | None = None
    grades: Mapping[str, str] = field(default_factory=dict)  # 목표 1차 지표 → 등급
    badges: tuple[str, ...] = ()
    factors: Mapping[str, Any] | None = None  # ContentFactors.model_dump(mode="json")
    lang: Lang = "ko"  # 추천 이유의 언어(세션 언어)
    disclose: tuple[str, ...] = ()  # 꼭 밝힐 것(prompts.must_disclose) — 설명 입력에 실은 문장


def evidence_row(hit: EvidenceHit, verdict: EvidenceVerdict) -> dict[str, Any]:
    """근거 하나 — 추천 이유의 인용(저자 · 연도)과 지표 · 플래그를 대조할 것만."""
    return {
        "id": hit.evidence_record_id,
        "key": hit.experiment_id or hit.paper.paper_id,
        "authors": hit.paper.authors,
        "year": hit.paper.year,
        "supports_goal": verdict.supports_goal,
        "outcomes": [o.value for o in verdict.supported_outcomes],
        "flags": [f.value for f in verdict.flags],
    }


def facts_for(
    protocol: CareProtocol,
    goal: CareGoal | None,
    cited: Sequence[tuple[EvidenceHit, EvidenceVerdict]],
    badges: Sequence[str] = (),
    factors: Mapping[str, Any] | None = None,
    lang: Lang = "ko",
    disclose: Sequence[str] = (),
) -> RationaleFacts:
    """plan 노드의 값으로 대조할 것을 만든다."""
    primary = GOAL_OUTCOMES[goal].primary if goal else []
    return RationaleFacts(
        evidence=tuple(evidence_row(hit, verdict) for hit, verdict in cited),
        composition=protocol.composition.value,
        grades={o.value: protocol.grades[o].grade.value for o in primary},
        badges=tuple(badges),
        factors=factors,
        lang=lang,
        disclose=tuple(disclose),
    )


RuleResult = tuple[bool | None, str]  # (통과 · 실패 · 해당 없음 None, 실패 내용)


def sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_END.split(text.strip()) if s]


def check_sentences(text: str, facts: RationaleFacts) -> RuleResult:
    """2~3문장. 한계를 밝히는 '다만 …' 문장(셋째 이후)이 있으면 4문장까지 — 꼭 밝힐 것을 넣으면
    문장이 하나 는다(2026-10-29 plan eval 36개 중 7개)."""
    found = sentences(text)
    prefixes = DISCLOSE_PREFIXES[facts.lang]
    limit = 4 if any(s.startswith(prefixes) for s in found[2:]) else 3
    return 2 <= len(found) <= limit, f"{len(found)}문장"


def check_script(text: str, facts: RationaleFacts) -> RuleResult:
    """글자는 한글 · 라틴 문자(저자 이름의 ö · é 포함)뿐이다 — 영어 세션은 라틴 문자뿐. 숫자 ·
    부호 · 기호는 보지 않는다."""
    allowed = ("HANGUL", "LATIN") if facts.lang == "ko" else ("LATIN",)
    foreign = [
        ch
        for ch in text
        if unicodedata.category(ch).startswith("L")
        and not unicodedata.name(ch, "").startswith(allowed)
    ]
    return not foreign, "".join(dict.fromkeys(foreign))


def check_codes(text: str, facts: RationaleFacts) -> RuleResult:
    words = [w for w in _UPPER_WORD.findall(text) if w in CODE_WORDS]
    found = list(dict.fromkeys(words + _ID.findall(text)))
    return not found, ", ".join(found)


def check_stats(text: str, facts: RationaleFacts) -> RuleResult:
    found = [m.group(0).strip() for m in _STATS.finditer(text)]
    return not found, ", ".join(found)


def factor_labels(factors: Mapping[str, Any] | None) -> list[str]:
    """고른 콘텐츠 Factor 값의 화면 이름(카탈로그 label_ko)."""
    labels = []
    for axis in FACTOR_AXES:
        keys = (factors or {}).get(axis) or []
        for key in keys if isinstance(keys, list) else [keys]:
            found = content_factors.value(axis, key)
            labels.append(found.label_ko if found else key)
    return labels


def factor_terms_en(factors: Mapping[str, Any] | None) -> list[str]:
    """고른 콘텐츠 Factor 값을 영어 문장에서 알아볼 말(FACTOR_TERMS_EN)."""
    terms = []
    for axis in FACTOR_AXES:
        keys = (factors or {}).get(axis) or []
        for key in keys if isinstance(keys, list) else [keys]:
            terms += FACTOR_TERMS_EN.get((axis, key), (key,))
    return terms


def check_factors(text: str, facts: RationaleFacts) -> RuleResult:
    if facts.lang == "ko":
        found = [label for label in factor_labels(facts.factors) if label in text]
    else:
        found = [
            term
            for term in factor_terms_en(facts.factors)
            if re.search(rf"\b{re.escape(term)}(?:e?s)?\b", text, re.I)
        ]
    return not found, ", ".join(dict.fromkeys(found))


def experimental_allowed(facts: RationaleFacts) -> bool:
    """'실험적'을 써도 되는가 — 설명 프롬프트의 조건(구성 · 목표 지표 등급)이나 카드 배지."""
    grades = list(facts.grades.values())
    only_d = bool(grades) and all(g == Grade.D.value for g in grades)
    return (
        facts.composition == Composition.EXPERIMENTAL.value
        or "EXPERIMENTAL" in facts.badges
        or only_d
    )


def check_experimental(text: str, facts: RationaleFacts) -> RuleResult:
    used = "실험적" in text if facts.lang == "ko" else bool(_EXPERIMENTAL_EN.search(text))
    if not used:
        return True, ""
    return experimental_allowed(facts), f"구성 {facts.composition} · 등급 {dict(facts.grades)}"


def check_years(text: str, facts: RationaleFacts) -> RuleResult:
    cited = {int(y) for y in _YEAR.findall(text)}
    known = {e["year"] for e in facts.evidence if e.get("year")}
    unknown = sorted(cited - known)
    return not unknown, ", ".join(map(str, unknown))


def check_no_evidence(text: str, facts: RationaleFacts) -> RuleResult:
    if facts.evidence:
        return None, ""
    pattern = _NO_EVIDENCE if facts.lang == "ko" else _NO_EVIDENCE_EN
    return bool(pattern.search(text)), "근거가 없다는 말이 없다"


def check_disclosure(text: str, facts: RationaleFacts) -> RuleResult:
    """꼭 밝힐 것이 있으면 '다만'(영어 'That said' · 'However')으로 시작하는 문장이 있다."""
    if not facts.disclose:
        return None, ""
    prefixes = DISCLOSE_PREFIXES[facts.lang]
    return any(s.startswith(prefixes) for s in sentences(text)), "; ".join(facts.disclose)


RULES: dict[str, Callable[[str, RationaleFacts], RuleResult]] = {
    "sentences": check_sentences,
    "script": check_script,
    "codes": check_codes,
    "stats": check_stats,
    "factors": check_factors,
    "experimental": check_experimental,
    "years": check_years,
    "no_evidence": check_no_evidence,
    "disclosure": check_disclosure,
}
CHECKS = tuple(RULES)
# plan 노드가 어기면 설명을 다시 묻는 규칙 — 문장 수는 보기 문제라 호출을 더 쓰지 않는다
# (평가만 센다)
RUNTIME_CHECKS = tuple(name for name in CHECKS if name != "sentences")

# 설명을 다시 물을 때 입력에 붙이는 사유 — 규칙마다 한 문장. {}는 실패 내용
_PROBLEMS = {
    "sentences": "추천 이유가 2~3문장(한계를 밝히는 '다만' 문장이 있으면 4문장)이 아니다({})",
    "script": "한국어 문장에 다른 문자({})가 섞였다 — 한국어와 저자 이름(라틴 문자)만 쓴다",
    "codes": "코드 · ID({})를 그대로 썼다 — 뜻으로 풀어 쓴다",
    "stats": "통계값({})을 썼다",
    "factors": "콘텐츠 선택({})을 추천 이유에 썼다 — 화면이 따로 보인다",
    "experimental": "'실험적'을 조건 밖에서 썼다",
    "years": "근거 목록에 없는 연도({})를 인용했다",
    "no_evidence": "검증된 근거가 없다는 것을 밝히지 않았다",
    "disclosure": (
        "꼭 밝힐 것({})을 '다만 …' 한 문장으로 마지막에 밝히지 않았다 — 빠짐없이 모아 쓴다"
    ),
}
# 영어 세션에서 말이 달라지는 사유 — 나머지는 한국어 세션과 같다(사유는 LLM에 다시 묻는 입력이다)
_PROBLEMS_EN = {
    **_PROBLEMS,
    "sentences": (
        "추천 이유가 2~3문장(한계를 밝히는 'That said' 문장이 있으면 4문장)이 아니다({})"
    ),
    "script": "영어 문장에 다른 문자({})가 섞였다 — 영어로만 쓰고 저자 이름은 원문 그대로 둔다",
    "experimental": "'experimental'을 조건 밖에서 썼다 — 연구는 'study' · 'experiment'로 쓴다",
    "disclosure": (
        "꼭 밝힐 것({})을 'That said, …' 한 문장으로 마지막에 밝히지 않았다 — 빠짐없이 모아 쓴다"
    ),
}
PROBLEMS: dict[Lang, dict[str, str]] = {"ko": _PROBLEMS, "en": _PROBLEMS_EN}


def check(text: str, facts: RationaleFacts) -> dict[str, RuleResult]:
    """규칙마다 (통과 · 실패 · 해당 없음, 실패 내용)."""
    return {name: rule(text, facts) for name, rule in RULES.items()}


def problems(text: str, facts: RationaleFacts, names: Sequence[str] = RUNTIME_CHECKS) -> list[str]:
    """names 규칙 중 어긴 것의 사유 — 비어 있으면 통과."""
    results = check(text, facts)
    reasons = PROBLEMS[facts.lang]
    return [reasons[name].format(results[name][1]) for name in names if results[name][0] is False]
