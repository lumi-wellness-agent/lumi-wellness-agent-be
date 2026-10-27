"""② 개념 정의 검색 — intake 목표 추론의 근거(evidence_records의 record_type = DEFINITION).

hybrid.search를 record_type = DEFINITION으로만 부른다(매체 · 순서 · 지표 필터 없음). 검색어는 ① 상황
읽기가 만든 영어 질의다(사용자 문장은 한국어, 원문은 영어 — 키워드 검색이 영어 낱말로 걸린다).
어느 목표의 정의인지는 개념 카드의 paper_id로 붙인다(app/constructs.goal_for_paper).

DB 풀이 없거나 0건이면 빈 목록이다. 그래도 목표 추론은 카드(정의 · 단서 · 경계)만으로 돈다 —
그때 goal_evidence_ids는 비고, 추천 이유는 근거 ID 없이 카드 출처만 말할 수 있다.
"""

from psycopg_pool import AsyncConnectionPool

from app.agent.intake_rules import DefinitionHit
from app.constructs import goal_for_paper
from app.retrieval import hybrid
from app.schemas.evidence import RecordType

TOP_K = 6  # 목표 3개 × 출처 2~3개라 전부 볼 수 있을 만큼


async def find_definitions(
    pool: AsyncConnectionPool | None, query: str, *, limit: int = TOP_K
) -> list[DefinitionHit]:
    if not query.strip():
        return []
    hits = await hybrid.search(pool, query=query, record_type=RecordType.DEFINITION, limit=limit)
    return [
        DefinitionHit(
            evidence_record_id=hit.evidence_record_id,
            paper_id=hit.paper.paper_id,
            goal=goal_for_paper(hit.paper.paper_id),
            authors=hit.paper.authors,
            year=hit.paper.year,
            excerpt=hit.excerpt,
        )
        for hit in hits
    ]
