"""Reference 카드에서 코드가 만드는 문장 — 목표 이름 · 한계 문장 · 목표 척도 안내.
세션 언어마다 하나.

한국어가 원본이고 영어는 같은 뜻을 옮긴 것이다. 영어 목표 이름은 FE 영어 화면의 목표 이름
(src/session/labels.ts)과 같다. 프로토콜이 적은 문장(이름 · 대상 메모 · reference_card_note)은
프로토콜 언어판(app/protocols.localized)을 따른다. 근거 메타데이터 중 카드에 보이는 칸(대상 · 결과의
척도 · 비교 · 통계)은 data/evidence_texts/<lang>.json의 문구 사전으로 옮긴다(evidence_text).
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.schemas.card import CardBadge
from app.schemas.care import CareGoal, Lang
from app.schemas.measure import InstrumentId
from app.schemas.retrieval import Flag


@dataclass(frozen=True)
class CardTexts:
    goal_labels: dict[CareGoal, str]
    general_limit: str  # 카드 끝 한계 문장의 첫 줄
    badge_limits: dict[CardBadge, str]  # 배지마다 고정 한계 문장
    flag_limits: dict[Flag, str]  # 근거마다 검증 플래그의 한계 문장
    # 근거의 1차 지표와 LUMI가 재는 것이 다른 목표(schemas/measure.GOAL_SCALE)
    measure_gaps: dict[CareGoal, str]
    # 목표 척도가 이 언어에서 PROVISIONAL일 때 밝히는 문장 — 척도마다 사정이 달라 따로 둔다.
    # 척도 이름은 measured_by에 보이므로 문장에 넣지 않는다
    provisional_notes: dict[InstrumentId, str]
    default_goal: Callable[[str], str]  # 기본 목표로 시작했을 때 — 목표 이름을 받는다


# 원척도가 검증된 영어판인 척도(STAI-6 · BSRI)의 한국어 번역 초안
TRANSLATED_DRAFT_NOTE = (
    "이 척도는 연구에서 타당성이 확인된 영어판 문항을 LUMI가 한국어로 옮긴 것입니다. 번역본은 아직 "
    "따로 검증하지 않아, 점수 변화는 참고로 봐 주세요."
)

KO = CardTexts(
    goal_labels={
        CareGoal.S1: "반추 완화",
        CareGoal.S2: "긴장·스트레스 완화",
        CareGoal.S3: "주의·피로 회복",
    },
    general_limit="연구 참여자에게서 관찰된 결과이며, 진단이나 치료 효과를 뜻하지 않습니다.",
    badge_limits={
        CardBadge.EXPERIMENTAL: (
            "여러 활동을 묶은 실험적 구성이라, 함께 했을 때의 효과는 검증되지 않았습니다."
        ),
        CardBadge.LIMITED_EVIDENCE: "이 활동을 뒷받침하는 연구가 아직 적습니다.",
        CardBadge.NO_VERIFIED_EVIDENCE: (
            "이번에는 이 활동을 직접 뒷받침하는 검증된 연구를 찾지 못했습니다."
        ),
        CardBadge.CONFLICTING_EVIDENCE: (
            "같은 활동 · 지표에서 효과가 없거나 반대로 나온 연구도 있습니다."
        ),
        CardBadge.ADAPTED_STIMULUS: (
            "연구에서 쓴 자극을 LUMI에 맞게 바꾼 콘텐츠라, 같은 효과가 나는지는 확인되지 "
            "않았습니다."
        ),
        CardBadge.AI_IMAGE_UNVERIFIED: (
            "AI로 만든 이미지는 연구의 자연 사진과 효과가 같은지 검증되지 않았습니다."
        ),
        CardBadge.RELAXED_ORDER: (
            "지금 상황에 맞춘 활동이 없어, 다른 상황을 위해 만든 활동을 골랐습니다."
        ),
        CardBadge.RELAXED_ENVIRONMENT: (
            "지금 있는 곳에 딱 맞는 활동이 없어, 환경이 맞지 않을 수 있는 활동을 골랐습니다."
        ),
    },
    flag_limits={
        Flag.POPULATION_MISMATCH: (
            "임상 진단을 받은 사람들을 대상으로 한 연구라 참고로만 인용합니다."
        ),
        Flag.CONFLICTING_EVIDENCE: (
            "같은 활동 · 지표에서 효과가 없거나 반대로 나온 연구가 있습니다."
        ),
        Flag.ADAPTED_STIMULUS: "LUMI 콘텐츠는 이 연구의 자극과 다릅니다.",
        Flag.NOT_SUPPORTING: "목표 지표의 개선은 보이지 않아 참고로만 인용합니다.",
    },
    measure_gaps={
        CareGoal.S3: (
            "근거 연구는 주의 과제 성적을 봤지만, LUMI는 주의 용량을 재지 않고 지금 느끼는 정신 "
            "피로로 변화를 봅니다."
        ),
    },
    provisional_notes={
        InstrumentId.STAI6: TRANSLATED_DRAFT_NOTE,
        InstrumentId.BSRI: TRANSLATED_DRAFT_NOTE,
        InstrumentId.MFVAS: (
            "이 척도는 지금 느끼는 정신 피로를 보려고 LUMI가 만든 질문이라, 아직 검증되지 "
            "않았습니다. 점수 변화는 참고로 봐 주세요."
        ),
    },
    default_goal=lambda label: f"상황 단서가 부족해 기본 목표({label})로 시작했습니다.",
)

# 영어 STAI-6 · BSRI는 원척도 문항이라 PROVISIONAL이 아니다(data/instruments/en) — MFVAS만 밝힌다
EN = CardTexts(
    goal_labels={
        CareGoal.S1: "Easing repetitive thoughts",
        CareGoal.S2: "Easing stress and tension",
        CareGoal.S3: "Recovering from mental fatigue",
    },
    general_limit=(
        "These results were observed in study participants and don't imply a diagnosis or a "
        "treatment effect."
    ),
    badge_limits={
        CardBadge.EXPERIMENTAL: (
            "This is an experimental combination of activities, so the effect of doing them "
            "together hasn't been tested."
        ),
        CardBadge.LIMITED_EVIDENCE: "There are still only a few studies supporting this activity.",
        CardBadge.NO_VERIFIED_EVIDENCE: (
            "This time we couldn't find a verified study that directly supports this activity."
        ),
        CardBadge.CONFLICTING_EVIDENCE: (
            "Some studies of the same activity and measure found no effect or the opposite effect."
        ),
        CardBadge.ADAPTED_STIMULUS: (
            "This content adapts the stimulus used in the research for LUMI, so it hasn't been "
            "confirmed to have the same effect."
        ),
        CardBadge.AI_IMAGE_UNVERIFIED: (
            "AI-generated images haven't been tested for the same effect as the nature photos "
            "used in the research."
        ),
        CardBadge.RELAXED_ORDER: (
            "No activity was made for your current situation, so we picked one made for a "
            "different situation."
        ),
        CardBadge.RELAXED_ENVIRONMENT: (
            "No activity fit exactly where you are, so we picked one that may not suit your "
            "surroundings."
        ),
    },
    flag_limits={
        Flag.POPULATION_MISMATCH: (
            "This study involved people with a clinical diagnosis, so it is cited for reference "
            "only."
        ),
        Flag.CONFLICTING_EVIDENCE: (
            "Some studies of the same activity and measure found no effect or the opposite effect."
        ),
        Flag.ADAPTED_STIMULUS: "LUMI's content differs from the stimulus used in this study.",
        Flag.NOT_SUPPORTING: (
            "It didn't show an improvement on the goal measure, so it is cited for reference only."
        ),
    },
    measure_gaps={
        CareGoal.S3: (
            "The supporting studies looked at performance on attention tasks, but LUMI doesn't "
            "measure attention capacity; it tracks the mental fatigue you feel right now."
        ),
    },
    provisional_notes={
        InstrumentId.MFVAS: (
            "LUMI wrote these questions to capture the mental fatigue you feel right now, and "
            "they haven't been validated yet. Please treat changes in the score as a rough guide."
        ),
    },
    default_goal=lambda label: (
        f"There weren't enough cues about your situation, so we started with the default goal "
        f"({label})."
    ),
)

TEXTS: dict[Lang, CardTexts] = {"ko": KO, "en": EN}

EVIDENCE_TEXT_DIR = Path(__file__).resolve().parents[2] / "data" / "evidence_texts"


@lru_cache
def evidence_glossary(lang: Lang) -> dict[str, str]:
    """카드에 보이는 근거 메타데이터의 문구 사전 — 한국어 원문 → 그 언어. 한국어는 비어 있다.

    값이 근거마다 반복되므로(성인(영국) · 자연 vs 도시 …) 레코드가 아니라 문구로 옮긴다.
    """
    if lang == "ko":
        return {}
    doc = json.loads((EVIDENCE_TEXT_DIR / f"{lang}.json").read_text(encoding="utf-8"))
    return doc["texts"]


def evidence_text(value: str | None, lang: Lang) -> str | None:
    """근거 메타데이터 한 칸을 세션 언어로. 사전에 없으면 원문 그대로(영어 원문 · 숫자 …)."""
    if value is None:
        return None
    return evidence_glossary(lang).get(value, value)
