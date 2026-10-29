"""세션 기록 — PostgresStore를 마이그레이션한 DB에 써 본다(TEST_DATABASE_URL이 있을 때만).

대역 그래프로 세션을 끝까지 돌리며 세션 API와 같은 recorder로 쓰고, 행 · 원문 · 동의 철회 ·
auth.users 삭제(cascade) · 정리 작업 SQL을 본다. 모두 트랜잭션 하나 안에서 돌고 끝에 되돌린다.
로컬 Postgres에는 auth 스키마가 없으므로 README 「데이터베이스」대로 auth.users · auth.sessions를
흉내 낸 뒤 supabase/migrations를 적용한다.
"""

import asyncio
import uuid
from datetime import timedelta

from langgraph.types import Command
from psycopg import AsyncConnection

from app.agent.checkpoint import memory_checkpointer
from app.agent.context import AgentContext
from app.agent.graph import build_graph
from app.api import sessions
from app.auth import AuthSessions
from app.records import PostgresStore, SessionRecord
from app.schemas.care import CareGoal, CareRequest
from tests.agent.test_graph import (
    ASK_TIMING,
    BREATHING,
    S2_READING,
    STAI6,
    FakeIntaker,
    FakePlanner,
    inference,
    reading,
    sam,
)
from tests.evidence_db import DATABASE_URL, OneConnection, requires_db

pytestmark = requires_db

PRE_S2 = {**sam(3, 7), "INS-STAI6": STAI6}


async def in_rollback(test) -> None:
    conn = await AsyncConnection.connect(DATABASE_URL, prepare_threshold=None)
    try:
        await test(conn, PostgresStore(OneConnection(conn)))
    finally:
        await conn.rollback()
        await conn.close()


async def new_user(
    conn: AsyncConnection, anonymous: bool = False, age: str = "0 days"
) -> uuid.UUID:
    """auth.users 행. anonymous면 게스트, age만큼 전에 만들었다."""
    user_id = uuid.uuid4()
    await conn.execute(
        "insert into auth.users (id, is_anonymous, created_at)"
        " values (%s, %s, now() - %s::interval)",
        (user_id, anonymous, age),
    )
    return user_id


async def login_session(conn: AsyncConnection, user_id: uuid.UUID, age: str) -> None:
    """age만큼 전에 갱신한 로그인 세션. refreshed_at은 Supabase처럼 시간대 없는 UTC다."""
    await conn.execute(
        "insert into auth.sessions (id, user_id, created_at, updated_at, refreshed_at) values"
        " (%s, %s, now() - interval '60 days', now() - %s::interval,"
        " (now() - %s::interval) at time zone 'UTC')",
        (uuid.uuid4(), user_id, age, age),
    )


async def drive(store, record, graph, inputs) -> dict:
    """세션 API처럼 입력마다 다음 멈춤까지 돌리고 기록한다. 마지막 응답 본문을 돌려준다."""
    view: dict = {}
    for value in inputs:
        finish = sessions.recorder(graph, store, record)
        async for name, data in sessions.run_events(
            graph, record.id, value, AgentContext(db_pool=None, session_id=record.id), finish
        ):
            if name == "result":
                view = data
    return view


async def one(conn: AsyncConnection, sql: str, *params):
    cur = await conn.execute(sql, params)
    return await cur.fetchone()


