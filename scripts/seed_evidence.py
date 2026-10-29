"""근거 arm 시트 → papers · evidence_records 적재. Docling 추출 전에 쓰는 데모용 씨앗이다.

    .venv/bin/python scripts/seed_evidence.py <xlsx> --dry-run
    .venv/bin/python scripts/seed_evidence.py <xlsx> --database-url <URL>

- papers: data/manifest.csv 84편 전부.
- evidence_records: 근거 arm 시트 65행 → 레코드 61개(여러 프로토콜이 인용한 같은 arm은 하나로) ×
  chunk 2개(METHODS = 대상·N·중재·측정, RESULTS = 결과). content는 그 키의 발췌가
  data/evidence_excerpts.json에 있으면 논문 원문 구간, 없으면 시트 요약이다(--no-excerpts).
- 메타데이터는 시트 열에서 정한다. 매체는 시트에 없어서 그 행을 인용한 프로토콜 JSON의 media를
  연구 매체로 바꿔 쓴다(AI_IMAGE → STATIC_IMAGE). 효과는 결과 열을 ';'로 나눈 구절마다 정한다 —
  유의 표현만 있으면 POSITIVE, 비유의·차이 없음·재현 실패만 있으면 NOT_SIGNIFICANT, 섞이거나 없으면
  UNCLEAR. 측정 지표에 있는데 어느 구절에도 안 나온 지표는 UNCLEAR다. 결과 열이 지표를 하나도 들지
  않을 때만('누적 효과 g=0.15 비유의') 그 판정을 측정 지표 전체에 쓴다.
- data/evidence_overrides.json이 시트에서 정한 값을 필드 단위로 덮어쓴다(사람이 원문으로 확정한 값).
  키는 experiment_id, arm이 없는 레코드(메타·관찰)는 paper_id. `_why`에 근거를 적는다.
- data/evidence_excerpts.json(scripts/extract_excerpts.py)은 같은 키 체계로 METHODS · RESULTS의 원문
  발췌를 준다. 발췌가 있는 섹션만 content를 바꾸고 없는 섹션은 시트 요약 그대로다. 키 · section ·
  chunk_idx는 그대로라 id가 유지된다.
- 사람이 확인할 항목은 --review CSV에 남긴다. dry-run은 DB·임베딩 없이 건수와 이 파일만 만든다.
- 임베딩은 검색과 같은 클라이언트(app/retrieval/embedding.py)가 설정의 embedding_model(OpenAI)로
  만든다. 입력은 논문 제목 + 메타데이터의 영어 라벨 한 줄 + content다. content가 한국어 요약이라
  영어 질의와 멀어서 붙인다(저장하는 content는 그대로).
- 다시 돌려도 된다. (paper_id, experiment_id, section, chunk_idx)가 같으면 갱신해 id가 유지된다 —
  세션 기록(care_rounds.evidence_record_ids)과 계획(CarePlan)이 이 id를 가리킨다.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.ingestion.extract import load_excerpts  # noqa: E402
from app.protocols import load_protocols  # noqa: E402
from app.retrieval.embedding import EMBEDDING_DIMS, embeddings  # noqa: E402
from app.schemas.evidence import (  # noqa: E402
    DESIGN_LABELS,
    VERIFICATION_LABELS,
    ClinicalStatus,
    Effect,
    EvidenceMetadata,
    EvidenceRecordIn,
    FulltextStatus,
    Medium,
    Outcome,
    OutcomeResult,
    Population,
    RecordType,
    Section,
    StudyDesign,
    Verification,
    parse_orders,
    study_medium,
)
from scripts.scaffold_protocols import (  # noqa: E402
    NEGATIVE,
    OUTCOME_KEYWORDS,
    POSITIVE,
    blank,
    map_outcomes,
    read_sheet,
)

MANIFEST = ROOT / "data" / "manifest.csv"
OVERRIDES = ROOT / "data" / "evidence_overrides.json"
EXCERPTS = ROOT / "data" / "evidence_excerpts.json"

RECORD_TYPE_FOR: dict[StudyDesign, RecordType] = {
    StudyDesign.RANDOMIZED_EXPERIMENT: RecordType.ARM,
    StudyDesign.QUASI_EXPERIMENT: RecordType.ARM,
    StudyDesign.SYSTEMATIC_REVIEW_META: RecordType.SYNTHESIS,
    StudyDesign.OBSERVATIONAL: RecordType.OBSERVATION,
}
KEYWORDS = dict(OUTCOME_KEYWORDS)
ARM_ID = re.compile(r"^([PEN]\d{2})-E(\d+)")
MERGED_ARMS = re.compile(r"-[ES]\d+/[ES]?\d+")  # P31-E1/E2 · P38-S1/S2
P_VALUE = re.compile(r"p\s*([=<])\s*(0?\.\d+)")
WEAK_POSITIVE = ("회복",)  # 명사로도 쓰여('회복 효과 불안정') 비유의 표현과 함께 나오면 무시한다
STUDENT = ("학생", "대생")  # 대학생 · 간호대생
ADULT = ("성인", "여성", "플레이어", "진단자")
SAME_AS_ABOVE = "동일"


@dataclass
class Issue:
    key: str
    protocols: str
    field: str
    issue: str
    value: str
    sheet: str


@dataclass
class Record:
    key: str
    protocols: list[str]
    title: str
    metadata: EvidenceMetadata
    chunks: list[EvidenceRecordIn]
    excerpted: list[Section] = field(default_factory=list)  # 원문 발췌로 바꾼 섹션


# ── 시트 값 → 메타데이터 ──────────────────────────────────────────────────────────────────


def clause_effect(clause: str) -> Effect | None:
    """구절 하나의 판정. 유의 표현만 → POSITIVE, 비유의 표현만 → NOT_SIGNIFICANT, 둘 다 → UNCLEAR,
    둘 다 없으면 None. p값은 .05로 가른다."""
    stripped = clause
    for cue in NEGATIVE:  # '비유의'가 '유의'로 읽히지 않게 먼저 지운다
        stripped = stripped.replace(cue, "")
    negative = stripped != clause
    positive = any(cue in stripped for cue in POSITIVE if cue not in WEAK_POSITIVE)
    weak = any(cue in stripped for cue in WEAK_POSITIVE)
    for op, value in P_VALUE.findall(clause):
        if op == "<" or float(value) < 0.05:
            positive = True
        else:
            negative = True
    if positive and negative:
        return Effect.UNCLEAR
    if positive or (weak and not negative):
        return Effect.POSITIVE
    return Effect.NOT_SIGNIFICANT if negative else None


def measure_for(outcome: Outcome, text: str) -> str | None:
    """text에 나온 그 지표의 키워드. 다른 키워드에 들어 있는 것('HR' ⊂ 'HRV')은 뺀다."""
    keys = [k for k in KEYWORDS[outcome] if k in text]
    return "·".join(k for k in keys if not any(k != o and k in o for o in keys)) or None


def derive_results(measures: str, result: str) -> tuple[list[OutcomeResult], list[str]]:
    """측정 지표 열 · 결과 열 → 결과 목록과 검토 사유. 측정 지표 열에 없는 지표는 넣지 않는다."""
    measured = map_outcomes(measures)
    results: list[OutcomeResult] = []
    reasons: list[str] = []
    general: list[tuple[Effect, str]] = []
    covered: set[Outcome] = set()
    for clause in (c.strip() for c in result.split(";")):
        if not clause:
            continue
        effect = clause_effect(clause)
        outcomes = [o for o in map_outcomes(clause) if o in measured]
        if not outcomes:
            if effect is not None:
                general.append((effect, clause))
            continue
        for outcome in outcomes:
            results.append(
                OutcomeResult(
                    outcome=outcome,
                    effect=effect or Effect.UNCLEAR,
                    measure=measure_for(outcome, clause),
                    note=clause,
                )
            )
            covered.add(outcome)
        if effect in (None, Effect.UNCLEAR):
            names = "·".join(o.value for o in outcomes)
            reasons.append(f"'{clause}'에서 {names}의 방향을 못 정해 UNCLEAR")
    rest = [o for o in measured if o not in covered]
    if rest:
        names = "·".join(o.value for o in rest)
        effects = {e for e, _ in general}
        if not covered and len(effects) == 1:
            effect, note = effects.pop(), "; ".join(c for _, c in general)
            reasons.append(
                f"{names}: 지표를 들지 않은 구절 '{note}'의 판정 {effect.value}를 따랐다"
            )
        else:
            effect, note = Effect.UNCLEAR, "결과 열에 이 지표의 결과가 없다"
            reasons.append(f"{names}: 결과 열에서 판정할 구절을 못 찾아 UNCLEAR")
        for outcome in rest:
            results.append(
                OutcomeResult(
                    outcome=outcome,
                    effect=effect,
                    measure=measure_for(outcome, measures),
                    note=note,
                )
            )
    return results, reasons


def derive_population(
    text: str, record_type: RecordType
) -> tuple[Population | None, ClinicalStatus | None]:
    """대상 열 → (Population, ClinicalStatus). 진단 집단만 CLINICAL — 호소(P38)는 NON_CLINICAL."""
    synthesis = record_type is RecordType.SYNTHESIS
    if blank(text):
        return (Population.MIXED if synthesis else None), None
    if "진단" in text or ("임상" in text and "비임상" not in text):
        clinical = ClinicalStatus.CLINICAL
    elif "비임상" in text or "건강" in text or not synthesis:
        clinical = ClinicalStatus.NON_CLINICAL
    else:
        clinical = None
    if any(s in text for s in STUDENT):
        population = Population.UNIVERSITY_STUDENT
    elif any(a in text for a in ADULT):
        population = Population.GENERAL_ADULT
    else:
        population = Population.MIXED if synthesis else None
    return population, clinical


def derive_counts(text: str, record_type: RecordType) -> dict:
    """N 열 → n · n_detail · n_studies. 실험 둘의 N이 함께 적힌 행('50 / 42')은 n을 비운다."""
    t = text.strip()
    if blank(t) or t == "원문 확인":
        return {}
    out: dict = {"n_detail": t}
    if record_type is RecordType.SYNTHESIS:
        if m := re.search(r"(\d+)\s*편", t) or re.search(r"k\s*=\s*(\d+)", t):
            out["n_studies"] = int(m.group(1))
        if m := re.search(r"(\d+)\s*명", t):
            out["n"] = int(m.group(1))
        return out
    if "/" not in re.sub(r"\(.*?\)", "", t) and (m := re.match(r"^(\d+)", t)):
        out["n"] = int(m.group(1))
    return out


def chunk_texts(row: dict[str, str], population: str) -> dict[Section, str]:
    label = row["arm / experiment_id"]
    methods = [label]
    for name, value in (
        ("대상", population),
        ("N", row["N"]),
        ("중재", row["중재(자극·시간)"]),
        ("측정", row["측정 지표"]),
    ):
        if not blank(value) and value != "원문 확인":
            methods.append(f"{name}: {value}")
    texts = {Section.METHODS: "\n".join(methods)}
    if not blank(row["결과"]):
        texts[Section.RESULTS] = f"{label}\n결과: {row['결과']}"
    return texts


def embedding_text(title: str, metadata: EvidenceMetadata, content: str) -> str:
    """제목과 content 사이에 영어 라벨 한 줄을 넣는다 — 'static image · post stress · outcomes:
    affect, rumination'."""

    def words(values) -> str:
        return ", ".join(dict.fromkeys(v.value.lower().replace("_", " ") for v in values))

    outcomes = words(r.outcome for r in metadata.results if r.outcome is not Outcome.OTHER)
    labels = [
        words(metadata.media),
        words(metadata.intervention_orders),
        f"outcomes: {outcomes}" if outcomes else "",
    ]
    return f"{title}\n{' · '.join(x for x in labels if x)}\n{content}"


