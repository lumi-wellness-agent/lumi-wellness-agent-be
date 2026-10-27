"""측정 도구 — data/instruments/*.json 계약과 채점. 문항 원문 채우기는 measure 담당의 일이다."""

import pytest

from app import instruments
from app.instruments import Instrument, ScoringKind, score
from app.schemas.care import CareGoal
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


def test_stai6_prorated_sum_with_reverse_items():
    stai6 = LOADED[InstrumentId.STAI6]
    assert stai6.scoring.kind is ScoringKind.SUM_PRORATED and stai6.scoring.prorate_items == 20
    assert [i.id for i in stai6.items if i.reverse] == ["calm", "relaxed", "content"]
    calmest = {"calm": 4, "tense": 1, "upset": 1, "relaxed": 4, "content": 4, "worried": 1}
    most_anxious = {"calm": 1, "tense": 4, "upset": 4, "relaxed": 1, "content": 1, "worried": 4}
    assert score(stai6, calmest) == {"total": 20.0}
    assert score(stai6, most_anxious) == {"total": 80.0}


def test_bsri_mean():
    bsri = LOADED[InstrumentId.BSRI]
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
    with pytest.raises(ValueError, match="text"):
        Instrument.model_validate(doc)


def test_todo_instruments_are_named_for_the_measure_owner():
    todo = sorted(i.value for i, ins in LOADED.items() if ins.status == "TODO")
    assert todo == ["INS-BSRI", "INS-STAI6"]  # 문항 원문·한국어판 확인 뒤 READY로 바꾼다
