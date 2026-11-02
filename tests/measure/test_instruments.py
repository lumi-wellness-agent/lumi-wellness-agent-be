"""측정 도구 — instrument 계약·번역 payload·채점 · 세션 언어별 제시 문구."""

import re

import pytest

from app import instruments
from app.instruments import Instrument, ScoringKind, score
from app.schemas.care import LANGS, CareGoal
from app.schemas.measure import GOAL_SCALE, InstrumentId, Timepoint, final_scale, round_scale

LOADED = instruments.load_instruments()


def test_every_instrument_loads_and_matches_its_id():
    assert set(LOADED) == set(InstrumentId)
    for instrument_id, instrument in LOADED.items():
        assert instrument.instrument_id is instrument_id


def test_goal_scale_bundle():
    assert GOAL_SCALE == {
        CareGoal.S1: InstrumentId.BSRI,
        CareGoal.S2: InstrumentId.STAI6,
        CareGoal.S3: InstrumentId.MFVAS,
    }
    # STAI-6 · BSRI는 사전·종료에만, MFVAS는 라운드마다 — 종료에 다시 묻지 않는다
    assert [round_scale(g) for g in CareGoal] == [None, None, InstrumentId.MFVAS]
    assert [final_scale(g) for g in CareGoal] == [InstrumentId.BSRI, InstrumentId.STAI6, None]


def test_sam_is_ready_and_scores_per_item():
    sam = LOADED[InstrumentId.SAM]
    assert sam.status == "READY" and [i.id for i in sam.items] == ["valence", "arousal"]
    assert score(sam, {"valence": 3, "arousal": 7}) == {"valence": 3.0, "arousal": 7.0}
    payload = instruments.present(sam)
    assert payload["items"][0]["low_label"] == "불쾌" and payload["scale"]["max"] == 9


def test_stai6_translation_and_prorated_sum_with_reverse_items():
    stai6 = LOADED[InstrumentId.STAI6]
    assert stai6.status == "PROVISIONAL"
    assert "한국어 번역본 · 내부 시험용" in stai6.name
    assert all(item.text for item in stai6.items)
    assert stai6.scoring.kind is ScoringKind.SUM_PRORATED and stai6.scoring.prorate_items == 20
    assert [i.id for i in stai6.items if i.reverse] == ["calm", "relaxed", "content"]
    calmest = {"calm": 4, "tense": 1, "upset": 1, "relaxed": 4, "content": 4, "worried": 1}
    most_anxious = {"calm": 1, "tense": 4, "upset": 4, "relaxed": 1, "content": 1, "worried": 4}
    assert score(stai6, calmest) == {"total": 20.0}
    assert score(stai6, most_anxious) == {"total": 80.0}


def test_bsri_translation_and_mean():
    bsri = LOADED[InstrumentId.BSRI]
    assert bsri.status == "PROVISIONAL"
    assert "한국어 번역본 · 내부 시험용" in bsri.name
    assert all(item.text for item in bsri.items)
    answers = {f"item{i}": 10 * i for i in range(1, 9)}
    assert score(bsri, answers) == {"mean": 45.0}


def test_mfvas_is_provisional_and_scores_each_item():
    """S3 임시 도구 — 팀이 쓴 문항이라 바로 제시하되 미검증.

    판정은 fatigue 한 문항, 나머지 둘은 데이터 수집용.
    """
    mfvas = LOADED[InstrumentId.MFVAS]
    assert mfvas.status == "PROVISIONAL" and mfvas.scoring.kind is ScoringKind.PER_ITEM
    assert [i.id for i in mfvas.items] == ["fatigue", "concentration", "energy"]
    assert all(i.text for i in mfvas.items)
    assert score(mfvas, {"fatigue": 80, "concentration": 70, "energy": 20}) == {
        "fatigue": 80.0,
        "concentration": 70.0,
        "energy": 20.0,
    }
    payload = instruments.present(mfvas)
    assert payload["scale"] == {"min": 0, "max": 100, "step": 1, "kind": "SLIDER"}
    assert payload["items"][0]["high_label"] == "매우 피로하다"


def test_missing_or_out_of_range_answers_are_rejected():
    sam = LOADED[InstrumentId.SAM]
    with pytest.raises(ValueError, match="arousal 응답이 없다"):
        score(sam, {"valence": 3})
    with pytest.raises(ValueError, match="밖이다"):
        score(sam, {"valence": 0, "arousal": 7})


