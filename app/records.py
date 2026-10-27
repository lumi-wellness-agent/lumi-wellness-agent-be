"""세션 기록 — 동의(user_consents) · 세션(care_sessions) · 라운드(care_rounds) · 원문(care_texts).

세션 API가 요청마다 그래프가 멈춘 뒤의 상태에서 행을 다시 쓴다(save). 상태가 원본이고 행은 그
사본이라 같은 상태로 두 번 써도 결과가 같다. 테이블은
supabase/migrations/20261027000000_care_records.sql.

원문 방침
- 분석 테이블(care_sessions · care_rounds)에는 분류 값 · 칩 답 · 측정값만 둔다. 사용자가 쓴
  글(처음 입력 · 직접 쓴 답 · PR-16 감정 단어 · 멈춘 이유)과 그 글에서 나온 글(intake의 사건 ·
  표현 · 요약 · 판단 이유, 추천 이유, 질문 문장)은 원문 동의를 한 사용자만 care_texts에 보관 기한을
  두고 남긴다. IntakeResult에 칸이 생기면 INTAKE_FIELDS나 INTAKE_TEXT_FIELDS 한쪽에 넣는다
  (tests/test_records.py).
- SAFETY로 끝난 세션은 끝난 사실만 남긴다 — 칩 · 분류 값 · 원문을 지운다.
- 끝난 세션의 체크포인트에도 원문이 있어 세션 API가 끝난 즉시 지운다. 끝나지 않고 쉬는 세션과 기록이
  없는 체크포인트, 기한이 지난 원문은 정리 작업(cleanup, Vercel Cron)이 지운다.
- 행은 모두 auth.users를 따라 지워진다. 탈퇴(delete_user, DELETE /me)는 auth.users 행을 지워 기록 ·
  원문 · 동의와 Supabase의 로그인 세션 · 연동 계정을 함께 지우고, 그 사용자의 체크포인트도 지운다.
  대시보드에서 사용자를 지운 경우 남은 체크포인트는 다음 정리 작업이 지운다.

저장소는 둘이다 — DATABASE_URL이 있으면 PostgresStore, 없으면(테스트 · DB 없는 로컬) MemoryStore.
둘 다 같은 행 함수(session_columns · round_row · text_row)를 쓴다.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from app.agent.state import IntakeResult, Round, SessionEnd
from app.schemas.care import CareRequest
from app.schemas.intake import IntakeTurn

# 동의 문구 버전. 문구(FE)를 바꾸면 올린다 — 이 버전에 동의하지 않은 사용자는 세션을 시작하지 못한다
CONSENT_VERSION = "2026-10-27"

# IntakeResult 중 분석 테이블에 두는 분류 값. goal_evidence는 paper_id만(문장은 정의 파일에 있다)
INTAKE_FIELDS = (
    "goal",
    "secondary_goal",
    "goal_source",
    "target_outcomes",
    "goal_evidence",
    "order_condition",
    "rumination_cue",
    "goal_hint",
    "setting",
    "earphones",
    "environment",
    "minutes",
    "place",
    "missing_slots",
    "end_reason",
)
# 문장에서 읽은 글 — 원문 동의가 있을 때만 care_texts로
INTAKE_TEXT_FIELDS = ("event", "cues", "summary", "goal_reason", "constraints")
# CareRequest 중 분석 테이블에 두는 칩. text와 프로필(접근성 메모 등 글)은 두지 않는다
REQUEST_FIELDS = ("setting", "earphones", "minutes_available", "goal", "place")


@dataclass(frozen=True)
class Consent:
    version: str
    raw_text: bool  # 원문 보관(선택)
    records_agreed_at: datetime  # 세션 기록(필수)에 동의한 시각
    updated_at: datetime

    @property
    def current(self) -> bool:
        """지금 문구 버전에 동의했다 — 세션을 시작할 수 있다."""
        return self.version == CONSENT_VERSION


@dataclass(frozen=True)
class SessionRecord:
    """세션 API가 요청마다 보는 세션 행 — 소유자 확인 · 끝난 세션의 응답."""

    id: str
    user_id: UUID
    end_summary: dict | None = None  # 끝났으면 END payload
    expired: bool = False  # 쉬다가 정리 작업이 체크포인트를 지웠다


# ── 상태 → 행 ─────────────────────────────────────────────────────────────────────────────


def request_row(request: CareRequest) -> dict:
    row = request.model_dump(mode="json", include=set(REQUEST_FIELDS))
    return {**row, "content_type": request.profile.content_type.value}


def intake_row(intake: IntakeResult) -> dict:
    row = intake.model_dump(mode="json", include=set(INTAKE_FIELDS))
    return {**row, "goal_evidence": [e.paper_id for e in intake.goal_evidence]}


def turn_row(turn: IntakeTurn) -> dict:
    return {
        "question_id": turn.question_id,
        "slot": turn.slot.value,
        "answer": turn.answer,
        "skipped": turn.skipped,
        "wrote": turn.free_text is not None,  # 직접 썼다 — 글은 care_texts에
    }


def safety_ended(values: dict) -> bool:
    return values.get("ended") == SessionEnd.SAFETY


def session_columns(values: dict, view: dict) -> dict:
    """care_sessions의 갱신 값. SAFETY로 끝났으면 끝난 사실(step · end_reason)만."""
    ended = view["step"] == "END"
    columns = {
        "step": view["step"],
        "node": view["node"],
        "end_reason": view["payload"]["reason"] if ended else None,
        "end_summary": view["payload"] if ended else None,
        "ended": ended,
    }
    if safety_ended(values):
        return {
            **columns,
            "goal": None,
            "request": {},
            "intake": None,
            "turns": [],
            "pre": None,
            "final": None,
        }
    intake: IntakeResult | None = values.get("intake")
    final = values.get("final")
    return {
        **columns,
        "goal": intake.goal.value if intake and intake.goal else None,
        "request": request_row(values["request"]),
        "intake": intake_row(intake) if intake else None,
        "turns": [turn_row(t) for t in values.get("intake_turns") or []],
        "pre": [m.model_dump(mode="json") for m in values["pre"]] if "pre" in values else None,
        "final": final.model_dump(mode="json") if final else None,
    }


def round_row(number: int, finished: Round) -> dict:
    activity, proposal = finished.plan.activity, finished.proposal
    return {
        "round": number,
        "protocol_id": activity.protocol_id,
        "minutes": activity.minutes,
        "evidence_record_ids": list(activity.evidence_record_ids),
        "relaxed": list(finished.plan.relaxed),
        "environment_gaps": list(finished.plan.environment_gaps),
        "completed": finished.report.completed,
        "pre": [m.model_dump(mode="json") for m in finished.pre],
        "post": [m.model_dump(mode="json") for m in finished.post],
        "deltas": finished.evaluation.deltas,
        "improved": finished.evaluation.improved,
        "recommended": proposal.recommended.value if proposal else None,
        "options": [step.value for step in proposal.options] if proposal else [],
        "next_step": finished.next_step.value,
    }


def round_rows(values: dict) -> list[dict]:
    return [round_row(i, r) for i, r in enumerate(values.get("rounds") or [], start=1)]


def text_row(values: dict) -> dict:
    """care_texts의 값 — 처음 입력, 질문 문장과 직접 쓴 답, 문장에서 읽은 글, 라운드의 글."""
    intake: IntakeResult | None = values.get("intake")
    return {
        "request_text": values["request"].text,
        "answers": [
            {
                "question_id": t.question_id,
                "slot": t.slot.value,
                "question": t.text,
                "free_text": t.free_text,
            }
            for t in values.get("intake_turns") or []
        ],
        "intake": intake.model_dump(mode="json", include=set(INTAKE_TEXT_FIELDS)) if intake else {},
        "rounds": [
            {
                "round": i,
                "rationale": r.plan.rationale,
                "stopped_reason": r.report.stopped_reason,
                "notice_words": list(r.report.notice_words),
            }
            for i, r in enumerate(values.get("rounds") or [], start=1)
        ],
    }


# ── 저장소 ────────────────────────────────────────────────────────────────────────────────


class SessionStore(Protocol):
    retention_days: int

    async def consent(self, user_id: UUID) -> Consent | None: ...

    async def save_consent(self, user_id: UUID, raw_text: bool) -> Consent:
        """지금 버전의 필수 동의와 원문 동의(raw_text)를 남긴다. 거두면 원문도 지운다."""
        ...

    async def create(self, session_id: str, user_id: UUID, request: CareRequest) -> None: ...

    async def session(self, session_id: str) -> SessionRecord | None: ...

    async def save(self, record: SessionRecord, values: dict, view: dict) -> None:
        """그래프가 멈춘 뒤의 상태(values)와 응답(view)으로 행을 다시 쓴다. 원문은 쓰는 순간에
        원문 동의가 있을 때만 — 요청이 도는 사이 동의를 거둬도 다시 쓰지 않는다."""
        ...

    # 정리 작업의 시각은 저장소가 정한다 — Postgres는 DB의 now()라 서버 시계와 어긋나지 않는다

    async def delete_expired_texts(self) -> int: ...

    async def stale_threads(self, idle: timedelta, checkpointer: BaseCheckpointSaver) -> list[str]:
        """지울 체크포인트의 thread — 기록 없음 · 끝남 · 만료, idle보다 오래 쉰 세션."""
        ...

    async def mark_expired(self, session_ids: Iterable[str]) -> int: ...

    async def delete_user(self, user_id: UUID) -> list[str]:
        """탈퇴 — 사용자와 그 행을 모두 지운다. 지운 세션의 ID(체크포인트 thread)를 돌려준다."""
        ...


async def cleanup(
    store: SessionStore, checkpointer: BaseCheckpointSaver, idle: timedelta
) -> dict[str, int]:
    """기한이 지난 원문과 쉬거나 주인이 없는 체크포인트를 지운다. 쉬던 세션은 만료로 남긴다."""
    texts = await store.delete_expired_texts()
    threads = await store.stale_threads(idle, checkpointer)
    for thread_id in threads:
        await checkpointer.adelete_thread(thread_id)
    expired = await store.mark_expired(threads)
    return {"texts": texts, "threads": len(threads), "expired_sessions": expired}


def _uuids(ids: Iterable[str]) -> list[UUID]:
    out = []
    for value in ids:
        try:
            out.append(UUID(value))
        except ValueError:
            continue
    return out


class MemoryStore:
    """프로세스 메모리 — 테스트와 DB 없이 띄운 서버. 행 모양은 PostgresStore와 같다."""

    def __init__(self, retention_days: int = 90, clock=lambda: datetime.now(UTC)):
        self.retention_days = retention_days
        self.clock = clock
        self.consents: dict[UUID, Consent] = {}
        self.sessions: dict[str, dict] = {}
        self.rounds: dict[tuple[str, int], dict] = {}
        self.texts: dict[str, dict] = {}

    async def consent(self, user_id: UUID) -> Consent | None:
        return self.consents.get(user_id)

    async def save_consent(self, user_id: UUID, raw_text: bool) -> Consent:
        now, old = self.clock(), self.consents.get(user_id)
        agreed = old.records_agreed_at if old and old.current else now
        self.consents[user_id] = Consent(CONSENT_VERSION, raw_text, agreed, now)
        if not raw_text:
            for sid in [sid for sid, t in self.texts.items() if t["user_id"] == user_id]:
                del self.texts[sid]
        return self.consents[user_id]

    async def create(self, session_id: str, user_id: UUID, request: CareRequest) -> None:
        now = self.clock()
        self.sessions[session_id] = {
            "user_id": user_id,
            "created_at": now,
            "updated_at": now,
            "step": None,
            "node": None,
            "goal": None,
            "request": request_row(request),
            "intake": None,
            "turns": [],
            "pre": None,
            "final": None,
            "end_reason": None,
            "end_summary": None,
            "ended_at": None,
            "expired_at": None,
        }

    async def session(self, session_id: str) -> SessionRecord | None:
        row = self.sessions.get(session_id)
        if row is None:
            return None
        return SessionRecord(
            id=session_id,
            user_id=row["user_id"],
            end_summary=row["end_summary"],
            expired=row["expired_at"] is not None,
        )

    async def save(self, record: SessionRecord, values: dict, view: dict) -> None:
        now, row = self.clock(), self.sessions[record.id]
        columns = session_columns(values, view)
        ended = columns.pop("ended")
        row.update(columns, updated_at=now)
        if ended and row["ended_at"] is None:
            row["ended_at"] = now
        for r in round_rows(values):
            self.rounds.setdefault((record.id, r["round"]), r)
        consent = self.consents.get(record.user_id)
        if safety_ended(values):
            self.texts.pop(record.id, None)
        elif consent is not None and consent.raw_text:
            old = self.texts.get(record.id)
            expires = old["expires_at"] if old else now + timedelta(days=self.retention_days)
            self.texts[record.id] = {
                **text_row(values),
                "user_id": record.user_id,
                "expires_at": expires,
            }

    async def delete_expired_texts(self) -> int:
        now = self.clock()
        gone = [sid for sid, t in self.texts.items() if t["expires_at"] < now]
        for sid in gone:
            del self.texts[sid]
        return len(gone)

    async def stale_threads(self, idle: timedelta, checkpointer: BaseCheckpointSaver) -> list[str]:
        cutoff = self.clock() - idle
        threads = {t.config["configurable"]["thread_id"] async for t in checkpointer.alist(None)}
        stale = []
        for thread_id in sorted(threads):
            row = self.sessions.get(thread_id)
            if (
                row is None
                or row["ended_at"] is not None
                or row["expired_at"] is not None
                or row["updated_at"] < cutoff
            ):
                stale.append(thread_id)
        return stale

    async def mark_expired(self, session_ids: Iterable[str]) -> int:
        now, count = self.clock(), 0
        for sid in session_ids:
            row = self.sessions.get(sid)
            if row and row["ended_at"] is None and row["expired_at"] is None:
                row["expired_at"] = now
                count += 1
        return count

    async def delete_user(self, user_id: UUID) -> list[str]:
        """auth.users 행을 지운 것과 같다(on delete cascade)."""
        self.consents.pop(user_id, None)
        session_ids = [sid for sid, row in self.sessions.items() if row["user_id"] == user_id]
        for sid in session_ids:
            del self.sessions[sid]
            self.texts.pop(sid, None)
            for key in [key for key in self.rounds if key[0] == sid]:
                del self.rounds[key]
        return session_ids


def _json(value: Any) -> Jsonb | None:
    return None if value is None else Jsonb(value)


CONSENT_SQL = """
insert into public.user_consents (user_id, version, records_agreed_at, raw_text)
values (%s, %s, now(), %s)
on conflict (user_id) do update set
  records_agreed_at = case when user_consents.version = excluded.version
    then user_consents.records_agreed_at else excluded.records_agreed_at end,
  version = excluded.version, raw_text = excluded.raw_text, updated_at = now()
