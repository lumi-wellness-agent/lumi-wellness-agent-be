"""근거 임베딩 — 설정의 embedding_model(OpenAI, OPENAI_API_KEY)로 질의와 chunk를 벡터로 바꾼다.

벡터 거리는 같은 모델·같은 입력 처리로 만든 것끼리만 뜻이 있어서, 적재(scripts/seed_evidence.py)와
검색(hybrid.search)이 이 모듈의 클라이언트를 함께 쓴다. 행의 embedding_model에 모델 이름이 남고
검색은 설정과 같은 모델의 행만 본다. 테스트는 embed_query를 가짜로 바꿔 끼운다.
"""

from langchain_core.embeddings import Embeddings

from app.config import Settings, get_settings

EMBEDDING_DIMS = 1536  # 마이그레이션의 vector(1536)
# 질의 하나는 보통 1초 안에 온다. 장애 때 서버리스 함수를 오래 붙잡지 않게 짧게 끊는다
QUERY_TIMEOUT_SECONDS = 5.0
QUERY_MAX_RETRIES = 1


def embeddings(
    settings: Settings, *, timeout: float | None = None, max_retries: int = 2
) -> Embeddings:
    from langchain_openai import OpenAIEmbeddings  # 쓸 때만 읽는다(테스트는 가짜를 쓴다)

    return OpenAIEmbeddings(
        model=settings.embedding_model,
        api_key=settings.openai_api_key,
        # tiktoken으로 잘라 보내지 않는다. chunk·질의가 짧고, 서버가 인코딩 파일을 내려받지 않는다
        check_embedding_ctx_length=False,
        timeout=timeout,
        max_retries=max_retries,
    )


async def embed_query(text: str) -> list[float] | None:
    """질의 벡터. OPENAI_API_KEY가 없으면 None이다(검색은 키워드로만 돈다)."""
    settings = get_settings()
    if not settings.openai_api_key:
        return None
    client = embeddings(settings, timeout=QUERY_TIMEOUT_SECONDS, max_retries=QUERY_MAX_RETRIES)
    return await client.aembed_query(text)
