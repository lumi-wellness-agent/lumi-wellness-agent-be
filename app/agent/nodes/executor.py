"""② Care Session Executor — 계획한 활동의 콘텐츠를 준비하고, 사용자가 실행을 마칠 때까지 기다린다.

- prepare_session: 활동마다 콘텐츠를 만든다. 기존 자연 사진이 우선이고 AI 생성 이미지는 보조다
  (app/content/image.py). 안내 문구·음성은 app/content/guide.py, Reference 카드는
  app/agent/reference_card.py가 만든다.
- run_session: interrupt로 멈춘다. FE 플레이어가 mode에 맞춰 단일 · 동시(병행) · 순차(차례로)로
  실행하고, 끝나면 사후 측정을 SessionReport로 보내 재개한다.

interrupt가 있는 노드는 재개될 때 처음부터 다시 실행된다. 콘텐츠 생성을 run_session에 두면
재개할 때마다 다시 만들기 때문에 두 노드로 나눴다.
"""

from langgraph.types import interrupt

from app.agent.state import ActivityContent, CareState, SessionReport


def prepare_session(state: CareState) -> dict:
    # TODO(Executor): app/content로 이미지·안내 문구를 만든다. 지금은 빈 콘텐츠다
    contents = [
        ActivityContent(protocol_id=activity.protocol_id)
        for activity in state["plan"].activities
    ]
    return {"contents": contents}


def run_session(state: CareState) -> dict:
    value = interrupt(
        {
            "plan": state["plan"].model_dump(mode="json"),
            "contents": [content.model_dump(mode="json") for content in state["contents"]],
        }
    )
    return {"report": SessionReport.model_validate(value)}
