# PR-11 실행 계약 및 FE 인계

## 확정 범위

PR-11은 기본 50분의 야외 자연 보행 활동이다. 20분과 90분은 근거가 서로 다른 참고 옵션으로 기록하지만 현재 UI에는 노출하지 않는다. 연구 효과나 보행 수행을 자동으로 판정하지 않으며, 결과는 사전 확인·완료 또는 중단 상태·타이머 기록으로만 구성한다.

시작 전에는 `walking_environment`와 `body_condition` 두 체크박스를 모두 확인해야 한다. 두 값이 모두 `true`일 때만 시작 버튼을 활성화한다. 체크는 안전을 판정하거나 보증하는 기능이 아니라 사용자의 자기확인 기록이다. 새 세션에서는 두 값을 모두 `false`로 시작하고 이전 세션의 체크 상태를 복원하지 않는다.

## 사용자 실행 흐름

사용자는 시작 화면에서 두 체크박스를 직접 확인한다. 두 값이 모두 `true`가 되면 시작 버튼이 활성화되고, 고정 시작 문구를 본 뒤 타이머가 시작된다. 활동 중에는 경과 시간·진행률·남은 시간을 표시하고 “화면을 계속 바라보기보다 타이머를 가끔 확인하며 주변의 자연물을 보세요.”라는 한 문장만 제공한다. 목표 시간 전에는 버튼을 `중단`으로 표시하고, 목표 시간에 도달하면 같은 버튼을 `완료`로 변경한다. 목표 도달 후에도 자동 종료하지 않으며, 사용자는 계속 활동할 수 있다. 목표 전 `중단`을 누르면 `안전` 또는 `불편`을 선택하고 확인한 뒤 즉시 종료한다. 일시정지·새로고침 복구 상태에서는 작은 팝업으로 `재개`를 누르기 전까지 `PAUSED` 상태를 유지한다. 종료 후 결과 화면에는 목표 달성 여부와 `다시 시작` 버튼만 표시한다.

시작 문구는 “권장 시간 동안 편안한 속도로 걸으며 주변의 자연물을 바라보고, 불편하거나 위험하다고 느끼면 무리하지 말고 중단하세요.”로 고정한다. 종료 문구는 “활동을 마쳤다면 `완료`를 눌러주세요.”로 고정한다. 결과 화면에는 “이 활동은 실제 보행 여부나 개인별 효과를 자동으로 확인하거나 검증하지 않습니다.”를 표시한다.

현재 버전에서는 활동 후 자기보고 문항을 제공하지 않는다. 따라서 `완료`는 사용자가 완료 버튼을 눌렀다는 사실만 의미하며 실제 보행이나 효과의 확인을 의미하지 않는다.

모든 사용자 문구는 `PR-11.proposed.json`의 `content_spec.slots`에서 읽는다. FE는 `phase`에 맞는 `slot`의 `text`를 출력하며, 문구를 코드에 하드코딩하거나 임의로 재작성하지 않는다. 필수 슬롯은 `INTRO_GUIDE`, `DURING_ACTIVITY_GUIDE`, `TARGET_REACHED_GUIDE`, `STOP_CONFIRMATION`, `LIMITATION_NOTICE`다.

슬롯의 `text`가 없거나 비어 있으면 같은 슬롯의 `fallback_text`를 출력한다. 문구 버전은 관리하지 않는다. TTS는 시작 버튼 후 `INTRO_GUIDE`를 한 번, 진행률이 처음 100%가 되는 순간 `TARGET_REACHED_GUIDE`를 한 번만 재생하며, 일시정지·재개·복구 시 반복하지 않는다. `DURING_ACTIVITY_GUIDE`는 선택 슬롯이며 두 문구가 없으면 안내 영역을 숨기고 활동을 계속한다.

정상 완료 결과는 `pr11-result.completed.example.json`, 안전·불편 중단 결과는 `pr11-result.example.json`을 참고한다.

## 상태

타이머 상태는 `READY`, `RUNNING`, `PAUSED`, `STOPPED`를 사용합니다. 화면의 `phase`는 소개·활동·보고·결과 화면 구분용이며, 결과 데이터에는 `timer.timer_status`를 사용합니다.

