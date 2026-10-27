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
- 스캔 PDF(OCR)는 macOS면 Apple Vision(`ocrmac`, `.[ingestion]` extras)을, 아니면 Docling 기본
  RapidOCR을 쓴다. 150ppi 스캔(P05 · P26)에서 RapidOCR은 낱말이 깨지고('tahle', 'haarg r arg')
  Vision은 대부분 바로 읽는다. 둘 다 통계 아래첨자 · 소수점('p_rep = .95' → 'Prep = 95')은 놓치므로
  OCR 텍스트의 발췌는 쪽 이미지와 대조한 뒤에 쓴다(extract_excerpts --include-ocr).

JATS XML은 루트가 `<article>`이고 `body/sec`가 섹션이다. 섹션마다 `title`이 있다
(예: 1. Introduction, 2. Method, 3. Results, 4. Discussion). 실험 조건과 결과는
Method·Results와 표·그림 캡션에 흩어져 있어 함께 뽑는다. (JATS 경로는 아직 구현하지 않았다.)
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
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
_RESULTS = re.compile(r"^(results?|findings)(\b|$)")
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


OCR_ENGINES = ("mac", "rapid")


def ocr_engine(requested: str | None = None) -> str:
    """OCR 엔진. 지정이 없으면 macOS + ocrmac이면 Apple Vision('mac'), 아니면 RapidOCR('rapid')."""
    if requested:
        if requested not in OCR_ENGINES:
            raise ValueError(f"OCR 엔진은 {OCR_ENGINES} 중 하나다: {requested!r}")
        return requested
    has_vision = sys.platform == "darwin" and importlib.util.find_spec("ocrmac") is not None
    return "mac" if has_vision else "rapid"


def tool_label(*, ocr: bool, engine: str | None = None) -> str:
    """캐시의 tool 값. extract_excerpts는 'ocr'이 들어 있으면 OCR 텍스트로 본다."""
    return f"docling+ocr-{ocr_engine(engine)}" if ocr else "docling"


def make_converter(*, ocr: bool, engine: str | None = None):
    """Docling 변환기. 텍스트층 PDF는 OCR을 끄고(빠르다), 스캔 PDF만 켠다. 표 구조는 늘 켠다."""
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import OcrMacOptions, PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    options = PdfPipelineOptions()
    options.do_ocr = ocr
    options.do_table_structure = True
    if ocr and ocr_engine(engine) == "mac":
        options.ocr_options = OcrMacOptions(recognition="accurate", lang=["en-US"])
    pdf_option = PdfFormatOption(pipeline_options=options)
    return DocumentConverter(format_options={InputFormat.PDF: pdf_option})


def parse_pdf(
    paper_id: str,
    pdf_path: Path,
    *,
    converter=None,
    ocr: bool = False,
    engine: str | None = None,
) -> ParsedPaper:
    """PDF 한 편 → ParsedPaper. converter를 넘기면 모델을 다시 읽지 않는다."""
    converter = converter or make_converter(ocr=ocr, engine=engine)
    document = converter.convert(str(pdf_path)).document
    return ParsedPaper(
        paper_id=paper_id,
        source=str(pdf_path.name),
        pages=len(document.pages),
        items=list(_docling_items(document)),
        tool=tool_label(ocr=ocr, engine=engine),
    )
