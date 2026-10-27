"""app/ingestion/parse.py의 Docling 없는 부분 — 제목 규칙, 항목 → 섹션 블록, 캐시 왕복."""

from app.ingestion import parse
from app.ingestion.parse import ParsedPaper, RawItem, Section

M, R, D, OTHER, A, INTRO = (
    Section.METHODS,
    Section.RESULTS,
    Section.DISCUSSION,
    Section.OTHER,
    Section.ABSTRACT,
    Section.INTRODUCTION,
)


def test_heading_key_strips_symbols_numbering_and_letter_spacing():
    assert parse.heading_key("■ MATERIALS AND METHODS") == "materials and methods"
    assert parse.heading_key("3 | RESULTS") == "results"
    assert parse.heading_key("III. Method:") == "method"
    assert parse.heading_key("2.1. Participants") == "participants"
    assert parse.heading_key("A B S T R A C T") == "abstract"
    assert parse.heading_key("INTRODUCTION") == "introduction"  # I는 로마 숫자가 아니다


def test_major_headings_switch_sections():
    assert parse.next_section("INTRODUCTION", OTHER) is INTRO
    assert parse.next_section("2. Method", INTRO) is M
    assert parse.next_section("STAR+METHODS", D) is M
    assert parse.next_section("RESULTS", M) is R
    assert parse.next_section("Results and Discussion", M) is R
    assert parse.next_section("Results of the Interventions", M) is R
    assert parse.next_section("SUMMARY", OTHER) is A
    assert parse.next_section("Summary and conclusions", R) is D
    assert parse.next_section("Limitations of the study", D) is D
    for back in ("REFERENCES", "Acknowledgments", "Author credit statement", "Keywords"):
        assert parse.next_section(back, D) is OTHER, back


def test_minor_headings_keep_the_current_section():
    for minor in (
        "Subject participation",
        "Outcome measures",
        "Cell Reports",
        "Figure 1. Study design",
        "Ethics Statement",  # Methods 안에 온다 — 본문을 OTHER로 보내지 않는다
        "6.3. Summary of mediation results",  # ABSTRACT가 아니다
    ):
        assert parse.next_section(minor, M) is M, minor
    # 방법 소제목은 결과 안에서는 섹션을 바꾸지 않는다(메타분석의 Study selection)
    assert parse.next_section("Study selection", R) is R
    assert parse.next_section("Participants", D) is M  # 다음 실험의 방법
    assert parse.next_section("2.1 Participants", INTRO) is M


def test_group_blocks_follows_reading_order():
    items = [
        RawItem("title", "Brief structured respiration practices"),
        RawItem("text", "Highlights text", page=1),
        RawItem("section_header", "SUMMARY"),
        RawItem("text", "We compared three breathing exercises.", page=1),
        RawItem("section_header", "RESULTS"),
        RawItem("section_header", "Subject participation"),
        RawItem("text", "Of 140 potential participants, 114 were invited.", page=3),
        RawItem("table", "| group | n |\n|---|---|\n| sighing | 30 |", page=4),
        RawItem("picture", "ignored"),
        RawItem("section_header", "STAR+METHODS"),
        RawItem("list_item", "Participants were recruited online.", page=12),
        RawItem("section_header", "REFERENCES"),
        RawItem("text", "1. Some reference.", page=14),
    ]
    paper = ParsedPaper("P16", "P16.pdf", 15, items)
    blocks = paper.blocks
    assert [b.section for b in blocks] == [OTHER, A, R, Section.TABLE, M, OTHER]
    assert blocks[2].heading == "Subject participation" and blocks[2].page == 3
    assert blocks[3].label == "table" and blocks[3].heading == "Subject participation"
    assert "114 were invited" in paper.section_text(R)
    assert "Some reference" not in paper.body_text()  # 참고문헌은 본문에서 뺀다
    assert paper.chars_by_section()["TABLE"] == len(blocks[3].text)


def test_cache_round_trip_and_markdown(tmp_path):
    items = [
        RawItem("section_header", "Participants"),
        RawItem("text", "Eighty-four undergraduates …", 2),
    ]
    paper = ParsedPaper("E01", "E01_2005_Curry.pdf", 5, items)
    parse.save_cache(tmp_path, paper)
    loaded = parse.load_cached(tmp_path, "E01")
    assert loaded == paper and loaded.blocks[0].section is M
    assert parse.load_cached(tmp_path, "E02") is None
    (tmp_path / "OLD.json").write_text('{"paper_id": "OLD", "blocks": []}', encoding="utf-8")
    assert parse.load_cached(tmp_path, "OLD") is None  # 옛 형식은 다시 파싱한다
    markdown = (tmp_path / "E01.md").read_text(encoding="utf-8")
    assert "## [METHODS] Participants" in markdown and "<!-- text p.2 -->" in markdown


def test_ocr_engine_prefers_apple_vision_on_macos(monkeypatch):
    monkeypatch.setattr(parse.sys, "platform", "darwin")
    monkeypatch.setattr(parse.importlib.util, "find_spec", lambda name: object())
    assert parse.ocr_engine() == "mac" and parse.tool_label(ocr=True) == "docling+ocr-mac"
    monkeypatch.setattr(parse.importlib.util, "find_spec", lambda name: None)
    assert parse.ocr_engine() == "rapid"
    monkeypatch.setattr(parse.sys, "platform", "linux")
    assert parse.ocr_engine("mac") == "mac" and parse.tool_label(ocr=False) == "docling"
