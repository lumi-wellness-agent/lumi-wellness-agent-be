from fastapi import APIRouter, HTTPException, Request
from psycopg_pool import PoolTimeout

router = APIRouter()

# 첫 마이그레이션(supabase/migrations)이 설치하는 확장
REQUIRED_EXTENSIONS = ("vector", "pg_trgm")


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/health/db")
async def health_db(request: Request):
    """DB에 연결되는지, 마이그레이션의 확장(pgvector·pg_trgm)이 설치됐는지 확인한다."""
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="DATABASE_URL이 설정되지 않았습니다.")
    try:
        async with pool.connection(timeout=5) as conn:
            cur = await conn.execute(
                "select extname, extversion from pg_extension where extname = any(%s)",
                (list(REQUIRED_EXTENSIONS),),
            )
            extensions = dict(await cur.fetchall())
    except PoolTimeout as exc:
        raise HTTPException(
            status_code=503,
            detail="DB에 연결하지 못했습니다. DATABASE_URL과 마이그레이션 적용 여부를 확인하세요.",
        ) from exc
    missing = [name for name in REQUIRED_EXTENSIONS if name not in extensions]
    if missing:
        raise HTTPException(
            status_code=503,
            detail=f"확장이 없습니다: {', '.join(missing)}. 마이그레이션을 적용하세요.",
        )
    return {"status": "ok", "extensions": extensions}
