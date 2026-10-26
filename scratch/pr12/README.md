# PR-12 야외 자연 보행 + 알아차림 웹 프로토타입

PR-12는 PR-11의 시작 확인·문구 fallback·TTS 오류 처리·목표 도달 정책만 공유하고, 30분 보행 후 알아차림 기록과 자기보고 단계는 별도로 유지합니다.

원본 `data/protocols/PR-12.json`의 기본 시간 30분과 야외 활동 후 알아차림 입력을 반영한 로컬 MVP입니다. GPS·센서·서버 저장은 사용하지 않습니다.

## 실행

PowerShell에서 이 폴더로 이동한 뒤 다음을 실행합니다.

```powershell
py -m http.server 8767
```

브라우저에서 `http://localhost:8767/index.html`을 엽니다. `file://` 직접 열기 대신 HTTP 서버를 사용하세요.

## 데이터와 복구

진행 상태와 결과는 브라우저 `localStorage`의 `lumi.pr11.outdoor.v1` 키에 저장됩니다. 새로고침 후 진행 상태가 복구되며, 서버의 공식 기록으로 간주하지 않습니다. 결과 화면의 JSON은 FE 연동용 형태를 확인하기 위한 것입니다.

타이머는 `Date.now()` 차이를 이용하므로 백그라운드 전환·화면 잠금 뒤 복귀 시에도 interval 호출 횟수에 의존하지 않습니다. 일시정지 중에는 경과 시간이 증가하지 않습니다.

## 향후 만보기 확장 지점

결과 JSON의 `estimated_steps: null`, `automatic_walking_verification: "not_performed"` 필드와 타이머 상태 객체를 유지해 센서 지원 여부, 권한, 추정 걸음 수, 신뢰도 필드를 추가할 수 있습니다. 현재는 센서 권한을 요청하지 않습니다.
