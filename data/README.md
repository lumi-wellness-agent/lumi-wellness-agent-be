# data/

## protocols/ — 활동 노드(Care Protocol) 21개

`PR-*.json` 하나가 활동 노드 하나다(파일명 = `protocol_id`). `app/protocols.py`가 시작할 때 읽고,
`get_care_protocols` Tool · `prepare` · `check_plan`이 쓴다. Protocol DB 테이블은 두지 않는다.

- 스키마는 `app/schemas/protocol.py`의 `CareProtocol`. 코드가 읽는 필드(id · 목표 · 매체 · 구성 · 시간 ·
  순서 조건 · steps · content_spec.slots · measures · evidence · grades)만 형을 고정하고 나머지 블록은 자유다.
- `PR-01.json`은 손으로 쓴 완성 예시. 나머지 20개는 `scripts/scaffold_protocols.py`가 워크북(프로토콜 ·
  근거 arm 시트)에서 만든 **초안**이다 — `review.status = DRAFT`, 확인할 것은 `review.open_items`.
- `evidence[].evidence_record_ids`는 적재 뒤 BE가 역기입하는 유일한 필드다.
- 합격 기준: `make test-protocols`.

## instruments/ — 측정 도구 3개

`INS-SAM` · `INS-STAI6` · `INS-BSRI`. 문항 · 응답 척도 · 채점 규칙(`app/instruments.py`). SAM은 READY,
STAI-6와 BSRI는 저작권·한국어판 확인 뒤 문항 원문을 채운다(`status: TODO`). LLM 번역 금지.

## evidence_overrides.json — 사람이 확정한 근거 메타데이터

`scripts/seed_evidence.py`(`make seed-evidence`)가 근거 arm 시트에서 정한 메타데이터를 필드 단위로
덮어쓴다. 키는 `experiment_id`(arm이 없는 메타·관찰 레코드는 `paper_id`), 값은 `EvidenceMetadata`의 필드와
근거를 적는 `_why`다. 시트의 결과 열은 자유 텍스트라 적재 스크립트가 방향을 못 정한 결과(UNCLEAR)나 시트와
원문이 다른 값이 생긴다 — 검토 목록(`evidence_review.csv`)에서 골라 원문으로 확정한 값을 여기에 적고 적재를
다시 돌린다. 시트에 없는 키나 `EvidenceMetadata`에 없는 필드가 있으면 적재가 멈춘다.

## evidence_excerpts.json — 레코드별 원문 발췌

`scripts/extract_excerpts.py`(`make extract-excerpts`)가 만든다. 키는 `evidence_overrides.json`과 같고
(`experiment_id`, 메타·관찰은 `paper_id`), 값은 `METHODS` · `RESULTS`마다 `{text, page, coverage, snapped,
dropped}`와 `_source` · `_confidence` · `_note`다. `text`는 Docling으로 파싱한 논문 본문에 실제로 있는 문장만
담는다 — LLM이 고른 발췌를 문장 단위로 본문과 대조해, 없는 문장은 가장 가까운 본문 문장으로 바꾸거나
버린다(`coverage`는 남은 비율). `scripts/seed_evidence.py`가 이 `text`를 `evidence_records.content`로 쓰고,
키나 섹션이 없으면 시트 요약을 쓴다. 논문 전문 캐시는 저작권 때문에 Research_paper(비공개) 저장소의
`LUMI_R_C_Research_2026-10-22/01_fulltext/`에 두고 여기에는 짧은 인용만 둔다. 검토 목록은
`evidence_excerpts_review.csv`(gitignore)다.

스캔 PDF(P05 · P26)는 OCR 텍스트에서 고른 초안을 사람이 쪽 이미지와 대조해 교정했다. 이런 항목에는
`_verified`(대조한 쪽)와 `_corrections`(고친 것)가 있고, `extract_excerpts.py`는 `--force`로도 덮어쓰지 않는다
(`--include-verified`일 때만 다시 고른다). OCR 초안은 숫자 아래첨자 · 소수점을 놓치고, 실험이 여럿인 논문에서
모델이 다른 실험의 문단을 가져오기도 해서(P05-E2) 대조 없이 쓰지 않는다.

## manifest.csv — 논문 83편 매니페스트

`papers` 테이블과 ingestion의 원천이고, 계약 테스트(`tests/schemas/test_manifest.py`)가 활동 노드 JSON ·
측정 도구 JSON · fixture의 논문 ID가 실재하는지 여기에 대조한다. 행 하나 = 논문 하나, `rc_id`가 기본 키다.

| 열 | 뜻 | 출처 |
| --- | --- | --- |
| `rc_id` | R/C 마스터 ID(`P01…P39`, `E01…E21`, `N01…N27`). `papers.id` | `LUMI_R_C_논문목록_2026-10-22.xlsx` R_/C_ 시트 |
| `legacy_36_id` | 36편 근거 재분류 시트의 옛 ID. 조인 키가 아니다(36편만 값이 있다) | `LUMI_RAW데이터 링크.xlsx` '34편 근거 재분류' 크로스워크 |
| `study_design` | 논문 유형 6범주(`app/schemas/evidence.py` `StudyDesign`) | 논문목록 '논문 유형' 열 |
| `fulltext_status` | `TEXT` · `IMAGE_PDF`(스캔, OCR 대상) · `ABSTRACT_ONLY` · `MISSING` | PDF 폴더 유무 + 시트 비고('이미지 PDF'). 적재 때 Docling 결과로 확정한다. N12·N14는 `/Font` 객체가 안 보여 스캔일 수 있으니 그때 확인 |
| `pdf_path` | 원문 PDF 경로. `Research_paper/` 폴더(리포 밖) 기준 상대 경로 | `LUMI_R_C_Research_2026-10-22/00_original_PDF/` |
| `year` `authors` `title` `doi` `url` | 서지. `doi`가 없는 논문(P08 · P32)은 `url`만 있다 | 논문목록 |
| `r_codes` `c_codes` | 이 논문이 연결된 RAG 역할(R1–R5) · 콘텐츠 유형(C01–C10). `;`로 구분 | 논문목록 |

같은 번호가 다른 논문인 사례가 있으므로(옛 P08 = Brown 2013 = R/C P28, R/C P08 = 이승훈·현명호 2004)
옛 ID로 들어온 질문은 반드시 `legacy_36_id`로 바꿔 본다.

재생성: 시트가 바뀌면 ingestion 패키지(`app/ingestion/collect.py`)의 매니페스트 생성 명령으로 다시 만든다.
그 전까지는 이 파일을 직접 고치고 PR에서 diff로 검토한다.
