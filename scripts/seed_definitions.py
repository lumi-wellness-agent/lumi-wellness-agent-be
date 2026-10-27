"""개념 정의 카드의 원문 구간 → evidence_records(record_type = DEFINITION) 적재.

    .venv/bin/python scripts/seed_definitions.py --dry-run
    .venv/bin/python scripts/seed_definitions.py --database-url <URL>

- 대상은 data/construct_definitions.json의 sources(목표 카드 출처 + 경계 출처 P06). anchor가 든
  문단을 파싱 캐시(Research_paper/…/01_fulltext/<paper_id>.json, scripts/parse_papers.py)에서 찾아
  그 문단 원문을 content로 쓴다. content는 원문 그대로다(EvidenceRecordIn 계약 — 요약 금지).
  캐시가 없으면 `make parse-papers ARGS="<논문 ID…>"`를 먼저 돌리라고 알리고 멈춘다.
- papers 행은 scripts/seed_evidence.py가 manifest 83편 전부를 이미 넣는다. 없으면 멈춘다.
- chunk_idx는 CHUNK_BASE부터 논문마다 0, 1, …을 더한다. (paper_id, experiment_id = null, section,
  chunk_idx)가 키라, 같은 논문의 통합·관찰 레코드(chunk_idx 0부터)와 겹치지 않게 띄운다.
  다시 돌려도 된다 — 같은 키면 갱신해 id가 유지된다.
- 임베딩 입력은 논문 제목 + "definition · <목표 지표>" 한 줄 + content(seed_evidence와 같은 모양).
- 대상 DB는 매번 적는다. 기본값을 두지 않는다(공용 Supabase에 실수로 쓰지 않게).
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.constructs import all_sources, goal_for_paper, load_cards  # noqa: E402
from app.ingestion import parse  # noqa: E402
from app.retrieval.embedding import EMBEDDING_DIMS, embeddings  # noqa: E402
from app.schemas.evidence import (  # noqa: E402
    EvidenceMetadata,
    EvidenceRecordIn,
    RecordType,
    StudyDesign,
    Verification,
)

MANIFEST = ROOT / "data" / "manifest.csv"
CHUNK_BASE = 100

RECORD_SQL = """
insert into public.evidence_records
  (paper_id, experiment_id, section, chunk_idx, content, metadata, embedding, embedding_model)
values (%s, %s, %s, %s, %s, %s, %s, %s)
on conflict (paper_id, experiment_id, section, chunk_idx) do update set
  content = excluded.content, metadata = excluded.metadata,
  embedding = excluded.embedding, embedding_model = excluded.embedding_model
returning id, (xmax = 0) as inserted
"""


def normalize(text: str) -> str:
    """줄 끝 하이픈 · 소프트 하이픈 · 공백을 지운 소문자 — PDF 줄바꿈과 무관하게 대조한다."""
    text = re.sub(r"-\s*\n\s*", "", text).replace("­", "")
    return re.sub(r"\s+", " ", text).strip().lower()


@dataclass
class Found:
    record: EvidenceRecordIn
    title: str
    anchor: str
    page: int | None


def find_block(paper: parse.ParsedPaper, anchor: str) -> parse.Block | None:
    wanted = normalize(anchor)
    for block in paper.blocks:
        if wanted in normalize(block.text):
            return block
    return None


def build(cache_dir: Path, papers: dict[str, dict[str, str]]) -> tuple[list[Found], list[str]]:
    found: list[Found] = []
    problems: list[str] = []
    next_idx: dict[str, int] = {}
    for source in all_sources():
        pid = source.paper_id
        row = papers.get(pid)
        if row is None:
            problems.append(f"{pid}: data/manifest.csv에 없다")
            continue
        paper = parse.load_cached(cache_dir, pid)
        if paper is None:
            problems.append(f'{pid}: 파싱 캐시가 없다 — make parse-papers ARGS="{pid}"')
            continue
        block = find_block(paper, source.anchor)
        if block is None:
            problems.append(f"{pid}: anchor를 찾지 못했다 — {source.anchor!r}")
            continue
        idx = next_idx.get(pid, 0)
        next_idx[pid] = idx + 1
        metadata = EvidenceMetadata(
            record_type=RecordType.DEFINITION,
            study_design=StudyDesign(row["study_design"]),
            verification=Verification.FULL_TEXT,
        )
        record = EvidenceRecordIn(
            paper_id=pid,
            experiment_id=None,
            section=block.section,
            chunk_idx=CHUNK_BASE + idx,
            content=block.text,
            metadata=metadata,
        )
        found.append(Found(record, row["title"], source.anchor, block.page))
    return found, problems


def embedding_text(item: Found) -> str:
    goal = goal_for_paper(item.record.paper_id)
    outcome = load_cards().goals[goal].outcome.value.lower() if goal else "boundary"
    return f"{item.title}\ndefinition · {outcome.replace('_', ' ')}\n{item.record.content}"


def load(database_url: str, items: list[Found]) -> None:
    import psycopg
    from pgvector import Vector
    from pgvector.psycopg import register_vector
    from psycopg.types.json import Jsonb

    settings = get_settings()
    vectors = embeddings(settings).embed_documents([embedding_text(i) for i in items])
    wrong = {len(v) for v in vectors} - {EMBEDDING_DIMS}
    if wrong:
        raise SystemExit(
            f"{settings.embedding_model}의 차원 {wrong}이 vector({EMBEDDING_DIMS})와 다르다"
        )
    with psycopg.connect(database_url, prepare_threshold=None) as conn:
        register_vector(conn)
        with conn.cursor() as cur:
            ids = {i.record.paper_id for i in items}
            cur.execute("select id from public.papers where id = any(%s)", (sorted(ids),))
            missing = ids - {r[0] for r in cur.fetchall()}
            if missing:
                raise SystemExit(
                    f"papers에 없는 논문 {sorted(missing)} — seed_evidence를 먼저 돌린다"
                )
            for item, vector in zip(items, vectors, strict=True):
                r = item.record
                cur.execute(
                    RECORD_SQL,
                    (
                        r.paper_id,
                        None,
                        r.section.value,
                        r.chunk_idx,
                        r.content,
                        Jsonb(r.metadata.model_dump(mode="json")),
                        Vector(vector),
                        settings.embedding_model,
                    ),
                )
                record_id, inserted = cur.fetchone()
                print(
                    f"  [{record_id}] {r.paper_id} {r.section.value} #{r.chunk_idx}"
                    f" {'새' if inserted else '갱신'}"
                )
    print(f"적재: DEFINITION {len(items)}행 ({settings.embedding_model})")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dry-run", action="store_true", help="DB · 임베딩 없이 찾은 구간만 본다")
    parser.add_argument("--database-url", help="적재할 DB(기본값 없음)")
    parser.add_argument("--research-root", type=Path, help="Research_paper 경로(기본: 형제 폴더)")
    args = parser.parse_args()
    if not args.dry_run and not args.database_url:
        parser.error("--dry-run 또는 --database-url을 준다")

    research = args.research_root or parse.research_root()
    papers = {r["rc_id"]: r for r in csv.DictReader(MANIFEST.open(encoding="utf-8"))}
    items, problems = build(research / parse.FULLTEXT_DIR, papers)
    for item in items:
        r = item.record
        page = f" p.{item.page}" if item.page else ""
        print(
            f"{r.paper_id} {r.section.value}{page} #{r.chunk_idx} ← {item.anchor!r}"
            f" ({len(r.content)}자)"
        )
    for problem in problems:
        print(f"문제: {problem}")
    if problems:
        return 1
    if args.dry_run:
        return 0
    load(args.database_url, items)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
