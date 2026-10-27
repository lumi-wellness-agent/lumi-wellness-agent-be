# PR-14 만다라 컬러링 프로토타입

## SVG 사용 정책

이 프로토타입은 PNG·JPG 이미지를 실행 중 SVG로 변환하지 않습니다. 클릭 색칠에 필요한 독립 영역과 `data-region-id`가 포함된 SVG를 사전에 준비하고, `pr14-runtime.json`의 `designs[].svg` 경로에서 직접 읽습니다.

이미지→SVG 변환 스크립트와 과거 중간 산출물은 참고용으로만 남아 있으며 현재 서비스 실행 경로에서 호출되지 않습니다.

## 확정 프로세스

1. 첫 도안을 자동으로 표시합니다.
2. 흰색 독립 영역을 처음 클릭할 때 타이머가 시작됩니다. 팔레트 선택, 배경, 검은 외곽선 클릭은 시작 조건이 아닙니다.
3. 독립 영역은 `UNCOLORED` 또는 `COLORED` 상태로 관리합니다.
4. 현재 도안의 모든 영역을 한 번 이상 색칠하면 도안 완성으로 기록합니다.
5. 도안 완성 후 `다음 도안`을 누르면 저장된 도안 중 하나를 랜덤으로 표시합니다. 새 도안은 흰색·`UNCOLORED` 상태로 시작합니다.
6. 완성 도안이 1개 이상이고 16분(20분의 80%) 이상 경과하면 `완료` 버튼이 활성화됩니다.
7. 20분 도달 후에도 진행률은 100%로 유지하며 자동 종료하지 않습니다.

## 상태·복구 규칙

- 일시정지는 제공하지 않습니다.
- 새로고침 시 타이머는 정지하고 색칠 상태는 `localStorage`에서 복구합니다.
- 새로고침 후 다시 유효 영역을 클릭하면 타이머가 재시작됩니다.
- 재시작 횟수와 이벤트는 개발 로그에만 보관합니다.
- `Reset`은 현재 도안의 모든 영역을 흰색·`UNCOLORED`로 되돌립니다.
- `Undo`는 직전 색상을 복원합니다. 이전 색상이 흰색이면 `UNCOLORED`, 팔레트 색상이면 `COLORED`로 해석합니다.
- 결과는 브라우저 저장용이며 공식 서버 기록이 아닙니다.

## 파일

- `index.html`: JSON 기반 UI와 컬러링 실행 화면
- `PR-14.proposed.json`: 활동 정의와 완료 기준
- `pr14-runtime.json`: 타이머·팔레트·도안·복구 설정
- `pr14-result.example.json`: 결과 JSON 예시
- `assets/white-space-regions.svg`: 441개 독립 색칠 영역과 검은 외곽선이 포함된 실행용 SVG
- `browser-integration-check.html`, `outline-integration-check.html`, `debug.html`: 검증용 화면

## 실행 방법 (Windows PowerShell)

```powershell
cd "...\lumi-wellness-agent-be\scratch\pr14"
py -m http.server 8765
```

브라우저에서 `http://localhost:8765/`를 엽니다. `fetch()`로 JSON과 SVG를 읽으므로 `file://` 직접 실행 대신 HTTP 서버를 사용해야 합니다.

실험용 도안 3개는 현재 동일한 검증 SVG를 재사용합니다. 실제 서로 다른 도안을 제공할 때 `pr14-runtime.json`의 `designs[].svg` 경로만 교체하면 됩니다. 각 SVG에는 독립 영역별 `data-region-id`와 최상단 외곽선이 있어야 합니다.

## 새 SVG를 추가하는 방법

1. 준비된 SVG 파일을 `scratch/pr14/assets/`에 저장합니다.
2. SVG 내부에 클릭 영역별 `data-region-id`가 있는지 확인합니다.
3. 검은 외곽선에는 `id="outline"`을 부여하고 색칠 영역과 분리합니다.
4. `pr14-runtime.json`의 `designs` 배열에 파일 경로를 추가합니다.

예:

```json
{
  "id": "mandala-04",
  "name": "만다라 도안 4",
  "svg": "assets/mandala-04.svg"
}
```

서비스가 사용하는 전처리 기준은 SVG 파일 자체에 보관합니다. 별도의 PNG 원본이나 변환 스크립트는 실행에 필요하지 않습니다.
