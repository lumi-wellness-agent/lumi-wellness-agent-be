"""원문 파싱 — 논문을 섹션 단위 텍스트로 바꾼다. PDF는 Docling, Europe PMC 전문은 JATS XML로 읽는다.

- 캐시는 Docling이 읽기 순서대로 낸 항목(`RawItem` — 제목 · 본문 · 목록 · 표 · 캡션)이다. 섹션은
  읽을 때 `group_blocks()`가 붙인다 — 규칙을 고쳐도 Docling을 다시 돌리지 않는다.
- 섹션은 제목을 만날 때 `next_section()`이 바꾼다. 큰 제목(Method · Results · Discussion …)만
  섹션을 바꾸고, 소제목(결과 문장형 제목 · Measures · 저널 머리글)은 직전 섹션을 잇는다.
  Participants · Study selection 같은 방법 소제목은 결과 섹션 안에서는 섹션을 바꾸지 않는다
  (메타분석은 Results 아래에 Study selection이 온다). 제목 앞 기호 · 번호('■', '3 |', 'III.')와
  글자 사이 공백('A B S T R A C T')은 떼고 본다. 참고문헌 · 감사의 글 · 저자 기여는 OTHER다.
- 표는 Markdown으로 바꿔 `Section.TABLE` 블록으로 둔다. 어느 제목 아래였는지는 `heading`에 둔다.
- 전문은 저작권이 있어 공개 BE 저장소가 아니라 Research_paper(비공개) 저장소의
  `LUMI_R_C_Research_2026-10-22/01_fulltext/`에 `<paper_id>.json`(항목) · `.md`(사람이 읽는 섹션
  보기)로 둔다. 형제 폴더를 `research_root()`가 찾는다(워크트리에서도 상위로 올라가며 찾는다).
- Docling · torch는 무거워 `parse_pdf()` 안에서만 읽는다. 섹션 규칙은 순수 함수라 테스트한다.
- 스캔 PDF는 Docling OCR을 거치지 않는다. 쪽에 든 스캔 원본 이미지를 해상도 그대로 꺼내
  RapidOCR(PP-OCR, onnxruntime)에 넣고, 단 나누기 · 줄 · 문단 · 제목을 `layout_items()`가 만든다.
  2026-10-27 P05 · P26 정답 문단 비교에서 숫자 토큰 정확도가 RapidOCR 직접 0.96, Apple Vision
  0.82, Tesseract 0.76, EasyOCR 0.56이었고, 같은 엔진도 Docling을 거치면(쪽을 3배로 키워 영역별
  OCR) 0.78 · 0.64로 떨어졌다. 그래도 OCR 텍스트의 발췌는 쪽 이미지와 대조한 뒤에 쓴다
  (extract_excerpts --include-ocr, 대조를 마친 항목은 `_verified`).

JATS XML은 루트가 `<article>`이고 `body/sec`가 섹션이다. 섹션마다 `title`이 있다
(예: 1. Introduction, 2. Method, 3. Results, 4. Discussion). 실험 조건과 결과는
Method·Results와 표·그림 캡션에 흩어져 있어 함께 뽑는다. (JATS 경로는 아직 구현하지 않았다.)
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from functools import cached_property
from pathlib import Path

from app.schemas.evidence import Section

ROOT = Path(__file__).resolve().parents[2]
RESEARCH_DIR = Path("LUMI_R_C_Research_2026-10-22")
FULLTEXT_DIR = RESEARCH_DIR / "01_fulltext"

_BACK_MATTER = re.compile(
    r"^(references?|bibliography|acknowledg|funding|author|credit author|declarations?|"
    r"conflicts? of interest|competing interests?|supplementa|inclusion and diversity|"
    r"appendix|key resources table|resource availability|lead contact|materials availability|"
    r"data (and code )?availability|additional information|orcid|highlights|"
    r"graphical abstract|correspondence|in brief|article ?info|keywords?|citation|copyright|"
    r"open access|publisher|received|please cite|glossary|참고문헌|감사의 글)"
)
_RESULTS = re.compile(r"^(results?|findings)(\b|$)|^\w+ results$")  # 'Physiological results'
_DISCUSSION = re.compile(
    r"^(general )?(discussion|conclusions?|limitations?|implications?|summary and conclusions?)"
)
_METHODS = re.compile(
    r"^(methods?|methodology|materials?( and methods?)?|method details|star\W*methods|"
    r"experimental (procedures?|design|methods?|model and subject details))$"
)
# 방법 소제목 — 결과 섹션 안에서는 섹션을 바꾸지 않는다
_METHODS_MINOR = re.compile(
    r"^(participants?( and (procedures?|design|materials?|methods?))?|procedures?|"
    r"search strategy|eligibility criteria|inclusion criteria|study selection|data extraction)$"
)
_ABSTRACT = re.compile(r"^(abstract|summary|synopsis)$")
_INTRODUCTION = re.compile(r"^(introduction|background|theoretical background|literature review)")
_SYMBOLS = re.compile(r"^[^\w]+")  # '■ ' · '| ' · '• '
# '2.1 ', '3) ', 'III. ' — 번호 뒤에 공백이 있을 때만 뗀다('INTRODUCTION'의 I는 로마 숫자가 아니다)
_NUMBERING = re.compile(r"^(?:(?:\d+(?:\.\d+)*|[ivx]+|[IVX]+)[.)]?\s+)+")
_SPACED = re.compile(r"(?:\w ){2,}\w")  # 'a b s t r a c t'
TEXT_LABELS = frozenset(
    {"text", "list_item", "paragraph", "caption", "footnote", "formula", "code"}
)
HEADING_LABELS = frozenset({"section_header", "title"})


def heading_key(heading: str) -> str:
    """비교용 제목 — 앞 기호 · 번호를 떼고, 소문자, 끝 구두점 제거, 글자 사이 공백을 붙인다."""
    text = heading.strip()
    for _ in range(3):
        text = _NUMBERING.sub("", _SYMBOLS.sub("", text))
    text = re.sub(r"[:\-–—.]+$", "", text.lower()).strip()
    if _SPACED.fullmatch(text):
        text = text.replace(" ", "")
    return text


def next_section(heading: str, current: Section) -> Section:
    """제목 한 줄을 만난 뒤의 섹션. 큰 제목만 바꾸고 소제목은 current를 잇는다."""
    key = heading_key(heading)
    if not key:
        return current
    if _BACK_MATTER.search(key):
        return Section.OTHER
    if _RESULTS.search(key):
        return Section.RESULTS
    if _DISCUSSION.search(key):
        return Section.DISCUSSION
    if _METHODS.search(key):
        return Section.METHODS
    if _METHODS_MINOR.search(key):
        return current if current is Section.RESULTS else Section.METHODS
    if _ABSTRACT.search(key):
        return Section.ABSTRACT
    if _INTRODUCTION.search(key):
        return Section.INTRODUCTION
    return current


@dataclass(frozen=True)
class RawItem:
    """Docling 항목을 섹션 규칙이 보는 최소 모양으로 줄인 것. 캐시에 이대로 남는다."""

    label: str
    text: str
    page: int | None = None


@dataclass
class Block:
    section: Section
    heading: str
    text: str
    page: int | None = None
    label: str = "text"


def group_blocks(items: Iterable[RawItem]) -> list[Block]:
    """읽기 순서의 항목 → 섹션이 붙은 블록. 첫 제목 전 본문은 OTHER(저자 · 하이라이트 등)."""
    blocks: list[Block] = []
    section = Section.OTHER
    heading = ""
    for item in items:
        text = (item.text or "").strip()
        if item.label in HEADING_LABELS:
            if text:
                section = next_section(text, section)
                heading = text
            continue
        if not text:
            continue
        if item.label == "table":
            blocks.append(Block(Section.TABLE, heading, text, item.page, "table"))
        elif item.label in TEXT_LABELS:
            blocks.append(Block(section, heading, text, item.page, item.label))
    return blocks


@dataclass
class ParsedPaper:
    paper_id: str
    source: str
    pages: int
    items: list[RawItem] = field(default_factory=list)
    tool: str = "docling"

    @cached_property
    def blocks(self) -> list[Block]:
        return group_blocks(self.items)

    def section_text(self, *sections: Section) -> str:
        chosen = set(sections)
        return "\n\n".join(b.text for b in self.blocks if b.section in chosen)

    def body_text(self) -> str:
        """참고문헌 등 OTHER를 뺀 본문 전체 — 발췌 검증(verbatim)에 쓴다."""
        return "\n\n".join(b.text for b in self.blocks if b.section is not Section.OTHER)

    def chars_by_section(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for block in self.blocks:
            out[block.section.value] = out.get(block.section.value, 0) + len(block.text)
        return out

    def to_json(self) -> str:
        payload = {
            "paper_id": self.paper_id,
            "source": self.source,
            "pages": self.pages,
            "tool": self.tool,
            "items": [asdict(item) for item in self.items],
        }
        return json.dumps(payload, ensure_ascii=False, indent=1)

    @classmethod
    def from_json(cls, text: str) -> ParsedPaper:
        payload = json.loads(text)
        items = [RawItem(**item) for item in payload.pop("items")]
        return cls(items=items, **payload)

    def to_markdown(self) -> str:
        lines = [f"# {self.paper_id} — {Path(self.source).name} ({self.pages}p, {self.tool})", ""]
        heading = None
        for block in self.blocks:
            if block.heading != heading:
                heading = block.heading
                lines.extend([f"## [{block.section.value}] {heading}", ""])
            page = f" p.{block.page}" if block.page else ""
            lines.extend([block.text, f"<!-- {block.label}{page} -->", ""])
        return "\n".join(lines)


def research_root(start: Path = ROOT) -> Path:
    """형제 폴더 Research_paper(비공개 저장소). 워크트리에서는 상위로 올라가며 찾는다."""
    for parent in [start, *start.parents]:
        candidate = parent.parent / "Research_paper"
        if (candidate / RESEARCH_DIR).is_dir():
            return candidate
    raise FileNotFoundError("Research_paper 폴더를 찾지 못했다 — 나란히 두거나 --research-root")


def cache_paths(cache_dir: Path, paper_id: str) -> tuple[Path, Path]:
    return cache_dir / f"{paper_id}.json", cache_dir / f"{paper_id}.md"


def load_cached(cache_dir: Path, paper_id: str) -> ParsedPaper | None:
    """캐시 JSON → ParsedPaper. 옛 형식(블록만 있고 항목이 없음)이면 None — 다시 파싱한다."""
    json_path, _ = cache_paths(cache_dir, paper_id)
    if not json_path.exists():
        return None
    payload = json_path.read_text(encoding="utf-8")
    if '"items"' not in payload:
        return None
    return ParsedPaper.from_json(payload)


def save_cache(cache_dir: Path, paper: ParsedPaper) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    json_path, md_path = cache_paths(cache_dir, paper.paper_id)
    json_path.write_text(paper.to_json(), encoding="utf-8")
    md_path.write_text(paper.to_markdown(), encoding="utf-8")


def _docling_items(document) -> Iterator[RawItem]:
    """Docling 문서 → RawItem. 표는 캡션 + Markdown으로 바꾼다."""
    for item, _level in document.iterate_items():
        label = item.label.value
        page = item.prov[0].page_no if getattr(item, "prov", None) else None
        if label == "table":
            try:
                text = item.export_to_markdown(document)
            except TypeError:  # 옛 docling-core 시그니처
                text = item.export_to_markdown()
            try:
                caption = item.caption_text(document)
            except Exception:  # noqa: BLE001 — 캡션은 있으면 좋고 없어도 된다
                caption = ""
            yield RawItem("table", f"{caption}\n{text}".strip() if caption else text, page)
        elif label in HEADING_LABELS or label in TEXT_LABELS:
            yield RawItem(label, getattr(item, "text", "") or "", page)


def make_converter():
    """Docling 변환기(텍스트층 PDF). OCR은 끈다 — 스캔 PDF는 ocr_pdf()가 맡는다. 표 구조는 켠다."""
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    options = PdfPipelineOptions()
    options.do_ocr = False
    options.do_table_structure = True
    pdf_option = PdfFormatOption(pipeline_options=options)
    return DocumentConverter(format_options={InputFormat.PDF: pdf_option})


def parse_pdf(paper_id: str, pdf_path: Path, *, converter=None) -> ParsedPaper:
    """텍스트층 PDF 한 편 → ParsedPaper. converter를 넘기면 모델을 다시 읽지 않는다."""
    converter = converter or make_converter()
    document = converter.convert(str(pdf_path)).document
    return ParsedPaper(
        paper_id=paper_id,
        source=str(pdf_path.name),
        pages=len(document.pages),
        items=list(_docling_items(document)),
        tool="docling",
    )


# ── 스캔 PDF OCR ──────────────────────────────────────────────────────────────────────────────

OCR_TOOL = "rapidocr"  # 캐시의 tool 값. extract_excerpts는 'ocr'이 들어 있으면 OCR 텍스트로 본다
RENDER_DPI = 150  # 쪽에 스캔 이미지가 없을 때만 렌더한다(스캔 원본과 같은 150ppi)
MARGIN = 0.05  # 위아래 5%는 머리글 · 바닥글(저널명 · 쪽 번호 · 내려받은 곳)로 보고 뺀다
GUTTER = (0.47, 0.53)  # 두 단 사이. 이 구간을 가로지르는 상자는 두 단에 걸친 줄(제목 · 그림 설명)
SPANNING_SHARE = 0.25  # 가로지르는 상자가 이 비율을 넘으면 한 단 쪽으로 본다
_OCR_HEADINGS = re.compile(
    r"^((general )?(discussion|conclusions?)|results?( and discussion)?|methods?|methodology|"
    r"materials?( and methods?)?|introduction|abstract|summary|references|acknowledge?ments?|"
    r"(experiment|study) \d+[a-z]?|subjects|participants|measures|procedures?|stimuli|design|"
    r"apparatus|stressor|environments|overview|statistical analys[ie]s|data analysis|"
    r"effects of environments|physiological results|self-ratings?)$"
)


@dataclass(frozen=True)
class OcrBox:
    """OCR 한 조각. 좌표는 이미지 너비 · 높이에 대한 비율(0~1), 위가 0이다."""

    text: str
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def height(self) -> float:
        return self.y1 - self.y0


def _rows(boxes: Iterable[OcrBox]) -> list[list[OcrBox]]:
    """세로 위치로 줄을 묶는다(세로 중심이 줄 높이 절반 안). 줄 안은 왼쪽부터."""
    rows: list[list[OcrBox]] = []
    for box in sorted(boxes, key=lambda b: (b.yc, b.x0)):
        if rows:
            last = rows[-1]
            middle = sum(b.yc for b in last) / len(last)
            if abs(box.yc - middle) <= 0.5 * max(box.height, *(b.height for b in last)):
                last.append(box)
                continue
        rows.append([box])
    return [sorted(row, key=lambda b: b.x0) for row in rows]


def _spans(box: OcrBox) -> bool:
    return box.x0 < GUTTER[0] and box.x1 > GUTTER[1]


def reading_order(boxes: Iterable[OcrBox]) -> list[list[OcrBox]]:
    """한 쪽의 상자 → 읽기 순서의 줄. 두 단 쪽이면 두 단에 걸친 줄로 띠를 나누고, 띠마다
    왼쪽 단 → 오른쪽 단 → 그 아래 걸친 줄 순서다. 걸친 상자가 많으면 한 단 쪽이다."""
    boxes = [b for b in boxes if b.text.strip()]
    spanning = [b for b in boxes if _spans(b)]
    if not boxes or len(spanning) > SPANNING_SHARE * len(boxes):
        return _rows(boxes)
    span_rows = _rows(spanning)
    cuts = [sum(b.yc for b in row) / len(row) for row in span_rows]
    column = [b for b in boxes if not _spans(b)]
    ordered: list[list[OcrBox]] = []
    edges = [-1.0, *cuts, 2.0]
    for i, (low, high) in enumerate(zip(edges, edges[1:], strict=False)):
        band = [b for b in column if low < b.yc <= high]
        ordered += _rows(b for b in band if b.xc < 0.5)
        ordered += _rows(b for b in band if b.xc >= 0.5)
        if i < len(span_rows):
            ordered.append(span_rows[i])
    return ordered


def is_heading(text: str) -> bool:
    """OCR 줄이 제목인가 — 알려진 제목 이름과 줄 전체가 같을 때만(본문 줄 'Results showed …'는
    제목이 아니다). 번호 · 기호 · 글자 사이 공백은 heading_key가 뗀다."""
    text = text.strip()
    if not 0 < len(text) <= 60 or text.endswith((".", ",", ";")):  # 'stressor.'는 문단 끝줄이다
        return False
    return bool(_OCR_HEADINGS.match(heading_key(text)))


def _join(lines: list[str]) -> str:
    """줄을 문단으로. 'digit-' + 'span'처럼 하이픈으로 끝나고 소문자로 이어지면 붙인다."""
    text = ""
    for line in (line.strip() for line in lines):
        if not text:
            text = line
        elif text.endswith("-") and line[:1].islower():
            text += line
        else:
            text += f" {line}"
    return text


def layout_items(boxes: Iterable[OcrBox], page: int) -> list[RawItem]:
    """한 쪽의 OCR 상자 → RawItem. 제목 줄은 section_header, 나머지는 문단(text)으로 묶는다.
    줄 간격이 줄 높이의 0.8배를 넘거나, 위로 올라가거나(다음 단), 왼쪽 끝이 크게 바뀌면
    새 문단이다."""
    items: list[RawItem] = []
    lines: list[str] = []
    previous: list[OcrBox] | None = None

    def flush() -> None:
        if lines:
            items.append(RawItem("text", _join(lines), page))
            lines.clear()

    for row in reading_order(boxes):
        text = " ".join(b.text.strip() for b in row)
        if is_heading(text):
            flush()
            items.append(RawItem("section_header", text, page))
            previous = None
            continue
        if previous is not None:
            gap = min(b.y0 for b in row) - max(b.y1 for b in previous)
            height = max(b.height for b in (*row, *previous))
            if gap > 0.8 * height or gap < -height or abs(row[0].x0 - previous[0].x0) > 0.3:
                flush()
        lines.append(text)
        previous = row
    flush()
    return items


def make_ocr_engine():
    """RapidOCR(PP-OCR, onnxruntime). PIL 이미지를 받아 결과를 돌려주는 함수다 — 모델을 한 번만
    읽도록 호출한 쪽이 들고 있는다. numpy · rapidocr는 `.[ingestion]` extras라 여기서만 읽는다."""
    import numpy as np
    from rapidocr import RapidOCR

    engine = RapidOCR()
    return lambda image: engine(np.asarray(image))


def ocr_boxes(image, engine) -> list[OcrBox]:
    """이미지 한 장 → OcrBox. 위아래 MARGIN은 머리글 · 바닥글로 보고 뺀다."""
    result = engine(image.convert("RGB"))
    if result.boxes is None or result.txts is None:
        return []
    width, height = image.size
    boxes = []
    for points, text in zip(result.boxes, result.txts, strict=True):
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        box = OcrBox(text, min(xs) / width, min(ys) / height, max(xs) / width, max(ys) / height)
        if MARGIN < box.yc < 1 - MARGIN:
            boxes.append(box)
    return boxes


def page_images(pdf_path: Path) -> Iterator[tuple[int, object]]:
    """(쪽 번호, PIL 이미지). 쪽에 든 가장 큰 스캔 이미지를 해상도 그대로 꺼낸다 — 렌더하면
    브라우저 인쇄 머리글 같은 벡터 글자가 섞인다. 이미지가 없는 쪽만 RENDER_DPI로 렌더한다."""
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_raw

    pdf = pdfium.PdfDocument(str(pdf_path))
    for index in range(len(pdf)):
        page = pdf[index]
        images = list(page.get_objects(filter=[pdfium_raw.FPDF_PAGEOBJ_IMAGE]))
        if images:
            largest = max(images, key=lambda o: o.get_px_size()[0] * o.get_px_size()[1])
            yield index + 1, largest.get_bitmap(render=False).to_pil()
        else:
            yield index + 1, page.render(scale=RENDER_DPI / 72).to_pil()


def ocr_pdf(paper_id: str, pdf_path: Path, *, engine=None) -> ParsedPaper:
    """스캔 PDF 한 편 → ParsedPaper(tool = OCR_TOOL). engine을 넘기면 모델을 다시 읽지 않는다."""
    engine = engine or make_ocr_engine()
    items: list[RawItem] = []
    pages = 0
    for page_no, image in page_images(pdf_path):
        pages = page_no
        items.extend(layout_items(ocr_boxes(image, engine), page_no))
    return ParsedPaper(paper_id, str(pdf_path.name), pages, items, OCR_TOOL)
