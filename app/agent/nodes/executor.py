"""② Care Session Executor — 계획한 활동의 콘텐츠를 준비하고, 사용자가 실행을 마칠 때까지 기다린다.

- prepare_session: 프로토콜(data/protocols)의 steps · guide · content_spec과 app/content가 준비한
  자산으로 ActivityContent를 만든다. content_spec(대본 · 재생 시점 · 화면 명세)은 그대로 넘겨 FE
  플레이어가 활동마다 읽고, NOTICE 단계가 있으면 단계 모듈(PR-16)의 화면 명세를 함께 싣는다. 생성
  콘텐츠는 UNVERIFIED로 표시한다.
- run_session: interrupt로 멈춘다. FE 플레이어가 프로토콜 1개를 실행하고(선택 NOTICE 단계 포함),
  끝나면 SessionReport로 재개한다. 점수는 여기서 받지 않는다 — measure_post가 잰다.

interrupt가 있는 노드는 재개될 때 처음부터 다시 실행된다. 콘텐츠 준비를 run_session에 두면
재개할 때마다 다시 만들기 때문에 두 노드로 나눴다.
"""

from langgraph.types import interrupt

from app.agent.state import ActivityContent, CareState, ContentStep, SessionReport
from app.content import resolve_activity_assets
from app.protocols import get_protocol, stage_module
from app.schemas.protocol import StepType


def prepare_session(state: CareState) -> dict:
    activity = state["plan"].activity
    protocol = get_protocol(activity.protocol_id)
    if protocol is None:  # check_plan이 막지만 방어한다
        raise ValueError(f"검수되지 않은 프로토콜 {activity.protocol_id}")
    steps = [
        ContentStep(
            order=s.order, type=s.type.value, label=s.label, minutes=s.minutes, optional=s.optional
        )
        for s in protocol.steps
    ]
    activity_steps = [s for s in steps if s.type == StepType.ACTIVITY.value]
    if len(activity_steps) == 1:  # ACTIVITY 하나면 계획한 시간으로 맞춘다
        activity_steps[0].minutes = activity.minutes
    notice = next((s.label for s in protocol.steps if s.type is StepType.NOTICE), None)
    resolution = resolve_activity_assets(protocol, state["request"])
    content = ActivityContent(
        protocol_id=protocol.protocol_id,
        name=protocol.name,
        steps=steps,
        assets=resolution.assets,
        asset_warnings=resolution.warnings,
        image_urls=[a.url for a in resolution.assets if a.slot in {"VISUAL_STILL", "ARTWORK"}],
        guide_text=protocol.guide.intro_text or "",
        notice_prompt=notice,
        content_spec=protocol.content_spec.model_dump(mode="json"),
        notice_module=_notice_module() if notice else None,
    )
    return {"content": content}


def _notice_module() -> dict | None:
    module = stage_module()
    return (module.content_spec.model_extra or {}).get("session_ui") if module else None


def run_session(state: CareState) -> dict:
    value = interrupt(
        {
            "plan": state["plan"].model_dump(mode="json"),
            "content": state["content"].model_dump(mode="json"),
        }
    )
    return {"report": SessionReport.model_validate(value)}
