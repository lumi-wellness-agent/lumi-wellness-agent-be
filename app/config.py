from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """실행 설정. `.env`에서 읽고, 키 목록의 원본은 `.env.example`이다."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Supabase Postgres 연결 문자열. 비어 있으면 DB 없이 뜬다(/health/db는 503)
    database_url: str = ""
    # 1순위 공급자 OpenAI. 키가 비어 있으면 등록하지 않는다(app/agent/assembly.py)
    openai_api_key: str = ""
    # 모델은 하나로 통일한다 — intake(구조화 출력 1회) · plan(Tool 루프) · 근거 추출(ingestion)
    llm_model: str = "gpt-5.4-mini"
    # 근거 임베딩(같은 OpenAI 키). 행의 embedding_model에 남고 검색은 이 값과 같은 행만 본다 —
    # 바꾸면 근거를 다시 임베딩해야 하고, 차원은 마이그레이션의 vector(1536)과 맞아야 한다
    embedding_model: str = "text-embedding-3-small"
    # 폴백 공급자 Anthropic. 키가 비어 있으면 폴백 없이 OpenAI만. 규칙은 app/agent/llm.py
    anthropic_api_key: str = ""
    fallback_model: str = "claude-haiku-5-5"

    # A 담당 콘텐츠 공급자. 키가 비어 있으면 해당 공급자를 호출하지 않는다.
    pixabay_api_key: str = ""
    freesound_api_key: str = ""
    ai_horde_api_key: str = ""
    # 콘텐츠 검색은 짧게 실패시키고, AI Horde 생성 폴링만 별도 상한을 둔다.
    content_request_timeout_seconds: float = 10.0
    ai_horde_poll_timeout_seconds: float = 60.0

    # 세션 API를 부를 FE 오리진(쉼표로 구분) — 로컬 개발 서버와 운영 FE
    cors_origins: str = "http://localhost:3000,https://lumi-wellness-agent-fe.vercel.app"
    # FE 프리뷰 배포(브랜치마다 주소가 바뀐다). 비우면 프리뷰를 허용하지 않는다
    cors_origin_regex: str = (
        r"https://lumi-wellness-agent-fe-[a-z0-9-]+-koothemavericks-projects\.vercel\.app"
    )

    # Supabase 프로젝트 주소(https://<ref>.supabase.co). 세션 API가 로그인 토큰을 이 프로젝트의
    # 공개 키(JWKS)로 검증한다(app/auth.py). 비우면 세션 API가 503 — 인증 없이 열리지 않는다
    supabase_url: str = ""
    # 원문(처음 입력 · 직접 쓴 답 · 활동 메모) 보관 기한(일). 원문 동의를 한 사용자만 저장하고,
    # 기한이 지나면 정리 작업이 지운다(app/records.py)
    text_retention_days: int = Field(default=90, gt=0)
    # 게스트(익명 로그인 체험)를 이만큼(일) 쓰지 않으면 정리 작업이 사용자째 지운다. 체험 내용은
    # 기록하지 않고, 주인 · 진행만 담은 세션 행과 남은 체크포인트가 이때까지 남는다
    guest_retention_days: int = Field(default=1, gt=0)
    # 이만큼 요청이 없던 미완료 세션은 정리 작업이 체크포인트(원문이 들어 있다)를 지우고 만료로
    # 남긴다
    session_idle_hours: int = Field(default=24, gt=0)
    # Vercel Cron이 정리 작업(GET /internal/cleanup)을 부를 때 보내는 값(Authorization: Bearer).
    # 비우면 정리 엔드포인트가 503
    cron_secret: str = ""

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
