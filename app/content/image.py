"""AI 이미지 생성·검증 — 선호 색상·풍경으로 자연 이미지를 만들고, 쓰기 전에 검사한다.

기존 자연 사진 추천이 우선이고, AI 생성 이미지는 보조 콘텐츠다(효과 검증 필요).
검증은 VLM, 이미지 임베딩, 규칙 기반 검사로 한다. 이미지 생성 API는 아직 정하지 않았다.
② Care Session Executor의 prepare_session(app/agent/nodes/executor.py)이 부른다.
"""
