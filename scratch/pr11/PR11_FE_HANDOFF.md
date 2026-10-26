# PR-11 진행률 기반 타이머 FE 인계

## 상태

타이머 상태는 `READY`, `RUNNING`, `PAUSED`, `STOPPED`를 사용합니다. 화면의 `phase`는 소개·활동·보고·결과 화면 구분용이며, 결과 데이터에는 `timer.timer_status`를 사용합니다.

## 시간과 진행률

`elapsed_seconds`는 실제 타임스탬프 차이로 계산한 누적 실행 시간이며 일시정지 시간은 제외합니다.

```text
progress_percent = min(100, elapsed_seconds / target_seconds * 100)
```

목표 도달 후에도 자동 종료하지 않습니다. 진행률은 100%에 고정되고 경과 시간은 계속 증가합니다. `target_reached=true`는 한 번만 기록합니다.

## 종료 의미

`normal_time_end`는 목표 시간에 도달한 뒤 종료했는지를 나타내며, `early_stopped`는 목표 도달 전에 사용자가 종료했는지를 나타냅니다. 실제 보행 여부는 `user_report` 자기보고만 의미하며 자동 검증은 실시하지 않습니다.

화면 잠금·백그라운드 상태에서 타이머 콜백이나 알림 실행은 보장하지 않습니다. 복귀 시 현재 시각을 기준으로 다시 계산해야 합니다.

## 테스트

`pr11-runtime.json`의 사용자 시간은 20·50·90분, 개발 테스트는 30·60초입니다. 개발 테스트 기록은 `timer.test_mode=true`로 구분합니다.