def test_measure_builds_measurement_and_drops_unknown_keys():
    sam = LOADED[InstrumentId.SAM]
    m = instruments.measure(sam, {"valence": 3, "arousal": 7, "extra": 1}, Timepoint.POST, 2)
    assert m.answers == {"valence": 3.0, "arousal": 7.0} and m.round == 2
    assert m.timepoint is Timepoint.POST and m.scores == {"valence": 3.0, "arousal": 7.0}


@pytest.mark.parametrize("status", ["READY", "PROVISIONAL"])
def test_presentable_instrument_needs_item_text(status):
    doc = LOADED[InstrumentId.STAI6].model_dump()
    doc["status"] = status
    doc["items"][0]["text"] = None
    with pytest.raises(ValueError, match="text"):
        Instrument.model_validate(doc)


def test_stai6_and_bsri_are_available_as_internal_translation_drafts():
    for instrument_id in (InstrumentId.STAI6, InstrumentId.BSRI):
        instrument = LOADED[instrument_id]
        assert instrument.status == "PROVISIONAL"
        assert all(item.text for item in instrument.items)
        assert "한국어 번역본 · 내부 시험용" in instrument.name


HANGUL = re.compile(r"[가-힣]")


def test_every_instrument_has_text_in_every_other_language():
    expected = {(lang, i) for lang in LANGS if lang != "ko" for i in InstrumentId}
    assert set(instruments.load_texts()) == expected


def test_korean_presentation_is_the_source_unchanged():
    for instrument in LOADED.values():
        assert instruments.localized(instrument, "ko") is instrument
        assert instruments.present(instrument) == instruments.present(instrument, "ko")


@pytest.mark.parametrize("instrument_id", list(InstrumentId))
def test_english_presentation_changes_only_the_wording(instrument_id):
    source = LOADED[instrument_id]
    ko = instruments.present(source, "ko")
    en = instruments.present(source, "en")
    assert en["instrument_id"] == ko["instrument_id"] and en["scale"] == ko["scale"]
    assert [i["id"] for i in en["items"]] == [i["id"] for i in ko["items"]]
    shown = [en["name"], en["instruction"]] + [
        v for i in en["items"] for v in (i["text"], i["low_label"], i["high_label"])
    ]
    assert not any(HANGUL.search(v) for v in shown if v)
    # 역채점 · 채점은 원천 그대로
    english = instruments.localized(source, "en")
    assert [i.reverse for i in english.items] == [i.reverse for i in source.items]
    assert english.scoring == source.scoring


def test_english_stai6_and_bsri_are_the_original_items_and_mfvas_stays_provisional():
    def status(instrument_id):
        return instruments.localized(LOADED[instrument_id], "en").status

    # 원척도가 영어로 타당화됐다 — 한국어 번역 초안과 달리 검증 전이 아니다
    assert status(InstrumentId.STAI6) == "READY" and status(InstrumentId.BSRI) == "READY"
    assert status(InstrumentId.SAM) == "READY"
    assert status(InstrumentId.MFVAS) == "PROVISIONAL"  # 팀이 쓴 문항은 언어와 상관없이 미검증
    bsri = instruments.localized(LOADED[InstrumentId.BSRI], "en")
    # 저자 문서 부록의 문장 그대로(따옴표 · 문장부호 포함)
    assert bsri.items[0].text == "Right now, I am reflecting about my mood."
    assert bsri.items[3].text == (
        "Right now, I am thinking: “why do I have problems other people don't have?”."
    )
    stai6 = instruments.localized(LOADED[InstrumentId.STAI6], "en")
    assert [i.text for i in stai6.items] == [
        "I feel calm.",
        "I am tense.",
        "I feel upset.",
        "I am relaxed.",
        "I feel content.",
        "I am worried.",
    ]


def test_a_translation_must_cover_every_item_and_label():
    source = LOADED[InstrumentId.SAM]
    text = instruments.load_texts()[("en", InstrumentId.SAM)]
    missing = text.model_copy(update={"items": {"valence": text.items["valence"]}})
    with pytest.raises(ValueError, match="문항 id"):
        instruments._check_covers(source, missing, "en/INS-SAM.json")
    unlabeled = text.model_copy(
        update={
            "items": {
                **text.items,
                "arousal": text.items["arousal"].model_copy(update={"low_label": None}),
            }
        }
    )
    with pytest.raises(ValueError, match="arousal.low_label"):
        instruments._check_covers(source, unlabeled, "en/INS-SAM.json")
