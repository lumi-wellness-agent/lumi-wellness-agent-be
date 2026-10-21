from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """실행 설정. `.env`에서 읽고, 키 목록의 원본은 `.env.example`이다."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Supabase Postgres 연결 문자열. 비어 있으면 DB 없이 뜬다(/health/db는 503)
    database_url: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
