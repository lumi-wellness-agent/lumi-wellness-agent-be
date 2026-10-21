"""원문 파싱 — 논문을 섹션 단위 텍스트로 바꾼다. PDF는 Docling, Europe PMC 전문은 JATS XML로 읽는다.

JATS XML은 루트가 `<article>`이고 `body/sec`가 섹션이다. 섹션마다 `title`이 있다
(예: 1. Introduction, 2. Method, 3. Results, 4. Discussion). 실험 조건과 결과는
Method·Results와 표·그림 캡션에 흩어져 있어 함께 뽑는다.
"""