def test_a_finished_session_is_recorded_and_follows_consent_and_account_removal():
    async def test(conn, store):
        user_id = await new_user(conn)
        await store.save_consent(user_id, raw_text=True)
        graph = build_graph(
            FakePlanner(BREATHING),
            FakeIntaker(S2_READING, goal=inference(CareGoal.S2)),
            memory_checkpointer(),
        )
        request = CareRequest(text="발표 앞두고 긴장돼요")
        record = SessionRecord(id=str(uuid.uuid4()), user_id=user_id)
        await store.create(record.id, user_id, request)
        final = {"INS-STAI6": {**STAI6, "tense": 1, "worried": 1}}
        end = await drive(
            store,
            record,
            graph,
            [
                {"request": request},
                Command(resume=PRE_S2),
                Command(resume={"completed": True, "notice_words": ["편안"]}),
                Command(resume=sam(5, 4)),
                Command(resume={"next_step": "END"}),
                Command(resume=final),
            ],
        )
        assert end["step"] == "END"

        row = await one(
            conn,
            "select step, goal, end_reason, end_summary, jsonb_array_length(pre),"
            " final->>'instrument_id', ended_at is not null from public.care_sessions"
            " where id = %s",
            uuid.UUID(record.id),
        )
        assert row == ("END", "S2", "DONE", end["payload"], 2, "INS-STAI6", True)
        rounds = await one(
            conn,
            "select round, protocol_id, completed, improved, recommended, options, next_step"
            " from public.care_rounds where session_id = %s",
            uuid.UUID(record.id),
        )
        assert rounds == (
            1,
            "PR-06",
            True,
            True,
            "END",
            ["END", "CHANGE_ACTIVITY", "ADDITIONAL_SESSION"],
            "END",
        )
        # 고른 과정(등급 가중 추첨 — 세션 ID 시드)과 추천의 출처(대역 그래프라 규칙 그대로)
        picked = await one(
            conn,
            "select selection->>'method', selection->'order' ? 'PR-06',"
            " (selection->>'probability')::float > 0, rule_recommended, recommended_by_llm"
            " from public.care_rounds where session_id = %s",
            uuid.UUID(record.id),
        )
        assert picked == ("WEIGHTED", True, True, "END", False)
        assert await store.recent_protocols(user_id, 5) == ["PR-06"]
        text = await one(
            conn,
            "select request_text, rounds->0->'notice_words',"
            " expires_at - created_at from public.care_texts where session_id = %s",
            uuid.UUID(record.id),
        )
        assert text == ("발표 앞두고 긴장돼요", ["편안"], timedelta(days=90))
        stored = await store.session(record.id)
        assert (stored.user_id, stored.end_summary) == (user_id, end["payload"])

        # 원문 동의를 거두면 원문만 지운다
        consent = await store.save_consent(user_id, raw_text=False)
        assert (consent.raw_text, consent.current) == (False, True)
        assert await one(conn, "select count(*) from public.care_texts") == (0,)
        assert await one(conn, "select count(*) from public.care_rounds") == (1,)

        # 탈퇴 — 사용자를 지우면 기록 · 동의 · 로그인 세션이 함께 사라진다
        login = uuid.uuid4()
        await conn.execute(
            "insert into auth.sessions (id, user_id) values (%s, %s)", (login, user_id)
        )
        assert await AuthSessions(OneConnection(conn)).alive(login)
        assert await store.delete_user(user_id) == [record.id]
        for table in ("public.care_sessions", "public.care_rounds", "public.user_consents"):
            assert await one(conn, f"select count(*) from {table}") == (0,), table
        assert await one(conn, "select count(*) from auth.users where id = %s", user_id) == (0,)
        assert not await AuthSessions(OneConnection(conn)).alive(login)

    asyncio.run(in_rollback(test))


def test_history_reads_the_sessions_with_activities_and_their_rounds():
    async def test(conn, store):
        user_id = await new_user(conn)
        await store.save_consent(user_id, raw_text=False)
        graph = build_graph(
            FakePlanner(BREATHING),
            FakeIntaker(S2_READING, goal=inference(CareGoal.S2)),
            memory_checkpointer(),
        )
        request = CareRequest(text="발표 앞두고 긴장돼요")
        waiting = SessionRecord(id=str(uuid.uuid4()), user_id=user_id)  # 활동 전에 멈췄다
        await store.create(waiting.id, user_id, request)
        await drive(store, waiting, graph, [{"request": request}])
        record = SessionRecord(id=str(uuid.uuid4()), user_id=user_id)
        await store.create(record.id, user_id, request)
        final = {"INS-STAI6": {**STAI6, "tense": 1, "worried": 1}}
        end = await drive(
            store,
            record,
            graph,
            [
                {"request": request},
                Command(resume=PRE_S2),
                Command(resume={"completed": True}),
                Command(resume=sam(5, 4)),
                Command(resume={"next_step": "END"}),
                Command(resume=final),
            ],
        )

        (session,) = await store.history(user_id, 20)
        assert (session["session_id"], session["goal"]) == (record.id, "S2")
        assert session["ended_at"] is not None
        # 라운드 행과 END payload가 같은 기록을 담는다
        assert session["rounds"] == end["payload"]["rounds"]
        assert session["goal_scale"] == end["payload"]["goal_scale"]
        assert await store.history(await new_user(conn), 20) == []

    asyncio.run(in_rollback(test))


def test_a_safety_end_drops_the_texts_already_kept():
    async def test(conn, store):
        user_id = await new_user(conn)
        await store.save_consent(user_id, raw_text=True)
        intaker = FakeIntaker(
            reading(question=ASK_TIMING), reading(safety=True), goal=inference(CareGoal.S2)
        )
        graph = build_graph(FakePlanner(BREATHING), intaker, memory_checkpointer())
        request = CareRequest(text="요즘 좀 힘들어요", setting="HOME")
        record = SessionRecord(id=str(uuid.uuid4()), user_id=user_id)
        await store.create(record.id, user_id, request)
        ask = await drive(store, record, graph, [{"request": request}])
        assert ask["step"] == "ASK"
        assert await one(conn, "select count(*) from public.care_texts") == (1,)

        answer = {"question_id": "q1", "text": "살 이유를 모르겠어요"}
        end = await drive(store, record, graph, [Command(resume=answer)])
        assert end["payload"] == {"reason": "SAFETY"}
        row = await one(
            conn,
            "select end_reason, request, intake, turns from public.care_sessions where id = %s",
            uuid.UUID(record.id),
        )
        assert row == ("SAFETY", {}, None, [])
        assert await one(conn, "select count(*) from public.care_texts") == (0,)

    asyncio.run(in_rollback(test))


