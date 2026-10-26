"""근거 추출 — 파싱한 섹션을 실험 단위 Evidence Record로 나누고 `EvidenceMetadata`를 붙인다.

- 분할: 논문 섹션별로 나누고, 같은 실험의 방법·결과는 experiment_id로 묶는다.
  한 논문의 서로 다른 실험(예: Berman 2008의 보행 실험과 사진 실험)을 하나로 합치지 않는다.
- 메타데이터: LLM 구조화 출력으로 `EvidenceMetadata`(schemas/evidence.py)를 채운다.
- 본문: 요약하지 않고 원문 구간을 그대로 저장해 Reference 카드에서 출처를 확인할 수 있게 한다.
- 임베딩: 설정의 embedding_model(기본 text-embedding-3-small, 1536차원 = 마이그레이션의
  vector(1536))로 만들고, 모델 이름을 행의 embedding_model에 같이 남긴다.

키·모델은 `app/config.py`에 있다 — OPENAI_API_KEY 하나로 LLM_MODEL · EMBEDDING_MODEL을 모두 부른다.
"""