returning version, raw_text, records_agreed_at, updated_at
"""
SESSION_SQL = """
select user_id, end_summary, expired_at is not null from public.care_sessions where id = %s
"""
UPDATE_SQL = """
update public.care_sessions set
  updated_at = now(), step = %(step)s, node = %(node)s, goal = %(goal)s, request = %(request)s,
  intake = %(intake)s, turns = %(turns)s, pre = %(pre)s, final = %(final)s,
  end_reason = %(end_reason)s, end_summary = %(end_summary)s,
  ended_at = case when %(ended)s then coalesce(ended_at, now()) end
where id = %(id)s
"""
ROUND_SQL = """
insert into public.care_rounds (
  session_id, round, protocol_id, minutes, evidence_record_ids, relaxed, environment_gaps,
  completed, pre, post, deltas, improved, recommended, options, next_step
) values (
  %(session_id)s, %(round)s, %(protocol_id)s, %(minutes)s, %(evidence_record_ids)s, %(relaxed)s,
  %(environment_gaps)s, %(completed)s, %(pre)s, %(post)s, %(deltas)s, %(improved)s,
  %(recommended)s, %(options)s, %(next_step)s
) on conflict (session_id, round) do nothing
"""
# 원문 동의가 있을 때만 쓴다 — 동의는 쓰는 순간의 값을 본다
TEXT_SQL = """
insert into public.care_texts (
  session_id, user_id, request_text, answers, intake, rounds, expires_at
)
select
  %(session_id)s, %(user_id)s, %(request_text)s, %(answers)s, %(intake)s, %(rounds)s,
  now() + make_interval(days => %(retention_days)s)
