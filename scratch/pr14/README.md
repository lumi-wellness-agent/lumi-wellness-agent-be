# PR-14 mandara3 로컬 실험

원본 `../mandara3.png`를 보존하고, `../floral-mandara-svg.svg`를 직접 불러와 클릭 색칠을 검증하는 별도 실험 결과입니다.

## 결과

- `mandara3.svg`: 이전 PNG 자동 분할 결과 보관본
- `mandara3-regions.svg`: 다운로드 SVG의 449개 닫힌 서브패스를 독립 path로 재구성
- `mandara3-region-review.svg`: 8색을 순환 적용한 검수용 분할 이미지
- `debug.html`: 모든 영역에 서로 다른 임시 색을 적용하고 실제 클릭 target·영역 ID·화면 범위를 기록하는 검수 화면
- `index.html`: `mandara3-regions.svg`의 영역을 클릭 색칠
- 다운로드 SVG 서브패스: 448개를 클릭 영역으로 사용. 첫 번째 전체 외곽 서브패스는 영역에서 제외하고 outline에만 유지했다.
- 외곽선: `#outline` 그룹으로 분리되어 색칠 클릭의 영향을 받지 않음
- `index.html`: 외부 서버·패키지 없이 SVG를 불러오는 로컬 테스트 화면

## 실행 방법 (Windows PowerShell / VS Code)

1. VS Code에서 이 폴더를 엽니다.
2. `index.html`을 마우스 오른쪽 버튼으로 클릭합니다.
3. **Open with Live Server**가 설치되어 있으면 선택합니다. 이 실험 폴더를 서버 루트로 지정합니다.
4. 확장이 없으면 PowerShell에서 이 실험 폴더로 이동한 뒤 아래 명령을 실행합니다.

```powershell
cd "C:\Users\Admin\Desktop\KG AI\coding\my_llm_service\lumi-wellness-agent\lumi-wellness-agent-be\scratch\coloring-review\experiment-mandara3"
python -m http.server 8765
```

브라우저에서 `http://localhost:8765/`를 엽니다. `fetch()`로 SVG를 읽기 때문에 `file:///`로 직접 여는 방식보다 로컬 서버가 안전합니다. 종료는 PowerShell에서 `Ctrl+C`입니다.

## 한계와 수동 보정

다운로드한 SVG는 하나의 검은 `path` 안에 449개 서브패스가 있고 선(stroke)이 없습니다. 테스트 결과는 이를 서브패스별 path로 분리하고 흰색 fill·검은 stroke를 적용합니다. 서브패스가 실제 사용자 색칠 공간과 일치하는지는 원본 제작 방식에 따라 다르며, 일부는 선 조각·겹치는 도형일 수 있습니다. 따라서 449개를 확정된 폐곡선 영역 수로 간주하면 안 됩니다. `mandara3-region-review.svg`에서 색이 같은 영역이 반복되어 보이는 것은 8색 검수 표시이며, 영역 구분은 `data-region-id`를 기준으로 합니다.

정식 사용 전에는 다운로드 출처와 라이선스를 확인하고, 벡터 편집기에서 실제 폐곡선만 남긴 뒤 영역 ID를 부여해야 합니다. 모바일에서는 작은 영역을 확대하여 탭하고, 경계 누락·겹침·색 번짐을 확인해야 합니다.

## 재사용 요소

- 팔레트 8색과 선택 상태 표시
- `data-region-id` 기반 영역 클릭
- `fill` 변경 방식의 재색칠
- 변경 이력 기반 Undo와 Reset
- `pointerdown` 기반 마우스·터치 입력
- 분리된 `#outline` 레이어