def results_summary(results: list[OutcomeResult]) -> str:
    return "; ".join(
        f"{r.outcome.value} {r.effect.value} {r.measure or ''}".strip() for r in results
    )


# ── 레코드 조립 ───────────────────────────────────────────────────────────────────────────


def load_excerpts_file(path: Path) -> dict[str, dict[Section, str]]:
    """data/evidence_excerpts.json → {키: {Section: 원문 발췌}}. 파일이 없으면 빈 dict."""
    if not path.exists():
        return {}
    return load_excerpts(json.loads(path.read_text(encoding="utf-8")))


def apply_excerpts(
    texts: dict[Section, str], found: dict[Section, str] | None
) -> tuple[dict[Section, str], list[Section]]:
    """시트 요약 content에 원문 발췌를 덧씌운다. 발췌가 있는 섹션만 바꾸고(시트에 결과가
    없어 RESULTS가 없던 레코드에는 더한다) 순서는 METHODS · RESULTS로 둔다."""
    if not found:
        return texts, []
    merged = dict(texts)
    replaced: list[Section] = []
    for section in (Section.METHODS, Section.RESULTS):
        text = (found.get(section) or "").strip()
        if text:
            merged[section] = text
            replaced.append(section)
    ordered = {s: merged[s] for s in (Section.METHODS, Section.RESULTS) if s in merged}
    return ordered, replaced


