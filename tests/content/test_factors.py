"""콘텐츠 Factor 카탈로그(data/content_factors.json) — Enum과 키가 맞는지, 근거가 실재하는지,
검색어가 공급자 필터를 벗어나지 않는지, 활동마다 열리는 칸이 맞는지."""

import csv
from pathlib import Path

import pytest

from app import protocols
from app.content import factors
from app.content.providers import EXCLUDED_IMAGE_TAGS
from app.schemas.content import ArtSubject, ContentFactors, MusicTrack, Scenery, Soundscape

ROOT = Path(__file__).resolve().parents[2]
PAPERS = {row["rc_id"] for row in csv.DictReader((ROOT / "data" / "manifest.csv").open())}
ENUMS = {
    factors.SCENERY: Scenery,
    factors.SOUNDSCAPE: Soundscape,
    factors.ART_SUBJECT: ArtSubject,
    factors.MUSIC: MusicTrack,
}


@pytest.mark.parametrize("axis", list(ENUMS))
def test_llm_enum_is_exactly_the_values_with_study_evidence(axis):
    # LLM이 고를 수 있는 값 = 논문 자극으로 쓰인 값(STIMULUS). 선호용(DESIGN)은 Enum에 없다
    assert {e.value for e in ENUMS[axis]} == set(factors.selectable(axis))


def test_every_stimulus_value_cites_papers_in_the_manifest():
    for axis in factors.load_catalog().axes.values():
        for key, value in axis.values.items():
            if value.basis is factors.Basis.STIMULUS:
                assert value.evidence, key
            assert {e.paper_id for e in value.evidence} <= PAPERS, key


def test_every_value_has_reviewed_queries():
    catalog = factors.load_catalog()
    for key, value in catalog.axes[factors.SCENERY].values.items():
        assert value.image_queries and value.ai_prompt, key
        assert set(value.matching_soundscapes) <= set(Soundscape), key
    for axis in (factors.SOUNDSCAPE, factors.ART_SUBJECT):
        for key, value in catalog.axes[axis].values.items():
            assert value.queries, key


def test_image_queries_stay_inside_the_photo_filter():
    # 검색어 자체가 Pixabay 태그 필터(사람 · 건물 · 도로 …)에 걸리는 낱말을 쓰지 않는다
    for key, value in factors.load_catalog().axes[factors.SCENERY].values.items():
        for query in value.image_queries + value.video_queries:
            assert not set(query.split()) & set(EXCLUDED_IMAGE_TAGS), (key, query)


def test_video_scenery_is_limited_to_filmed_settings():
    # 영상 근거는 숲 · 시냇물(P25 Nature Forest/Stream, N17 강 영상)뿐이다
    video = [
        k for k, v in factors.load_catalog().axes[factors.SCENERY].values.items() if v.video_queries
    ]
    assert video == ["forest", "lake river"]


@pytest.mark.parametrize(
    ("protocol_id", "axes", "capacity"),
    [
        ("PR-01", ["scenery"], 4),  # 사진 25장
        ("PR-02", ["scenery", "soundscape"], 1),  # 영상 + 자연음
        ("PR-03", ["scenery"], 1),  # 사진 1장
        ("PR-04", ["soundscape"], 0),
        ("PR-05", ["music"], 0),  # 검수 음원 3곡 중 사용자 선택 — 추천 칸만
        ("PR-20", ["art_subject"], 0),
        ("PR-21", ["scenery"], 1),
        ("PR-06", [], 0),  # 호흡
    ],
)
def test_slots_open_per_activity(protocol_id, axes, capacity):
    protocol = protocols.get_protocol(protocol_id)
    assert factors.axes_for(protocol) == axes
    assert factors.scenery_capacity(protocol) == capacity


def test_pick_query_uses_the_lead_query_without_rng():
    assert factors.pick_query(["a", "b", "c"], None) == "a"


def test_sanitize_matches_video_sound_to_the_video_scene():
    pr02 = protocols.get_protocol("PR-02")
    out = factors.sanitize(
        pr02,
        ContentFactors(
            scenery=[Scenery.SEA, Scenery.LAKE_RIVER],  # 바다는 영상 근거가 없다
            soundscape=Soundscape.BIRDSONG,  # 시냇물 영상에 새소리 → 시냇물 소리로 맞춘다
            art_subject=None,
            music=None,
            reason="-",
        ),
    )
    assert out.scenery == [Scenery.LAKE_RIVER] and out.soundscape is Soundscape.STREAM


def test_sanitize_drops_video_sound_without_a_video_scene():
    pr02 = protocols.get_protocol("PR-02")
    out = factors.sanitize(
        pr02,
        ContentFactors(
            scenery=[], soundscape=Soundscape.RAIN, art_subject=None, music=None, reason="-"
        ),
    )
    assert out.is_empty()  # 기본 숲 영상 · 숲 소리를 그대로 쓴다


def test_sanitize_keeps_any_soundscape_for_sound_only_activity():
    pr04 = protocols.get_protocol("PR-04")
    out = factors.sanitize(
        pr04,
        ContentFactors(
            scenery=[Scenery.SEA],
            soundscape=Soundscape.RAIN,
            art_subject=None,
            music=None,
            reason="-",
        ),
    )
    assert out.scenery == [] and out.soundscape is Soundscape.RAIN


def test_menu_tells_the_llm_how_video_and_sound_pair():
    menu = factors.menu(protocols.get_protocol("PR-02"))
    sound = next(item for item in menu if item["field"] == "soundscape")
    assert "forest → birdsong, forest_wind" in sound["note"]
    assert "lake river → stream" in sound["note"]


def test_every_music_value_points_at_a_registered_pr05_track():
    from app.content.curated import curated_asset

    refs = factors.music_refs(protocols.get_protocol("PR-05"))
    for key, value in factors.load_catalog().axes[factors.MUSIC].values.items():
        assert value.asset_ref in refs and curated_asset(value.asset_ref), key
        assert {"form", "vocals", "sacred", "character"} <= set(value.labels), key


def test_music_menu_shows_labels_and_keeps_the_choice_with_the_user():
    menu = factors.menu(protocols.get_protocol("PR-05"))
    assert [item["field"] for item in menu] == ["music"]
    music = menu[0]
    assert [v["value"] for v in music["values"]] == ["miserere", "mozart_k448", "vivaldi_spring"]
    assert music["values"][0]["labels"]["vocals"] == "라틴어 가사"
    assert "사용자가 목록에서 직접 고르고" in music["note"]


def test_sanitize_drops_music_for_activities_without_a_music_choice():
    out = factors.sanitize(
        protocols.get_protocol("PR-21"),  # 고정 명상 음성뿐 — 음악 칸이 없다
        ContentFactors(
            scenery=[], soundscape=None, art_subject=None, music=MusicTrack.MISERERE, reason="-"
        ),
    )
    assert out.is_empty()


def test_scenery_menu_asks_for_four_kinds_when_there_is_no_cue():
    scenery = factors.menu(protocols.get_protocol("PR-01"))[0]
    assert scenery["max"] == 4
    assert "단서가 없으면 서로 다른 값 4개" in scenery["note"]
    # 사진 1장(PR-03)은 섞지 않는다
    assert "note" not in factors.menu(protocols.get_protocol("PR-03"))[0]
