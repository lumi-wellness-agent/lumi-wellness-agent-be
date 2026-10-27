"""워크북(프로토콜 · 근거 arm 시트) → 활동 노드 JSON 초안. data/protocols/PR-*.json에 쓴다.

    .venv/bin/python scripts/scaffold_protocols.py <xlsx> [--force] [--only PR-02,PR-03]

- 시트의 열을 JSON 상위 필드로 옮기고(이름·목표·매체·구성·순서 조건·시간·근거·등급·제한),
  근거 arm 행을 evidence[]로, 등급 셀의 괄호(지지·상충 논문)를 grades.support/conflicts로 푼다.
- 측정 지표 열의 키워드로 각 근거가 측정한 Outcome을 정한다. 등급 셀이 근거로 든 논문이 그 지표를
  측정하지 않은 것으로 보이면 지표를 더하고 review.open_items에 남긴다 — 담당자가 원문으로 확정한다.
- steps · content_spec · guide는 매체에서 기계적으로 만든 초안이다. selection · render ·
  personalization ·
  generation 블록은 비워 두었다(PR-01.json이 완성 예시).
- 이미 있는 파일은 건너뛴다(--force로 덮어씀). PR-01은 손으로 쓴 기준 파일이라 --force에도
  건드리지 않는다.
- 표준 라이브러리만 쓴다(xlsx는 zip + XML). 저장소 루트에서 가상환경 python으로 실행한다.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.schemas.care import CareGoal  # noqa: E402
from app.schemas.evidence import (  # noqa: E402
    DESIGN_LABELS,
    GRADED_OUTCOMES,
    OUTCOME_LABELS,
    Medium,
    Outcome,
    StudyDesign,
    parse_media,
    parse_orders,
)
from app.schemas.measure import GOAL_SCALE, InstrumentId  # noqa: E402
from app.schemas.protocol import CareProtocol, Composition, EvidenceRole, Grade  # noqa: E402

OUT_DIR = ROOT / "data" / "protocols"
KEEP = {"PR-01"}  # 손으로 쓴 기준 파일
# 시트가 부르는 도구 이름 → data/instruments.
KNOWN_TOOLS = {i.value for i in InstrumentId}

# ── xlsx 읽기 ─────────────────────────────────────────────────────────────────────────────

M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def read_sheet(xlsx: Path, name: str, id_col: str) -> list[dict[str, str]]:
    z = zipfile.ZipFile(xlsx)
    shared: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        shared = ["".join(t.text or "" for t in si.iter(M + "t")) for si in root.findall(M + "si")]
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    targets = {r.get("Id"): r.get("Target") for r in rels}
    sheet = next(s for s in wb.find(M + "sheets") if s.get("name") == name)
    path = "xl/" + targets[sheet.get(R)].lstrip("/").replace("xl/", "")

    def value(c: ET.Element) -> str:
        if c.get("t") == "inlineStr":
            return "".join(t.text or "" for t in c.iter(M + "t"))
        v = c.find(M + "v")
        if v is None:
            return ""
        return shared[int(v.text)] if c.get("t") == "s" else (v.text or "")

    rows = []
    for row in ET.fromstring(z.read(path)).iter(M + "row"):
        cells = {re.match(r"([A-Z]+)", c.get("r")).group(1): value(c) for c in row.findall(M + "c")}
        rows.append(cells)
    header_at = next(i for i, r in enumerate(rows) if r.get("A") == id_col)
    header = rows[header_at]
    cols = sorted(header, key=lambda k: (len(k), k))
    return [
        {header[c]: r.get(c, "").strip() for c in cols} for r in rows[header_at + 1 :] if r.get("A")
    ]


# ── 라벨 → 값 ─────────────────────────────────────────────────────────────────────────────

COMPOSITION = {
    "단일": Composition.SINGLE,
    "검증된 다중매체": Composition.VALIDATED_MULTIMODAL,
    "실험적": Composition.EXPERIMENTAL,
    "단계 모듈": Composition.STAGE_MODULE,
}
CHANNEL = {
    "앱 내": "IN_APP",
    "야외": "OUTDOOR",
    "야외+앱": "OUTDOOR_APP",
    "앱 내(디지털) / 종이": "IN_APP_OR_PAPER",
}
SLOTS_FOR: dict[Medium, list[str]] = {
    Medium.STATIC_IMAGE: ["VISUAL_STILL"],
    Medium.AI_IMAGE: ["VISUAL_STILL"],
    Medium.VIDEO: ["VISUAL_VIDEO"],
    Medium.VR: ["VISUAL_VIDEO"],
    Medium.NATURE_SOUND: ["SOUNDSCAPE"],
    Medium.MUSIC: ["MUSIC"],
    Medium.OUTDOOR_WALK: ["OUTDOOR_TASK"],
    Medium.BREATHING: ["GUIDE_AUDIO", "PACER"],
    Medium.PMR: ["GUIDE_AUDIO"],
    Medium.GUIDED_IMAGERY: ["GUIDE_AUDIO"],
    Medium.MINDFULNESS: ["GUIDE_AUDIO"],
    Medium.NOTICING: ["WRITING_PROMPT"],
    Medium.COLORING: ["COLORING_TEMPLATE"],
    Medium.WRITING: ["WRITING_PROMPT"],
    Medium.GROUNDING: ["GUIDE_AUDIO"],
    Medium.ART_VIEWING: ["ARTWORK"],
    Medium.CBT: ["GUIDE_AUDIO"],
}
MEDIA_POLICY = {"curated": "ALLOWED", "generated": "ALLOWED_WITH_FLAG", "user_upload": "FORBIDDEN"}
TEXT_POLICY = {"fixed_text": "ALLOWED", "tts": "ALLOWED", "llm_rewrite": "FORBIDDEN"}
MEDIA_SLOTS = {
    "VISUAL_STILL",
    "VISUAL_VIDEO",
    "SOUNDSCAPE",
    "MUSIC",
    "ARTWORK",
    "COLORING_TEMPLATE",
}
VISUAL_SLOTS = {"VISUAL_STILL", "VISUAL_VIDEO", "ARTWORK", "COLORING_TEMPLATE"}
AUDIO_SLOTS = {"SOUNDSCAPE", "MUSIC", "GUIDE_AUDIO"}

# 측정 지표 열 → Outcome. 순서대로 부분 일치. 괄호 안 설명은 PR-01 · 근거 arm 시트의 실제 값
OUTCOME_KEYWORDS: list[tuple[Outcome, list[str]]] = [
    (
        Outcome.AFFECT,
        ["PA", "NA", "정서", "기분", "ZIPERS", "이완", "에너지", "동기", "PANAS", "분노"],
    ),
    (Outcome.SUBJECTIVE_STRESS, ["불안", "STAI", "TAQ", "스트레스", "주관", "이완", "긴장"]),
    (Outcome.RUMINATION, ["반추", "RRQ", "SRQ", "RNT", "Thoughts Questionnaire", "BSRI"]),
    (
        Outcome.ATTENTION,
        [
            "SART",
            "BDS",
            "DSB",
            "DSF",
            "Digit Span",
            "n-back",
            "TMT",
            "LDST",
            "Posner",
            "주의",
            "인지",
            "기억",
            "Necker",
        ],
    ),
    (
        Outcome.PHYSIO,
        [
            "코르티솔",
            "HRV",
            "HR",
            "혈압",
            "SCL",
            "SCR",
            "EDA",
            "fMRI",
            "편도체",
            "RVLPFC",
            "sgPFC",
            "호흡수",
            "RMSSD",
            "RSA",
            "PEP",
            "PTT",
            "근전도",
            "심박",
            "IBI",
            "TWA",
            "sAA",
            "생리",
            "활성",
            "DMN",
        ],
    ),
    (
        Outcome.OTHER,
        [
            "웰빙",
            "SWB",
            "행복",
            "우울",
            "몰입",
            "자연 연결감",
            "외로움",
            "즐거움",
            "마음챙김",
            "탈중심화",
            "회복성",
            "지각된 회복",
            "탈락",
            "장기",
            "방법",
            "건강",
            "증상",
            "주관적 효과",
            "의미감",
            "순응",
            "과제 수행",
        ],
    ),
]
NEGATIVE = [
    "비유의",
    "차이 없음",
    "효과 없음",
    "재현 실패",
    "불안정",
    "이점 없음",
    "비효과",
    "없음",
]
POSITIVE = ["↑", "↓", "향상", "개선", "감소", "증가", "유의", "효과적", "지속", "빠름", "회복"]
CONTEXT_MARKERS = ["제약 조건 출처", "선별 참고", "콘텐츠 선별"]
PRIMARY_DESIGNS = {StudyDesign.RANDOMIZED_EXPERIMENT, StudyDesign.QUASI_EXPERIMENT}
ID = re.compile(r"[PEN][0-9]{2}(?:-E[1-9][0-9]*)?")
PR = re.compile(r"PR-[0-9]{2}")
BLANK = {"", "—", "-", "–", "없음"}


def blank(text: str) -> bool:
    return text.strip() in BLANK


def split_items(text: str) -> list[str]:
    if blank(text):
        return []
    parts = re.split(r";|\.\s+(?=[A-Za-z가-힣])|,\s+(?=[A-Z가-힣])", text)
    return [p.strip().rstrip(".") for p in parts if p.strip() and not blank(p)]


def map_outcomes(text: str) -> list[Outcome]:
    out: list[Outcome] = []
    for outcome, keys in OUTCOME_KEYWORDS:
        if any(k in text for k in keys) and outcome not in out:
            out.append(outcome)
    return out


def negative_only(text: str) -> bool:
    has_neg = any(n in text for n in NEGATIVE)
    stripped = text
    for n in NEGATIVE:
        stripped = stripped.replace(n, "")
    has_pos = any(p in stripped for p in POSITIVE)
    return has_neg and not has_pos


def parse_minutes(text: str) -> list[float]:
    """'약 6분' '3–5분' '30초–1분' '20분 × 3일' → 분 단위 값. 등장 순서, 중복 제거."""
    values: list[float] = []
    for a, b in re.findall(r"(\d+(?:\.\d+)?)\s*[–\-~]\s*(\d+(?:\.\d+)?)\s*분", text):
        values += [float(a), float(b)]
    for m in re.finditer(r"(?<![\d.])(\d+(?:\.\d+)?)\s*분", text):
        values.append(float(m.group(1)))
    if not values:
        values = [int(s) / 60 for s in re.findall(r"(\d+)\s*초", text)]
    seen: list[float] = []
    for v in values:
        if v not in seen:
            seen.append(v)
    return seen


def parse_n(text: str) -> int | str | None:
    t = text.strip()
    if blank(t) or t == "원문 확인":
        return None
    outside = re.sub(r"\(.*?\)", "", t)
    if any(x in outside for x in ("/", "편", "k=", "결과")):
        return t
    m = re.match(r"^(\d+)", t)
    return int(m.group(1)) if m else t


def particle(word: str) -> str:
    code = ord(word[-1])
    if 0xAC00 <= code <= 0xD7A3:
        return "을" if (code - 0xAC00) % 28 else "를"
    return "을(를)"


# ── 근거 ──────────────────────────────────────────────────────────────────────────────────


def build_evidence(
    pid: str, arms: list[dict[str, str]], limits_cell: str, open_items: list[str]
) -> list[dict]:
    refs: list[dict] = []
    for arm in arms:
        paper = arm["논문 ID"].strip()
        label = arm["arm / experiment_id"].strip()
        design = DESIGN_LABELS.get(arm["논문 유형"].strip())
        if design is None:
            raise ValueError(f"{pid} {paper}: 모르는 논문 유형 {arm['논문 유형']!r}")
        m = re.match(r"^([PEN]\d{2})-E(\d+)", label)
        experiment_id = f"{m.group(1)}-E{int(m.group(2))}" if m else None
        if experiment_id is None and design in PRIMARY_DESIGNS:
            experiment_id = f"{paper}-E1"
            open_items.append(
                f"{paper}: arm 표기 '{label}'에 -E 번호가 없어 {experiment_id}로 두었다. 원문 확인"
            )
        note = arm.get("비고", "")
        result = arm["결과"]
        if any(k in note for k in CONTEXT_MARKERS) or design is StudyDesign.OBSERVATIONAL:
            role = EvidenceRole.CONTEXT
        elif design is StudyDesign.SYSTEMATIC_REVIEW_META:
            role = EvidenceRole.CONFLICT if negative_only(result) else EvidenceRole.META
        elif (
            "상충" in note
            or negative_only(result)
            or (paper in limits_cell and negative_only(result))
        ):
            role = EvidenceRole.CONFLICT
        else:
            role = EvidenceRole.PRIMARY
        if role is EvidenceRole.META:
            experiment_id = None
        ref = {
            "paper_id": paper,
            "experiment_id": experiment_id,
            "role": role.value,
            "design": design.value,
            "n": parse_n(arm["N"]),
            "order": arm["순서"].strip() or None,
            "population": None if blank(arm["대상"]) else arm["대상"],
            "stimulus": None if blank(arm["중재(자극·시간)"]) else arm["중재(자극·시간)"],
            "outcomes": [o.value for o in map_outcomes(arm["측정 지표"])],
            "result": result or None,
            "verification": arm["확인 수준"].strip(),
            "evidence_record_ids": [],
        }
        if ref["order"] is not None:
            parse_orders(ref["order"])  # 모르는 라벨이면 여기서 터진다
        key = experiment_id or paper
        if any((r["experiment_id"] or r["paper_id"]) == key for r in refs):
            open_items.append(f"{key}: 근거 arm 행이 둘이라 뒤 행을 버렸다")
            continue
        refs.append(ref)
    return refs


def resolve_key(token: str, refs: list[dict]) -> str | None:
    """등급 셀의 'P24' · 'P24-E1'을 이 프로토콜 evidence[]의 키로."""
    keys = [r["experiment_id"] or r["paper_id"] for r in refs]
    if token in keys:
        return token
    paper = token.split("-")[0]
    arms = [k for k in keys if k.startswith(paper + "-")]
    if arms:
        return arms[0]
    return paper if paper in keys else None


def build_grades(row: dict[str, str], refs: list[dict], open_items: list[str]) -> dict[str, dict]:
    by_key = {(r["experiment_id"] or r["paper_id"]): r for r in refs}
    grades: dict[str, dict] = {}
    for label, outcome in OUTCOME_LABELS.items():
        cell = row.get(label, "").strip()
        head, _, rest = cell.partition("(")
        token = head.strip() or "—"
        paren = rest.rstrip(")").strip()
        grade = {"—": Grade.NONE, "미확인": Grade.PENDING, "원문 확인": Grade.PENDING}.get(token)
        if grade is None:
            grade = Grade(token)
        support: list[str] = []
        conflicts: list[str] = []
        if paren and grade is not Grade.NONE:
            before, _, after = paren.partition("상충")
            conflict_segment = after.split(";")[0] if after else ""
            support_text = before + (after[len(conflict_segment) :] if after else "")
            if grade is Grade.D:
                conflict_segment, support_text = paren, ""
            for seg, bucket in ((conflict_segment, conflicts), (support_text, support)):
                for token_id in ID.findall(seg):
                    key = resolve_key(token_id, refs)
                    if key is None:
                        open_items.append(
                            f"grades.{outcome.value}: 등급 셀의 {token_id}가 근거 arm에 없다"
                        )
                        continue
                    if outcome.value not in by_key[key]["outcomes"]:
                        by_key[key]["outcomes"].append(outcome.value)
                        open_items.append(
                            f"grades.{outcome.value}: 등급 셀이 {key}를 들지만 측정 지표 열에서 "
                            f"{outcome.value}를 못 찾아 outcomes에 더했다. 원문 확인"
                        )
                    if key not in bucket:
                        bucket.append(key)
        conflicts = [k for k in conflicts if k not in support] or conflicts
        support = [k for k in support if k not in conflicts]
        if grade in (Grade.A, Grade.B, Grade.C) and not support:
            support = [
                k
                for k, r in by_key.items()
                if r["role"] in (EvidenceRole.PRIMARY.value, EvidenceRole.META.value)
                and outcome.value in r["outcomes"]
                and k not in conflicts
            ]
            if not support:
                open_items.append(
                    f"grades.{outcome.value}: 시트 등급 {grade.value}인데 지지 근거를 못 찾아 "
                    "미확인으로 두었다"
                )
                grade = Grade.PENDING
        if grade is Grade.D and not conflicts:
            conflicts = [
                k
                for k, r in by_key.items()
                if r["role"] == EvidenceRole.CONFLICT.value and outcome.value in r["outcomes"]
            ]
            if not conflicts:
                open_items.append(
                    f"grades.{outcome.value}: 시트 등급 D인데 상충 근거를 못 찾아 미확인으로 두었다"
                )
                grade = Grade.PENDING
        if grade is Grade.NONE:
            support, conflicts = [], []
        grades[outcome.value] = {
            "grade": grade.value,
            "support": support,
            "conflicts": conflicts,
            "note": paren or None,
        }
    return grades


# ── 프로토콜 ──────────────────────────────────────────────────────────────────────────────


def build_steps(
    text: str, composition: Composition, default_minutes: float, media: list[Medium]
) -> list[dict]:
    activity_slots: list[str] = []
    for medium in media:
        for slot in SLOTS_FOR.get(medium, []):
            if slot not in activity_slots:
                activity_slots.append(slot)
    if composition is Composition.STAGE_MODULE:
        label = text.split(":")[-1].strip() or "지금 감정을 한 단어로 명명"
        return [
            {
                "order": 1,
                "type": "NOTICE",
                "label": label,
                "minutes": default_minutes,
                "slots": ["NOTICE_PROMPT"],
                "optional": False,
                "note": "다른 프로토콜의 NOTICE 단계로 끼워 쓴다. 한 단어 응답, 점수화하지 않음",
            }
        ]
    steps: list[dict] = []
    tokens = [t.strip() for t in text.split("→")]
    for token in tokens:
        if "MEASURE" in token or token.startswith("["):
            continue
        token = re.sub(r"^[^A-Z\[]*(?=ACTIVITY|NOTICE|CHOOSE)", "", token)
        m = re.match(r"^(ACTIVITY|NOTICE|CHOOSE)\s*(?:\((.*?)\))?", token)
        if not m:
            continue
        kind, label = m.group(1), (m.group(2) or "").strip()
        if kind == "ACTIVITY":
            steps.append(
                {
                    "type": "ACTIVITY",
                    "label": label or "활동",
                    "minutes": default_minutes,
                    "slots": activity_slots,
                    "user_can_stop": True,
                }
            )
        elif kind == "NOTICE":
            optional = label in ("", "선택")
            steps.append(
                {
                    "type": "NOTICE",
                    "label": "지금 느낌 알아차리기" if optional else f"{label} 알아차리기",
                    "minutes": 0.5,
                    "slots": ["NOTICE_PROMPT"],
                    "optional": True,
                    "note": "한 단어 응답. 점수화하지 않음(E10 정서 명명 참고)",
                }
            )
    for i, step in enumerate(steps, start=1):
        step["order"] = i
    return [{"order": s.pop("order"), **s} for s in steps]


def build_protocol(row: dict[str, str], arms: list[dict[str, str]], today: str) -> dict:
    pid = row["protocol_id"].strip()
    open_items: list[str] = [
        "초안: scripts/scaffold_protocols.py가 시트에서 만들었다. "
        "steps · content_spec · guide 문안 검수 필요"
    ]
    name = row["프로토콜"].strip()
    base_name = re.sub(r"\(.*?\)$", "", name).strip()
    goals = []
    for g in re.findall(r"S[123]", row["주 목표"]):
        if g not in goals:
            goals.append(g)
    goal, secondary = goals[0], goals[1:]
    media = parse_media(row["매체(BE Medium)"])
    composition = COMPOSITION[row["구성 유형"].strip()]
    orders = parse_orders(row["순서 조건(intervention_order)"])

    minutes = parse_minutes(row["활동 시간"]) or parse_minutes(row["단계(steps)"])
    if not minutes:
        minutes = [10.0]
        open_items.append("활동 시간을 시트에서 못 읽어 10분으로 두었다(원문 확인)")
    default = minutes[0]
    lo, hi = min(minutes), max(minutes)
    duration = {
        "default": default,
        "min": lo if len(minutes) > 1 else None,
        "max": hi if len(minutes) > 1 else None,
        "evidence": row["활동 시간"] or None,
    }

    steps = build_steps(row["단계(steps)"], composition, default, media)
    slot_types: list[str] = []
    for step in steps:
        for slot in step["slots"]:
            if slot not in slot_types:
                slot_types.append(slot)
    limits = row["상충·제한 근거"]
    evidence = build_evidence(pid, arms, limits, open_items)
    grades = build_grades(row, evidence, open_items)
    stimuli = "; ".join(
        f"{r['experiment_id'] or r['paper_id']}: {r['stimulus']}"
        for r in evidence
        if r["stimulus"] and r["role"] in (EvidenceRole.PRIMARY.value, EvidenceRole.CONFLICT.value)
    )
    slots = []
    for slot in slot_types:
        optional_notice = slot == "NOTICE_PROMPT" and any(
            s["type"] == "NOTICE" and s.get("optional") for s in steps
        )
        slots.append(
            {
                "slot": slot,
                "required": not optional_notice,
                "evidence_stimulus": stimuli or None,
                "source_policy": MEDIA_POLICY if slot in MEDIA_SLOTS else TEXT_POLICY,
                "note": "초안: selection · render · personalization · generation은 담당자가 "
                "채운다(PR-01.json 참고)",
            }
        )

    measure_cell = row["사전·사후 측정 노드"].strip()
    anchor = "INS-SAM" if "INS-SAM" in measure_cell else None
    extra_tools = [t for t in re.findall(r"INS-[A-Z0-9-]+", measure_cell) if t not in KNOWN_TOOLS]
    if extra_tools:
        open_items.append(
            f"시트의 측정 도구 {extra_tools}는 data/instruments 묶음 밖이라 넣지 않았다"
        )
    measures = {
        "anchor": anchor,
        "goal_scale_by_goal": {
            g: (GOAL_SCALE[CareGoal(g)].value if GOAL_SCALE[CareGoal(g)] else None) for g in goals
        },
        "schedule": {
            "pre": ["anchor", "goal_scale"],
            "post_round": ["anchor"],
            "session_end": ["goal_scale"],
        }
        if anchor
        else {},
        "note": f"시트: {measure_cell or '—'}. 측정은 노드 바깥(measure_pre / measure_post 코드 "
        "노드)에서 실행한다. 프로토콜은 어떤 도구를 쓰는지만 가리킨다",
    }

    needs = []
    if slot_types and set(slot_types) & VISUAL_SLOTS:
        needs.append("SCREEN")
    if set(slot_types) & AUDIO_SLOTS:
        needs.append("AUDIO")
    if "OUTDOOR_TASK" in slot_types:
        needs.append("OUTDOORS")
    channel = CHANNEL.get(row["채널"].strip(), "IN_APP")
    eligibility = {
        "min_minutes_available": lo if len(minutes) > 1 else default,
        "needs": needs,
        "place": "OUTDOOR" if channel in ("OUTDOOR", "OUTDOOR_APP") else "ANY",
        "quiet_required": "AUDIO" in needs,
        "not_recommended_if": [],
    }
    guide = {
        "intro_text": f"지금부터 {base_name}{particle(base_name)} 약 {default:g}분 동안 합니다. "
        "편한 자세로 시작하고, 불편하면 언제든 멈출 수 있습니다.",
        "intro_text_ref": f"guide/{pid.lower().replace('-', '')}_intro_ko",
        "max_seconds": 15,
        "source_policy": TEXT_POLICY,
        "during_activity": "GUIDE_AUDIO" if "GUIDE_AUDIO" in slot_types else "NONE",
        "note": "초안 문안. 활동 중 안내 음성이 자극 자체인 경우(호흡·PMR·심상·마음챙김)만 "
        "GUIDE_AUDIO 슬롯을 쓴다",
    }

    safety_items = split_items(row["필수 제한·안전"])
    compatible = [
        {
            "protocol_id": p,
            "composition": "EXPERIMENTAL" if "실험적" in row["병행 가능(검증된 조합만)"] else None,
            "basis": row["병행 가능(검증된 조합만)"],
        }
        for p in dict.fromkeys(PR.findall(row["병행 가능(검증된 조합만)"]))
        if p != pid
    ]
    exclusive = [
        {"protocol_id": p, "reason": limits}
        for p in dict.fromkeys(PR.findall(limits))
        if "혼용 금지" in limits and p != pid
    ]
    letters = {g["grade"] for g in grades.values()}
    if composition is Composition.EXPERIMENTAL:
        flag = "EXPERIMENTAL"
    elif letters & {"C", "D"} and not letters & {"A", "B"}:
        flag = "LIMITED_EVIDENCE"
    else:
        flag = None
    if not blank(row.get("비고", "")):
        open_items.append(f"시트 비고: {row['비고']}")
    population = [p.strip() for p in row["검증 집단"].split("·") if p.strip() and not blank(p)]
    media_label = ", ".join(m.value for m in media) or "없음(단계 모듈)"

    return {
        "schema_version": "1.0",
        "record_type": "stage_module"
        if composition is Composition.STAGE_MODULE
        else "care_protocol",
        "protocol_id": pid,
        "name": name,
        "name_en": None,
        "summary": f"{base_name}. 약 {default:g}분. 매체 {media_label}. "
        f"허용 사용자 상태 {', '.join(o.value for o in orders) or '제한 없음'}.",
        "goal": goal,
        "secondary_goals": secondary,
        "media": [m.value for m in media],
        "composition": composition.value,
        "channel": channel,
        "intervention_order": [o.value for o in orders],
        "duration_minutes": duration,
        "eligibility": eligibility,
        "guide": guide,
        "steps": steps,
        "measures": measures,
        "content_spec": {"slots": slots},
        "grades": grades,
        "paper_id_scheme": "RC_MASTER",
        "evidence": evidence,
        "constraints": split_items(limits),
        "safety": safety_items,
        "unverified_claims": split_items(row["미검증 주장(표시 필요)"]),
        "population_validated": population,
        "population_note": None
        if any("한국" in p for p in population)
        else "한국 성인 대상 검증 없음",
        "compatible_with": compatible,
        "exclusive_with": exclusive,
        "user_facing_flags": {
            "default": flag,
            "by_goal": {},
            "when_generated_assets": "AI_IMAGE_UNVERIFIED"
            if set(slot_types) & {"VISUAL_STILL", "VISUAL_VIDEO"}
            else None,
        },
        "review": {
            "status": "DRAFT",
            "open_items": open_items,
            "source": f"LUMI_측정도구_프로토콜_2026-10-23.xlsx › 프로토콜 {pid}, "
            f"근거 arm {len(arms)}행",
            "reviewed_by": None,
            "updated_at": today,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("xlsx", type=Path)
    parser.add_argument("--force", action="store_true", help="있는 파일도 덮어쓴다(PR-01 제외)")
    parser.add_argument("--only", default="", help="쉼표로 나눈 protocol_id만")
    args = parser.parse_args()
    protocols = read_sheet(args.xlsx, "프로토콜", "protocol_id")
    arms = read_sheet(args.xlsx, "근거 arm", "protocol_id")
    only = {p.strip() for p in args.only.split(",") if p.strip()}
    today = dt.date.today().isoformat()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    failed = 0
    for row in protocols:
        pid = row["protocol_id"].strip()
        if only and pid not in only:
            continue
        path = OUT_DIR / f"{pid}.json"
        if pid in KEEP or (path.exists() and not args.force):
            print(f"skip  {pid}")
            continue
        doc = build_protocol(row, [a for a in arms if a["protocol_id"].strip() == pid], today)
        try:
            CareProtocol.model_validate(doc)
        except Exception as exc:  # noqa: BLE001 — 초안이라 실패해도 쓰고 보고한다
            failed += 1
            print(f"FAIL  {pid}: {exc}")
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(
            f"wrote {pid} ({len(doc['evidence'])} evidence, "
            f"{len(doc['review']['open_items'])} open items)"
        )
    assert all(o in {g.value for g in GRADED_OUTCOMES} for o in OUTCOME_LABELS.values())
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