## 시간과 진행률

`elapsed_seconds`는 실제 타임스탬프 차이로 계산한 누적 실행 시간이며 일시정지 시간은 제외합니다.

```text
progress_percent = min(100, elapsed_seconds / target_seconds * 100)
```

목표 도달 후에도 자동 종료하지 않습니다. 진행률은 100%에 고정되고 경과 시간은 계속 증가합니다. `target_reached=true`는 한 번만 기록합니다.

## 종료 의미

`normal_time_end`는 목표 시간에 도달한 뒤 안전하게 종료했는지를 나타냅니다. 일반적인 목표 전 종료는 허용하지 않으며, 안전 또는 불편 중단만 허용합니다. 목표 전에 안전·불편 중단을 하면 `early_stopped=true`와 함께 `stop_reason`에 `SAFETY` 또는 `DISCOMFORT`를 기록합니다. 목표 도달 후 종료하면 `early_stopped=false`입니다. 실제 보행 여부와 개인별 효과는 자동 검증하지 않습니다.

화면 잠금·백그라운드 상태에서 타이머 콜백이나 알림 실행은 보장하지 않습니다. 복귀 시 현재 시각을 기준으로 다시 계산해야 합니다.

## 테스트

`pr11-runtime.json`에는 20·50·90분의 근거 메타데이터가 있으나 현재 실행 선택은 50분으로 고정합니다. 개발 테스트는 30·60초이며, 개발 테스트 기록은 `timer.test_mode=true`로 구분하고 프로토콜 완료로 집계하지 않습니다.

## 결과 필드 제약

- `protocol_id`는 `PR-11`이다.
- `pre_activity_check.walking_environment`와 `pre_activity_check.body_condition`은 시작 시 모두 `true`여야 한다.
- 두 시작 전 확인 중 하나라도 확인되지 않으면 시작 버튼을 활성화하지 않는다.
- 운영 실행의 `timer.planned_seconds`는 3000이다.
- `timer.timer_status`는 `READY`, `RUNNING`, `PAUSED`, `STOPPED` 중 하나다.
- 일시정지 시간은 `elapsed_seconds`에 포함하지 않으며 `paused_seconds`가 있으면 0 이상이어야 한다.
- `verification.automatic_walking`은 `not_performed`, `verification.gps`는 `disabled`, `verification.self_report_basis`는 `false`다. 현재 버전에서는 활동 후 자기보고를 수집하지 않는다.
- `step_tracking.status`는 `not_measured`이며 걸음 수와 신뢰도는 null이다.
- `stop_reason`은 안전 중단이면 `SAFETY`, 불편 중단이면 `DISCOMFORT`다.
- 결과는 브라우저 임시 저장이며 공식 서버 기록이 아니다.

## 상태 전이 규칙

`READY → RUNNING`으로 시작하고, `RUNNING ↔ PAUSED`는 사용자의 일시정지·재개로만 전환한다. 목표 도달 시 `TARGET_REACHED` 이벤트를 기록하고 경과 시간과 진행률을 고정하며, `완료`만 활성화한다. 목표 전 중단은 확인 뒤 `STOPPED`와 중단을 기록한다. 새로고침 중 `RUNNING`은 `PAUSED`로 복구하며 재개는 사용자 동작을 요구한다. 실행 기록의 `phase_history`는 확인 완료, 시작, 일시정지, 재개, 목표 도달, 완료, 중단 이벤트만 저장한다.

## FE 구현 주의

안전 중단 핸들러는 반드시 `stop_reason`을 기록해야 한다. 일반 조기 종료 버튼은 제공하지 않는다. `paused_seconds`를 제공하는 경우 실제 일시정지 구간의 합으로 계산하며, 추정값을 넣지 않는다.

## 시작 전 체크박스 문구

- `walking_environment`: 현재 보행할 장소와 경로가 충분히 안전합니다.
- `body_condition`: 현재 몸 상태로 이 활동을 진행할 수 있습니다.

두 항목은 의료적 판정이 아니라 사용자의 자기판단이다. 하나라도 아니오이면 활동을 시작하지 않고 나중에 조건을 다시 확인한다.
