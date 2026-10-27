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
    assert parse.next_section("Physiological results", M) is R
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


def box(text, x0, y0, x1, y1):
    return parse.OcrBox(text, x0, y0, x1, y1)


def test_reading_order_two_columns_with_spanning_rows():
    boxes = [
        box("Figure 1. A caption across both columns", 0.10, 0.50, 0.90, 0.52),
        box("right column line one", 0.55, 0.10, 0.95, 0.12),
        box("left column line one", 0.08, 0.10, 0.45, 0.12),
        box("left column line two", 0.08, 0.13, 0.45, 0.15),
        box("right column line two", 0.55, 0.13, 0.95, 0.15),
        box("left below caption", 0.08, 0.60, 0.45, 0.62),
        box("right below caption", 0.55, 0.60, 0.95, 0.62),
    ]
    texts = [" ".join(b.text for b in row) for row in parse.reading_order(boxes)]
    assert texts == [
        "left column line one",
        "left column line two",
        "right column line one",
        "right column line two",
        "Figure 1. A caption across both columns",
        "left below caption",
        "right below caption",
    ]


def test_reading_order_single_column_is_top_to_bottom():
    boxes = [
        box("second line of the page text", 0.10, 0.20, 0.90, 0.22),
        box("first line of the page text", 0.10, 0.10, 0.90, 0.12),
        box("settings.", 0.10, 0.30, 0.20, 0.32),  # 짧은 마지막 줄도 한 단으로 읽는다
    ]
    texts = [" ".join(b.text for b in row) for row in parse.reading_order(boxes)]
    assert texts == ["first line of the page text", "second line of the page text", "settings."]


def test_is_heading_matches_whole_known_titles_only():
    titles = ("Results and Discussion", "EXPERIMENT 2", "Method", "Subjects", "GENERAL DISCUSSION")
    for title in titles:
        assert parse.is_heading(title), title
    body_lines = (
        "Results showed that the nature group had",
        "Twelve (8 females, 4 males;",
        "stressor.",
    )
    for line in (*body_lines, ""):
        assert not parse.is_heading(line), line


def test_layout_items_builds_headings_and_paragraphs():
    boxes = [
        box("Method", 0.08, 0.10, 0.20, 0.12),
        box("Thirty-eight University of Michigan students took a", 0.08, 0.13, 0.45, 0.15),
        box("walk. Backwards digit-", 0.08, 0.155, 0.45, 0.175),
        box("span was measured.", 0.08, 0.18, 0.45, 0.20),
        box("Results and Discussion", 0.55, 0.10, 0.80, 0.12),
        box("Performance improved after the nature walk.", 0.55, 0.13, 0.95, 0.15),
    ]
    items = parse.layout_items(boxes, page=2)
    assert items == [
        parse.RawItem("section_header", "Method", 2),
        parse.RawItem(
            "text",
            "Thirty-eight University of Michigan students took a walk. Backwards digit-span "
            "was measured.",
            2,
        ),
        parse.RawItem("section_header", "Results and Discussion", 2),
        parse.RawItem("text", "Performance improved after the nature walk.", 2),
    ]
    paper = ParsedPaper("P05", "P05.pdf", 6, items, parse.OCR_TOOL)
    assert "digit-span" in paper.section_text(R) or "digit-span" in paper.section_text(M)
    assert paper.blocks[0].section is M and paper.blocks[1].section is R


class FakeImage:
    """PIL 없이(CI는 .[dev]만 설치) ocr_boxes가 쓰는 size · convert만 흉내 낸다."""

    def __init__(self, width, height):
        self.size = (width, height)

    def convert(self, mode):
        return [[[0, 0, 0]] * self.size[0]] * self.size[1]


def test_ocr_boxes_normalizes_coordinates_and_drops_margins():
    class Result:
        boxes = [
            [[10, 2], [190, 2], [190, 8], [10, 8]],  # 위 5% 안 — 머리글
            [[10, 100], [190, 100], [190, 110], [10, 110]],
        ]
        txts = ["Cognitive Benefits of Nature Interaction", "Body text line"]

    boxes = parse.ocr_boxes(FakeImage(200, 400), lambda array: Result())
    assert boxes == [parse.OcrBox("Body text line", 0.05, 0.25, 0.95, 0.275)]

    class Empty:
        boxes = txts = None

    assert parse.ocr_boxes(FakeImage(10, 10), lambda array: Empty()) == []