def test_cleanup_queries_pick_idle_orphan_and_finished_threads():
    async def test(conn, store):
        user_id = await new_user(conn)
        idle, active, ended, orphan = (str(uuid.uuid4()) for _ in range(4))
        for sid in (idle, active, ended):
            await store.create(sid, user_id, CareRequest(text="-"))
        await conn.execute(
            "update public.care_sessions set updated_at = now() - interval '2 days' where id = %s",
            (uuid.UUID(idle),),
        )
        await conn.execute(
            "update public.care_sessions set ended_at = now() where id = %s", (uuid.UUID(ended),)
        )
        for sid in (idle, active, ended, orphan):
            await conn.execute(
                "insert into langgraph.checkpoints (thread_id, checkpoint_id, checkpoint)"
                " values (%s, '1', '{}')",
                (sid,),
            )
        await conn.execute(
            "insert into public.care_texts (session_id, user_id, request_text, expires_at)"
            " values (%s, %s, '-', now() - interval '1 day')",
            (uuid.UUID(active), user_id),
        )

        stale = await store.stale_threads(timedelta(hours=24), checkpointer=None)
        assert stale == sorted([idle, ended, orphan])
        assert await store.mark_expired(stale) == 1  # 끝난 세션과 주인 없는 thread는 만료가 아니다
        assert (await store.session(idle)).expired
        assert await store.delete_expired_texts() == 1

    asyncio.run(in_rollback(test))


def test_a_login_session_past_not_after_is_not_alive():
    async def test(conn, store):
        user_id = await new_user(conn)
        sessions = AuthSessions(OneConnection(conn))
        current, boxed = uuid.uuid4(), uuid.uuid4()
        await conn.execute(
            "insert into auth.sessions (id, user_id, not_after) values"
            " (%s, %s, null), (%s, %s, now() - interval '1 minute')",
            (current, user_id, boxed, user_id),
        )
        assert await sessions.alive(current)
        assert not await sessions.alive(boxed)  # 기한을 둔 세션이 끝났다
        assert not await sessions.alive(uuid.uuid4())  # 로그아웃해 행이 없다

    asyncio.run(in_rollback(test))


def test_a_guest_session_keeps_only_its_progress():
    async def test(conn, store):
        guest = await new_user(conn, anonymous=True)
        graph = build_graph(
            FakePlanner(BREATHING),
            FakeIntaker(S2_READING, goal=inference(CareGoal.S2)),
            memory_checkpointer(),
        )
        request = CareRequest(text="발표 앞두고 긴장돼요", setting="HOME")
        record = SessionRecord(id=str(uuid.uuid4()), user_id=guest, guest=True)
        await store.create(record.id, guest, request, guest=True)
        final = {"INS-STAI6": {**STAI6, "tense": 1, "worried": 1}}
        end = await drive(
            store,
            record,
            graph,
            [
                {"request": request},
                Command(resume=PRE_S2),
                Command(resume={"completed": True, "notice_words": ["편안"]}),
                Command(resume=sam(5, 4)),
                Command(resume={"next_step": "END"}),
                Command(resume=final),
            ],
        )
        row = await one(
            conn,
            "select step, end_reason, end_summary, goal, request, intake, turns, pre, final"
            " from public.care_sessions where id = %s",
            uuid.UUID(record.id),
        )
        assert row == ("END", "DONE", end["payload"], None, {}, None, [], None, None)
        assert await one(conn, "select count(*) from public.care_rounds") == (0,)
        assert await one(conn, "select count(*) from public.care_texts") == (0,)

    asyncio.run(in_rollback(test))


def test_inactive_guests_go_with_their_records_and_the_rest_stay():
    async def test(conn, store):
        # GUEST_RETENTION_DAYS 기본 1일
        idle = await new_user(conn, anonymous=True, age="3 days")
        await login_session(conn, idle, age="2 days")
        await store.create(str(uuid.uuid4()), idle, CareRequest(text="-"), guest=True)
        await conn.execute(
            "update public.care_sessions set updated_at = now() - interval '2 days'"
            " where user_id = %s",
            (idle,),
        )
        returning = await new_user(conn, anonymous=True, age="3 days")
        await login_session(conn, returning, age="2 hours")  # 두 시간 전에 다시 와 토큰을 갱신했다
        playing = await new_user(conn, anonymous=True, age="3 days")
        await store.create(str(uuid.uuid4()), playing, CareRequest(text="-"), guest=True)  # 방금
        fresh = await new_user(conn, anonymous=True, age="1 hour")
        member = await new_user(conn, anonymous=False, age="400 days")  # 회원은 지우지 않는다

        assert await store.delete_inactive_guests() == 1
        cur = await conn.execute(
            "select id from auth.users where id = any(%s)",
            ([idle, returning, playing, fresh, member],),
        )
        assert {row[0] for row in await cur.fetchall()} == {returning, playing, fresh, member}
        assert await one(
            conn, "select count(*) from public.care_sessions where user_id = %s", idle
        ) == (0,)

    asyncio.run(in_rollback(test))
