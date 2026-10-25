# data/

## manifest.csv — 논문 82편 매니페스트

`papers` 테이블과 ingestion의 원천이고, 계약 테스트(`tests/schemas/test_manifest.py`)가 활동 노드 JSON ·
fixture의 논문 ID가 실재하는지 여기에 대조한다. 행 하나 = 논문 하나, `rc_id`가 기본 키다.

| 열 | 뜻 | 출처 |
| --- | --- | --- |
| `rc_id` | R/C 마스터 ID(`P01…P39`, `E01…E21`, `N01…N26`). `papers.id` | `LUMI_R_C_논문목록_2026-10-22.xlsx` R_/C_ 시트 |
| `legacy_36_id` | 36편 근거 재분류 시트의 옛 ID. 조인 키가 아니다(36편만 값이 있다) | `LUMI_RAW데이터 링크.xlsx` '34편 근거 재분류' 크로스워크 |
| `study_design` | 논문 유형 6범주(`app/schemas/evidence.py` `StudyDesign`) | 논문목록 '논문 유형' 열 |
| `fulltext_status` | `TEXT` · `IMAGE_PDF`(스캔, OCR 대상) · `ABSTRACT_ONLY` · `MISSING` | PDF 폴더 유무 + 시트 비고('이미지 PDF'). 적재 때 Docling 결과로 확정한다. N12·N14는 `/Font` 객체가 안 보여 스캔일 수 있으니 그때 확인 |
| `pdf_path` | 원문 PDF 경로. `Research_paper/` 폴더(리포 밖) 기준 상대 경로 | `LUMI_R_C_Research_2026-10-22/00_original_PDF/` |
| `year` `authors` `title` `doi` `url` | 서지. `doi`가 없는 논문(P08 · P32)은 `url`만 있다 | 논문목록 |
| `r_codes` `c_codes` | 이 논문이 연결된 RAG 역할(R1–R5) · 콘텐츠 유형(C01–C10). `;`로 구분 | 논문목록 |

같은 번호가 다른 논문인 사례가 있으므로(옛 P08 = Brown 2013 = R/C P28, R/C P08 = 이승훈·현명호 2004)
옛 ID로 들어온 질문은 반드시 `legacy_36_id`로 바꿔 본다.

재생성: 시트가 바뀌면 ingestion 패키지(PR-C, `app/ingestion/collect.py`)의 매니페스트 생성 명령으로 다시 만든다.
그 전까지는 이 파일을 직접 고치고 PR에서 diff로 검토한다.
