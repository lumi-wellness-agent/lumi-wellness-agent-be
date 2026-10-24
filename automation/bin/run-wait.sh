#!/usr/bin/env bash
# Actions 워크플로우 run 대기 — 특정 커밋이 트리거한 run의 완료를 폴링한다 (30초 간격).
# main 머지 커밋의 CI(ci.yml) 대기에 쓴다. 배포 워크플로우가 생기면 같은 스크립트로 기다린다.
#
# run이 한 번도 발견되지 않은 채 타임아웃되면 NOT_FOUND로 구분한다 — 워크플로우 미트리거
# (트리거 조건·파일명 변경 등) 신호이므로, 진행 중일 수 있는 TIMEOUT과 다르게 처리한다.
#
# 사용법: run-wait.sh <워크플로우파일> <브랜치> <커밋SHA> <타임아웃초>
#   예: run-wait.sh ci.yml main "$MERGE_SHA" 600
# 종료 마커: RUN_RESULT result=success|<conclusion>|NOT_FOUND|TIMEOUT|API_ERROR|ERROR
#   완료 시 run_id=<id>가 함께 찍힌다 — 실패면 gh run view <id> --log-failed로 로그를 요약한다.
set -u

if [ $# -ne 4 ]; then
  echo "usage: $0 <워크플로우파일> <브랜치> <커밋SHA> <타임아웃초>" >&2
  exit 2
fi
WORKFLOW=$1
BRANCH=$2
SHA=$3
LIMIT=$4
command -v jq >/dev/null 2>&1 \
  || { echo "jq를 찾을 수 없다 — run 상태 파싱에 필요하다"; echo "RUN_RESULT result=ERROR"; exit 1; }
INTERVAL=30
API_ERRORS=0
SECONDS=0
FOUND=0

while :; do
  # GitHub 조회 실패를 pending으로 삼키지 않는다 — 3회 연속 실패 시 API_ERROR로 종료.
  if RUN=$(gh run list --branch "$BRANCH" --workflow "$WORKFLOW" --commit "$SHA" --limit 1 \
             --json status,conclusion,databaseId --jq '.[0] // empty'); then
    API_ERRORS=0
  else
    API_ERRORS=$((API_ERRORS + 1))
    echo "GitHub API query failed ($API_ERRORS/3)"
    if [ "$API_ERRORS" -ge 3 ]; then echo "RUN_RESULT result=API_ERROR"; exit 1; fi
    sleep "$INTERVAL"; continue
  fi
  if [ -n "$RUN" ]; then
    FOUND=1
    STATUS=$(echo "$RUN" | jq -r '.status // empty')
    if [ "$STATUS" = "completed" ]; then
      CONCLUSION=$(echo "$RUN" | jq -r '.conclusion // empty')
      RUN_ID=$(echo "$RUN" | jq -r '.databaseId // empty')
      if [ "$CONCLUSION" = "success" ]; then
        echo "RUN_RESULT result=success run_id=$RUN_ID"
        exit 0
      fi
      echo "RUN_RESULT result=${CONCLUSION:-unknown} run_id=$RUN_ID"
      exit 1
    fi
  fi
  if [ "$SECONDS" -ge "$LIMIT" ]; then
    if [ "$FOUND" -eq 0 ]; then
      echo "RUN_RESULT result=NOT_FOUND"
    else
      echo "RUN_RESULT result=TIMEOUT"
    fi
    exit 1
  fi
  sleep "$INTERVAL"
done
