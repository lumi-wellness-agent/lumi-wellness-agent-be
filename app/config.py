from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """실행 설정. `.env`에서 읽고, 키 목록의 원본은 `.env.example`이다."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Supabase Postgres 연결 문자열. 비어 있으면 DB 없이 뜬다(/health/db는 503)
    database_url: str = ""
    # 1순위 공급자 OpenAI. 키가 비어 있으면 등록하지 않는다(app/agent/assembly.py)
    openai_api_key: str = ""
    # 모델은 하나로 통일한다 — intake(구조화 출력 1회) · plan(Tool 루프) · 근거 추출(ingestion)
    llm_model: str = "gpt-4o-mini"
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