where exists (
  select 1 from public.user_consents where user_id = %(user_id)s and raw_text
)
on conflict (session_id) do update set
  answers = excluded.answers, intake = excluded.intake, rounds = excluded.rounds, updated_at = now()
"""
STALE_SQL = """
select distinct c.thread_id from langgraph.checkpoints c
left join public.care_sessions s on s.id::text = c.thread_id
where s.id is null or s.ended_at is not null or s.expired_at is not null
  or s.updated_at < now() - %s
order by c.thread_id
"""


class PostgresStore:
    """Supabase Postgres. app/db.py의 풀로 쓴다. 체크포인트 목록은 langgraph 스키마에서 읽는다."""

    def __init__(self, pool: AsyncConnectionPool, retention_days: int = 90):
        self.pool = pool
        self.retention_days = retention_days

    async def consent(self, user_id: UUID) -> Consent | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "select version, raw_text, records_agreed_at, updated_at"
                " from public.user_consents where user_id = %s",
                (user_id,),
            )
            row = await cur.fetchone()
        return Consent(*row) if row else None

    async def save_consent(self, user_id: UUID, raw_text: bool) -> Consent:
        async with self.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(CONSENT_SQL, (user_id, CONSENT_VERSION, raw_text))
            row = await cur.fetchone()
            if not raw_text:
                await conn.execute("delete from public.care_texts where user_id = %s", (user_id,))
        return Consent(*row)

    async def create(self, session_id: str, user_id: UUID, request: CareRequest) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "insert into public.care_sessions (id, user_id, request) values (%s, %s, %s)",
                (UUID(session_id), user_id, Jsonb(request_row(request))),
            )

    async def session(self, session_id: str) -> SessionRecord | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(SESSION_SQL, (UUID(session_id),))
            row = await cur.fetchone()
        if row is None:
            return None
        user_id, end_summary, expired = row
        return SessionRecord(session_id, user_id, end_summary, expired)

    async def save(self, record: SessionRecord, values: dict, view: dict) -> None:
        columns = session_columns(values, view)
        params = {
            **columns,
            "id": UUID(record.id),
            **{
                key: _json(columns[key])
                for key in ("request", "intake", "turns", "pre", "final", "end_summary")
            },
        }
        rounds = [
            {
                **r,
                "session_id": UUID(record.id),
                **{key: Jsonb(r[key]) for key in ("pre", "post", "deltas")},
            }
            for r in round_rows(values)
        ]
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(UPDATE_SQL, params)
            if rounds:
                async with conn.cursor() as cur:
                    await cur.executemany(ROUND_SQL, rounds)
            if safety_ended(values):
                await conn.execute(
                    "delete from public.care_texts where session_id = %s", (UUID(record.id),)
                )
            else:
                text = text_row(values)
                await conn.execute(
                    TEXT_SQL,
                    {
                        **text,
                        "session_id": UUID(record.id),
                        "user_id": record.user_id,
                        "retention_days": self.retention_days,
                        **{key: Jsonb(text[key]) for key in ("answers", "intake", "rounds")},
                    },
                )

    async def delete_expired_texts(self) -> int:
        async with self.pool.connection() as conn:
            cur = await conn.execute("delete from public.care_texts where expires_at < now()")
            return cur.rowcount

    async def stale_threads(self, idle: timedelta, checkpointer: BaseCheckpointSaver) -> list[str]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(STALE_SQL, (idle,))
            return [thread_id for (thread_id,) in await cur.fetchall()]

    async def mark_expired(self, session_ids: Iterable[str]) -> int:
        ids = _uuids(session_ids)
        if not ids:
            return 0
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "update public.care_sessions set expired_at = now()"
                " where id = any(%s) and ended_at is null and expired_at is null",
                (ids,),
            )
            return cur.rowcount

    async def delete_user(self, user_id: UUID) -> list[str]:
        """Supabase Auth 사용자를 지운다. auth 스키마의 로그인 세션 · 연동 계정 · MFA와 우리 기록이
        모두 on delete cascade라 행 하나로 끝난다(2026-10-28 운영 DB에서 제약 확인)."""
        async with self.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "select id::text from public.care_sessions where user_id = %s", (user_id,)
            )
            session_ids = [sid for (sid,) in await cur.fetchall()]
            await conn.execute("delete from auth.users where id = %s", (user_id,))
        return session_ids
