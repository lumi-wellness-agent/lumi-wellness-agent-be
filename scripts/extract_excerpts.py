"""레코드(arm 61개)마다 논문 원문 METHODS · RESULTS 발췌를 고른다 → data/evidence_excerpts.json.

    .venv/bin/python scripts/extract_excerpts.py <xlsx> --dry-run    # 입력 크기 · 캐시 유무만
    .venv/bin/python scripts/extract_excerpts.py <xlsx>              # 캐시된 논문 전부(LLM 비용)
    .venv/bin/python scripts/extract_excerpts.py <xlsx> --only P16-E1 P01 --force

- 레코드와 메타데이터는 seed_evidence.build_records(근거 arm 시트 + overrides)에서 가져온다.
- 논문 텍스트는 scripts/parse_papers.py가 만든 캐시(Research_paper/…/01_fulltext). 캐시가
  없는 논문(미확보 · 아직 파싱 안 함)은 건너뛰고 알린다. OCR로 읽은 논문(스캔 PDF)은
  숫자 오인식 위험이 있어 --include-ocr일 때만 쓴다.
- 발췌는 app/ingestion/extract.py — LLM(설정의 llm_model)이 고르고 본문 문장으로 맞춘다.
  이미 JSON에 있는 키는 건너뛴다(--force로 다시). 결과는 키별로 병합해 쓴다.
- 검토 목록은 --review CSV(기본 evidence_excerpts_review.csv, gitignore) — confidence가
  HIGH가 아니거나 coverage가 낮은 레코드. 사람이 p값 · N 같은 숫자를 PDF와 대조한다.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.ingestion import extract, parse  # noqa: E402
from app.schemas.evidence import Section  # noqa: E402
from scripts.scaffold_protocols import read_sheet  # noqa: E402
from scripts.seed_evidence import MANIFEST, OVERRIDES, build_records, load_overrides  # noqa: E402

EXCERPTS = ROOT / "data" / "evidence_excerpts.json"
REVIEW = ROOT / "evidence_excerpts_review.csv"


def write_json(path: Path, payload: dict) -> None:
    text = json.dumps(dict(sorted(payload.items())), ensure_ascii=False, indent=1)
    path.write_text(text + "\n", encoding="utf-8")


def review_row(result: extract.RecordExcerpts) -> dict[str, str]:
    def head(section: Section) -> str:
        excerpt = result.sections.get(section)
        return excerpt.text[:200] if excerpt else ""

    return {
        "key": result.key,
        "paper_id": result.paper_id,
        "reasons": "; ".join(result.review_reasons),
        "note": result.note or "",
        "methods": head(Section.METHODS),
        "results": head(Section.RESULTS),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("xlsx", type=Path, help="LUMI_측정도구_프로토콜 워크북")
    parser.add_argument("--out", type=Path, default=EXCERPTS)
    parser.add_argument("--review", type=Path, default=REVIEW)
    parser.add_argument("--research-root", type=Path, help="Research_paper 경로(기본: 형제 폴더)")
    parser.add_argument("--only", nargs="*", help="레코드 키(P16-E1 · P01 …)만")
    parser.add_argument("--limit", type=int, help="앞에서 N개만(비용 확인용)")
    parser.add_argument("--workers", type=int, default=4, help="동시 LLM 호출 수")
    parser.add_argument("--force", action="store_true", help="JSON에 있는 키도 다시 고른다")
    parser.add_argument("--include-ocr", action="store_true", help="OCR로 읽은 논문도 쓴다")
    parser.add_argument("--dry-run", action="store_true", help="LLM 없이 입력 크기와 캐시 유무만")
    args = parser.parse_args()

    settings = get_settings()
    research = args.research_root or parse.research_root()
    cache_dir = research / parse.FULLTEXT_DIR
    rows = read_sheet(args.xlsx, "근거 arm", "protocol_id")
    papers = {row["rc_id"]: row for row in csv.DictReader(MANIFEST.open(encoding="utf-8"))}
    records, _ = build_records(rows, papers, load_overrides(OVERRIDES))
    existing: dict = {}
    if args.out.exists():
        existing = json.loads(args.out.read_text(encoding="utf-8"))

    only = set(args.only or [])
    todo, skipped = [], []
    for record in records:
        if only and record.key not in only:
            continue
        if record.key in existing and not args.force:
            skipped.append(f"{record.key}: 이미 있음")
            continue
        paper_id = record.chunks[0].paper_id
        paper = parse.load_cached(cache_dir, paper_id)
        if paper is None:
            skipped.append(f"{record.key}: 캐시 없음({papers[paper_id]['fulltext_status']})")
            continue
        if "ocr" in paper.tool and not args.include_ocr:
            skipped.append(f"{record.key}: OCR 텍스트(--include-ocr)")
            continue
        todo.append((record, paper))
    if args.limit:
        todo = todo[: args.limit]
    for line in skipped:
        print(f"  건너뜀 {line}")
    print(f"발췌 대상 {len(todo)}개 (레코드 {len(records)}개 중)", flush=True)

    if args.dry_run:
        for record, paper in todo:
            facts = extract.facts_from_seed(record, title=paper.source)
            size = len(extract.build_user_prompt(facts, paper))
            print(f"  {record.key}: 입력 {size:,}자 · {paper.pages}p · {paper.chars_by_section()}")
        return 0
    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY가 없어 발췌를 고를 수 없다(.env)")

    picker = extract.make_picker(model=settings.llm_model, api_key=settings.openai_api_key)
    stamp = time.strftime("%Y-%m-%d")
    cached = (parse.load_cached(cache_dir, path.stem) for path in cache_dir.glob("*.json"))
    vocab = extract.corpus_vocab(filter(None, cached))  # 합자 복원용 낱말 빈도

    def work(record, paper) -> extract.RecordExcerpts:
        title = papers[record.chunks[0].paper_id]["title"]
        facts = extract.facts_from_seed(record, title=title)
        source = f"{paper.tool} {paper.source} · {settings.llm_model} · {stamp}"
        return extract.extract_record(facts, paper, picker, source=source, vocab=vocab)

    review_rows: list[dict[str, str]] = []
    done = failed = 0
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(work, record, paper): record.key for record, paper in todo}
        for i, future in enumerate(as_completed(futures), 1):
            key = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 — 한 레코드 실패가 전체를 막지 않는다
                failed += 1
                print(f"[{i}/{len(todo)}] {key}: 실패 {type(exc).__name__}: {exc}", file=sys.stderr)
                continue
            existing[key] = result.to_json()
            write_json(args.out, existing)
            done += 1
            sizes = " ".join(
                f"{s.value}={len(e.text)}({e.coverage:.2f})"
                + (f"·복원{e.repaired}" if e.repaired else "")
                for s, e in result.sections.items()
            )
            flag = " ← 검토" if result.review_reasons else ""
            print(f"[{i}/{len(todo)}] {key}: {result.confidence} {sizes}{flag}", flush=True)
            if result.review_reasons:
                review_rows.append(review_row(result))
    if review_rows:
        with args.review.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(review_rows[0]))
            writer.writeheader()
            writer.writerows(sorted(review_rows, key=lambda r: r["key"]))
    elapsed = time.perf_counter() - started
    target = args.review if review_rows else "없음"
    print(f"완료 {done} · 실패 {failed} · 검토 {len(review_rows)}건 → {target} · {elapsed:.0f}s")
    print(f"저장: {args.out} ({len(existing)}개 키)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