def load_overrides(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    allowed = set(EvidenceMetadata.model_fields) | {"_why"}
    for key, fields in data.items():
        unknown = set(fields) - allowed
        if unknown:
            raise SystemExit(f"{path.name} {key}: EvidenceMetadata에 없는 필드 {sorted(unknown)}")
    return data


def build_records(
    rows: list[dict[str, str]],
    papers: dict[str, dict[str, str]],
    overrides: dict[str, dict],
    excerpts: dict[str, dict[Section, str]] | None = None,
) -> tuple[list[Record], list[Issue]]:
    protocols = load_protocols()
    issues: list[Issue] = []
    groups: dict[str, list[tuple[dict[str, str], str]]] = {}
    last_population: dict[str, str] = {}
    early: dict[str, list[tuple[str, str, str]]] = {}  # key → (필드, 사유, 시트 값)
    for row in rows:
        pid, paper = row["protocol_id"].strip(), row["논문 ID"].strip()
        label = row["arm / experiment_id"].strip()
        if paper not in papers:
            raise SystemExit(f"{pid} {paper}: data/manifest.csv에 없는 논문")
        design = DESIGN_LABELS.get(row["논문 유형"].strip())
        if design not in RECORD_TYPE_FOR:
            raise SystemExit(f"{pid} {paper}: 레코드로 옮길 수 없는 논문 유형 {row['논문 유형']!r}")
        key = paper
        notes: list[tuple[str, str, str]] = []
        if RECORD_TYPE_FOR[design] is RecordType.ARM:
            m = ARM_ID.match(label)
            if m and m.group(1) == paper:
                key = f"{paper}-E{int(m.group(2))}"
            else:
                key = f"{paper}-E1"
                notes.append(("experiment_id", f"arm 표기에 -E 번호가 없어 {key}로 두었다", label))
            if MERGED_ARMS.search(label):
                notes.append(
                    (
                        "arm_label",
                        "실험 둘이 한 행이다. 원문에서 실험별 결과가 갈리면 나눈다",
                        label,
                    )
                )
        population = row["대상"].strip()
        if population == SAME_AS_ABOVE and pid in last_population:
            population = last_population[pid]
            notes.append(
                ("population", f"대상 '동일'을 앞 행 값 '{population}'으로 풀었다", "동일")
            )
        elif not blank(population):
            last_population[pid] = population
        groups.setdefault(key, []).append((row, population))
        bucket = early.setdefault(key, [])
        bucket.extend(n for n in notes if n not in bucket)

    records: list[Record] = []
    chunk_counter: Counter[tuple[str, str | None, Section]] = Counter()
    for key, members in groups.items():
        row, population_text = members[0]
        pids = list(dict.fromkeys(r["protocol_id"].strip() for r, _ in members))
        paper = row["논문 ID"].strip()
        design = DESIGN_LABELS[row["논문 유형"].strip()]
        record_type = RECORD_TYPE_FOR[design]
        experiment_id = key if record_type is RecordType.ARM else None
        found: list[tuple[str, str, str]] = list(early.get(key, []))

        for other, _ in members[1:]:
            diff = [
                c
                for c in row
                if c not in ("protocol_id", "비고") and row[c].strip() != other[c].strip()
            ]
            if diff:
                found.append(
                    (
                        "content",
                        f"{other['protocol_id']} 행과 열 {diff}이 달라 "
                        f"{row['protocol_id']} 행을 썼다",
                        " | ".join(other[c] for c in diff),
                    )
                )
        manifest_design = StudyDesign(papers[paper]["study_design"])
        if manifest_design is not design:
            found.append(
                ("study_design", f"manifest는 {manifest_design.value}다", row["논문 유형"])
            )

        per_protocol = {
            p: list(dict.fromkeys(study_medium(m) for m in protocols[p].media)) for p in pids
        }
        media: list[Medium] = []
        for ms in per_protocol.values():
            media.extend(m for m in ms if m not in media)
        if not media:
            found.append(("media", f"인용한 프로토콜 {', '.join(pids)}에 매체가 없다", ""))
        elif len({frozenset(ms) for ms in per_protocol.values()}) > 1:
            each = " / ".join(
                f"{p} {'+'.join(m.value for m in ms)}" for p, ms in per_protocol.items()
            )
            found.append(("media", f"프로토콜마다 매체가 달라 합쳤다({each})", ""))

        orders_label = row["순서"].strip()
        orders = parse_orders(orders_label)
        if not orders and not blank(orders_label):
            found.append(
                (
                    "intervention_orders",
                    "순서 라벨이 조건을 가리키지 않아 미상으로 두었다",
                    orders_label,
                )
            )

        population, clinical = derive_population(population_text, record_type)
        if population is None and not blank(population_text):
            found.append(("population", "대상 표기를 분류하지 못했다", population_text))

        verification = VERIFICATION_LABELS[row["확인 수준"].strip()]
        fulltext = FulltextStatus(papers[paper]["fulltext_status"])
        if fulltext is FulltextStatus.MISSING and verification is not Verification.SECONDARY:
            found.append(
                ("verification", "manifest에 원문이 없는데 2차 자료가 아니다", row["확인 수준"])
            )

        results, reasons = derive_results(row["측정 지표"], row["결과"])
        found.extend(("results", reason, row["결과"]) for reason in reasons)

        fields = {
            "record_type": record_type,
            "study_design": design,
            "verification": verification,
            "arm_label": row["arm / experiment_id"],
            "media": media,
            "intervention_orders": orders,
            "population": population,
            "clinical_status": clinical,
            "population_detail": None if blank(population_text) else population_text,
            "results": results,
            **derive_counts(row["N"], record_type),
        }
        override = {k: v for k, v in overrides.get(key, {}).items() if k != "_why"}
        metadata = EvidenceMetadata.model_validate({**fields, **override})

        for field_name, issue, sheet in found:
            if field_name in override:
                continue
            current = {
                "results": results_summary(metadata.results),
                "media": ", ".join(m.value for m in metadata.media),
            }.get(field_name, "")
            issues.append(Issue(key, " ".join(pids), field_name, issue, current, sheet))

        texts, excerpted = apply_excerpts(
            chunk_texts(row, population_text), (excerpts or {}).get(key)
        )
        chunks = []
        for section, content in texts.items():
            slot = (paper, experiment_id, section)
            chunks.append(
                EvidenceRecordIn(
                    paper_id=paper,
                    experiment_id=experiment_id,
                    section=section,
                    chunk_idx=chunk_counter[slot],
                    content=content,
                    metadata=metadata,
                )
            )
            chunk_counter[slot] += 1
        records.append(Record(key, pids, papers[paper]["title"], metadata, chunks, excerpted))

    unknown = set(overrides) - set(groups)
    if unknown:
        raise SystemExit(f"{OVERRIDES.name}: 시트에 없는 키 {sorted(unknown)}")
    return records, issues


# ── 적재 ──────────────────────────────────────────────────────────────────────────────────

PAPER_SQL = """
insert into public.papers
  (id, legacy_36_id, study_design, fulltext_status, title, authors, year, doi, url)
values
  (%(rc_id)s, %(legacy_36_id)s, %(study_design)s, %(fulltext_status)s, %(title)s, %(authors)s,
   %(year)s, %(doi)s, %(url)s)
on conflict (id) do update set
  legacy_36_id = excluded.legacy_36_id, study_design = excluded.study_design,
  fulltext_status = excluded.fulltext_status, title = excluded.title, authors = excluded.authors,
  year = excluded.year, doi = excluded.doi, url = excluded.url
"""
RECORD_SQL = """
insert into public.evidence_records
  (paper_id, experiment_id, section, chunk_idx, content, metadata, embedding, embedding_model)
values (%s, %s, %s, %s, %s, %s, %s, %s)
on conflict (paper_id, experiment_id, section, chunk_idx) do update set
  content = excluded.content, metadata = excluded.metadata,
  embedding = excluded.embedding, embedding_model = excluded.embedding_model
returning (xmax = 0) as inserted
"""


def embed(texts: list[str]) -> tuple[list[list[float]], str]:
    settings = get_settings()
    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY가 없어 임베딩할 수 없다(.env)")
    vectors = embeddings(settings).embed_documents(texts)
    wrong = {len(v) for v in vectors} - {EMBEDDING_DIMS}
    if wrong:
        raise SystemExit(
            f"{settings.embedding_model}의 차원 {wrong}이 vector({EMBEDDING_DIMS})와 다르다"
        )
    return vectors, settings.embedding_model


def load(database_url: str, papers: dict[str, dict[str, str]], records: list[Record]) -> None:
    import psycopg
    from pgvector import Vector
    from pgvector.psycopg import register_vector
    from psycopg.types.json import Jsonb

    chunks = [(r, c) for r in records for c in r.chunks]
    vectors, model = embed([embedding_text(r.title, r.metadata, c.content) for r, c in chunks])
    print(f"임베딩 {len(vectors)}개 ({model})")

    paper_rows = [
        {**row, "year": int(row["year"]) if row["year"] else None}
        | {k: row[k] or None for k in ("legacy_36_id", "authors", "doi", "url")}
        for row in papers.values()
    ]
    inserted: Counter[str] = Counter()
    with psycopg.connect(database_url, prepare_threshold=None) as conn:
        register_vector(conn)
        with conn.cursor() as cur:
            cur.executemany(PAPER_SQL, paper_rows)
            for (_, chunk), vector in zip(chunks, vectors, strict=True):
                cur.execute(
                    RECORD_SQL,
                    (
                        chunk.paper_id,
                        chunk.experiment_id,
                        chunk.section.value,
                        chunk.chunk_idx,
                        chunk.content,
                        Jsonb(chunk.metadata.model_dump(mode="json")),
                        Vector(vector),
                        model,
                    ),
                )
                inserted["새" if cur.fetchone()[0] else "갱신"] += 1
            ours = {(c.paper_id, c.experiment_id, c.section.value, c.chunk_idx) for _, c in chunks}
            cur.execute(
                "select paper_id, experiment_id, section, chunk_idx from public.evidence_records"
            )
            stale = [k for k in cur.fetchall() if k not in ours]
            cur.execute(
                "select (select count(*) from public.papers),"
                " (select count(*) from public.evidence_records),"
                " (select count(*) from public.evidence_records where embedding_model = %s)",
                (model,),
            )
            n_papers, n_records, n_embedded = cur.fetchone()
    print(
        f"적재: papers {len(paper_rows)}편 · evidence_records {len(chunks)}행"
        f"(새 {inserted['새']} · 갱신 {inserted['갱신']})"
    )
    print(f"DB: papers {n_papers} · evidence_records {n_records} · {model} 임베딩 {n_embedded}")
    if stale:
        print(f"주의: 이번 적재에 없는 행 {len(stale)}개가 DB에 있다 — 예: {stale[:3]}")


# ── 실행 ──────────────────────────────────────────────────────────────────────────────────


def write_review(path: Path, issues: list[Issue]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:  # 엑셀이 한글을 읽게 BOM
        writer = csv.writer(f)
        writer.writerow(["키", "프로토콜", "필드", "확인할 것", "현재 값", "시트 값"])
        for i in issues:
            writer.writerow([i.key, i.protocols, i.field, i.issue, i.value, i.sheet])


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("xlsx", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="DB·임베딩 없이 건수와 검토 파일만")
    parser.add_argument(
        "--database-url",
        help="적재할 DB. 공용 Supabase에 실수로 쓰지 않도록 .env 값을 기본으로 쓰지 않는다",
    )
    parser.add_argument("--review", type=Path, default=ROOT / "evidence_review.csv")
    parser.add_argument(
        "--no-excerpts", action="store_true", help="원문 발췌를 쓰지 않고 전부 시트 요약으로"
    )
    args = parser.parse_args()
    if not args.dry_run and not args.database_url:
        parser.error("적재하려면 --database-url이 필요하다(건수만 보려면 --dry-run)")

    papers = {row["rc_id"]: row for row in csv.DictReader(MANIFEST.open(encoding="utf-8"))}
    rows = read_sheet(args.xlsx, "근거 arm", "protocol_id")
    overrides = load_overrides(OVERRIDES)
    excerpts = {} if args.no_excerpts else load_excerpts_file(EXCERPTS)
    records, issues = build_records(rows, papers, overrides, excerpts)
    write_review(args.review, issues)

    chunks = [c for r in records for c in r.chunks]
    kinds = Counter(r.metadata.record_type.value for r in records)
    levels = Counter(r.metadata.verification.value for r in records)
    effects = Counter(x.effect.value for r in records for x in r.metadata.results)
    print(
        f"시트 {len(rows)}행 → 레코드 {len(records)}개"
        f"(같은 arm {len(rows) - len(records)}행 합침) · "
        f"논문 {len({c.paper_id for c in chunks})}편 · chunk {len(chunks)}개"
    )
    print("종류 " + " · ".join(f"{k} {v}" for k, v in kinds.most_common()))
    print("확인 수준 " + " · ".join(f"{k} {v}" for k, v in levels.most_common()))
    print("결과 판정 " + " · ".join(f"{k} {v}" for k, v in effects.most_common()))
    print(f"덮어쓴 레코드 {len(overrides)}개 ({OVERRIDES.relative_to(ROOT)})")
    excerpted = [r for r in records if r.excerpted]
    n_chunks = sum(len(r.excerpted) for r in excerpted)
    stale = sorted(set(excerpts) - {r.key for r in records})
    print(
        f"원문 발췌 적용 레코드 {len(excerpted)}개 · chunk {n_chunks}개"
        f" ({EXCERPTS.relative_to(ROOT)} 키 {len(excerpts)}개)"
        + (f" · 시트에 없는 키 {stale}" if stale else "")
    )
    print(f"검토 항목 {len(issues)}건 → {args.review}")
    if args.dry_run:
        return 0
    load(args.database_url, papers, records)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
