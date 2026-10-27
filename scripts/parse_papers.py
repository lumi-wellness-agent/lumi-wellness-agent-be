"""논문 PDF → 섹션 텍스트 캐시(Docling). app/ingestion/parse.py를 논문 목록에 돌린다.

    .venv/bin/python scripts/parse_papers.py --with-records     # 레코드가 있는 논문(PDF 있는 것)
    .venv/bin/python scripts/parse_papers.py P16 P18 --force     # 지정 논문만, 캐시 덮어쓰기
    .venv/bin/python scripts/parse_papers.py --all               # manifest의 PDF 76편 전부

- PDF 경로는 data/manifest.csv의 pdf_path(Research_paper 저장소 기준). fulltext_status가
  IMAGE_PDF면 OCR을 켠다(--no-ocr로 끈다). 엔진은 macOS면 Apple Vision, 아니면 RapidOCR
  (--ocr-engine mac|rapid).
- 캐시는 Research_paper/LUMI_R_C_Research_2026-10-22/01_fulltext/<paper_id>.json · .md — 비공개
  저장소다. 이미 있으면 건너뛴다(--force로 덮어씀). 한 편에 CPU로 10~60초 걸린다.
- `.[ingestion]` extras(docling)가 필요하다: .venv/bin/pip install -e ".[ingestion]"
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.ingestion import parse  # noqa: E402

MANIFEST = ROOT / "data" / "manifest.csv"


def papers_with_records(database_url: str | None) -> set[str]:
    """DB가 있으면 evidence_records의 paper_id, 없으면 활동 노드 JSON evidence[]의 논문."""
    if database_url:
        import psycopg

        sql = "select distinct paper_id from public.evidence_records"
        with psycopg.connect(database_url, prepare_threshold=None) as conn:
            return {r[0] for r in conn.execute(sql)}
    from app.protocols import load_protocols

    return {e.paper_id for p in load_protocols().values() for e in p.evidence}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("papers", nargs="*", help="논문 ID(P16 …). 비우면 --with-records · --all")
    parser.add_argument("--with-records", action="store_true", help="근거 레코드가 있는 논문만")
    parser.add_argument("--all", action="store_true", help="PDF가 있는 논문 전부")
    parser.add_argument("--database-url", help="--with-records 판정에 쓸 DB(없으면 프로토콜 JSON)")
    parser.add_argument("--research-root", type=Path, help="Research_paper 경로(기본: 형제 폴더)")
    parser.add_argument("--force", action="store_true", help="캐시가 있어도 다시 파싱")
    parser.add_argument("--no-ocr", action="store_true", help="IMAGE_PDF도 OCR 없이(텍스트층만)")
    parser.add_argument("--ocr-engine", choices=parse.OCR_ENGINES, help="기본: macOS면 mac")
    parser.add_argument(
        "--reindex", action="store_true", help="Docling 없이 캐시로 .md만 다시 쓴다(규칙 변경 뒤)"
    )
    args = parser.parse_args()

    research = args.research_root or parse.research_root()
    cache_dir = research / parse.FULLTEXT_DIR
    if args.reindex:
        papers = [parse.load_cached(cache_dir, p.stem) for p in sorted(cache_dir.glob("*.json"))]
        for paper in filter(None, papers):
            parse.save_cache(cache_dir, paper)
            chars = " ".join(f"{k}={v}" for k, v in sorted(paper.chars_by_section().items()))
            print(f"{paper.paper_id}: {chars}")
        return 0
    rows = {r["rc_id"]: r for r in csv.DictReader(MANIFEST.open(encoding="utf-8"))}
    if args.papers:
        wanted = list(dict.fromkeys(args.papers))
    elif args.with_records:
        wanted = sorted(papers_with_records(args.database_url))
    elif args.all:
        wanted = sorted(rows)
    else:
        parser.error("논문 ID나 --with-records 또는 --all을 준다")

    todo: list[tuple[str, Path, bool]] = []
    for pid in wanted:
        row = rows.get(pid)
        if row is None:
            print(f"{pid}: manifest에 없다", file=sys.stderr)
            continue
        if not row["pdf_path"]:
            print(f"{pid}: PDF 없음({row['fulltext_status']}) — 건너뜀")
            continue
        pdf = research / row["pdf_path"]
        if not pdf.exists():
            print(f"{pid}: 파일이 없다 {pdf}", file=sys.stderr)
            continue
        if not args.force and parse.load_cached(cache_dir, pid) is not None:
            print(f"{pid}: 캐시 있음 — 건너뜀")
            continue
        todo.append((pid, pdf, row["fulltext_status"] == "IMAGE_PDF" and not args.no_ocr))

    print(f"파싱 {len(todo)}편 → {cache_dir}", flush=True)
    converters: dict[bool, object] = {}
    failed: list[str] = []
    for i, (pid, pdf, ocr) in enumerate(todo, 1):
        started = time.perf_counter()
        try:
            if ocr not in converters:
                converters[ocr] = parse.make_converter(ocr=ocr, engine=args.ocr_engine)
            paper = parse.parse_pdf(
                pid, pdf, converter=converters[ocr], ocr=ocr, engine=args.ocr_engine
            )
            parse.save_cache(cache_dir, paper)
        except Exception as exc:  # noqa: BLE001 — 한 편이 실패해도 나머지는 계속
            failed.append(pid)
            print(f"[{i}/{len(todo)}] {pid}: 실패 {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        chars = " ".join(f"{k}={v}" for k, v in sorted(paper.chars_by_section().items()))
        elapsed = time.perf_counter() - started
        mode = f"OCR({parse.ocr_engine(args.ocr_engine)}) " if ocr else ""
        print(f"[{i}/{len(todo)}] {pid}: {paper.pages}p {elapsed:.0f}s {mode}{chars}")
        sys.stdout.flush()
    if failed:
        print(f"실패 {len(failed)}편: {' '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
