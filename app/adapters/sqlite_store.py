"""SQLite 저장소: 계정, IP별 사용 횟수, 달별 리랭크 호출 수.

역할: 서버가 재시작돼도 남아야 하는 작은 상태 세 가지를 한 파일(`data/app.db`)에 보관한다.
- 계정(`admin`, `test`)과 비밀번호 해시 — 앱이 시작할 때 `.env`의 해시로 맞춘다(Task 14).
- `test` 계정의 IP별 사용 횟수 — "같은 IP에서 5회"(스펙 6.6).
- 달별 Cohere 리랭크 호출 수 — 월 950회에 닿으면 리랭크를 끈다(스펙 6.3, Task 8).
사용처: Task 8(RerankGate), Task 14(로그인·사용 횟수 제한).

핵심 설계 — **확인과 증가를 한 번에**: "5회 미만이면 허용"을 읽고 나서 따로 1을 올리면, 동시에 들어온 요청 둘이
모두 "4회 사용"을 읽고 통과해 6번째가 생긴다. 그래서 `BEGIN IMMEDIATE`로 쓰기 잠금을 먼저 잡고, 한 문장으로
조건을 검사하며 올린다.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Role = Literal["admin", "test"]

# 다른 요청이 쓰는 중이면 이 시간(초)까지 기다린다. 기다리지 않으면 "database is locked" 오류가 난다.
_BUSY_TIMEOUT_S = 30.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username      TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('admin', 'test'))
);
CREATE TABLE IF NOT EXISTS ip_usage (
    ip_key TEXT PRIMARY KEY,
    used   INTEGER NOT NULL DEFAULT 0 CHECK (used >= 0)
);
CREATE TABLE IF NOT EXISTS rerank_calls (
    month TEXT PRIMARY KEY,
    calls INTEGER NOT NULL DEFAULT 0
);
"""


@dataclass(frozen=True)
class User:
    """저장된 계정 한 건. password_hash는 bcrypt 해시이며 비밀번호 원문은 어디에도 저장하지 않는다."""

    username: str
    password_hash: str
    role: Role


class AppStore:
    """계정·사용 횟수·리랭크 호출 수를 보관하는 SQLite 저장소.

    path: 파일 경로. 폴더가 없으면 만든다. 테이블이 없으면 만든다.

    연결을 호출마다 새로 여는 이유: sqlite3 연결은 만든 스레드에서만 쓸 수 있다(`check_same_thread`).
    FastAPI가 요청을 여러 스레드에서 처리하므로, 연결을 공유하지 않고 호출마다 열고 닫는 쪽이 단순하고 안전하다.
    작은 파일이라 여는 비용은 무시할 만하다.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            # WAL: 읽는 쪽이 쓰는 쪽을 막지 않는다. 설정은 파일에 남으므로 처음 만들 때 한 번이면 된다.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None: 파이썬이 몰래 트랜잭션을 열지 않게 하고, 필요한 곳에서 BEGIN을 직접 쓴다.
        return sqlite3.connect(self._path, timeout=_BUSY_TIMEOUT_S, isolation_level=None)

    # ---- 계정 ----

    def upsert_user(self, username: str, password_hash: str, role: Role) -> None:
        """계정을 저장한다. 같은 이름이 있으면 해시와 역할을 덮어쓴다(역할이 admin/test가 아니면 IntegrityError)."""
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO users(username, password_hash, role) VALUES (?, ?, ?) "
                "ON CONFLICT(username) DO UPDATE SET password_hash = excluded.password_hash, role = excluded.role",
                (username, password_hash, role),
            )

    def get_user(self, username: str) -> User | None:
        """계정을 읽는다. 없으면 None."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT username, password_hash, role FROM users WHERE username = ?", (username,)
            ).fetchone()
        return User(*row) if row else None

    # ---- IP별 사용 횟수 ----

    def try_consume_ip_quota(self, ip_key: str, limit: int) -> bool:
        """이 IP의 사용 횟수가 `limit` 미만이면 1 올리고 True, 이미 한도면 올리지 않고 False.

        `BEGIN IMMEDIATE`: 이 시점에 쓰기 잠금을 잡아 다른 요청이 끼어들지 못하게 한다. 그 안에서
        `UPDATE ... WHERE used < limit` 한 문장으로 "검사 + 증가"를 하고, 바뀐 행이 있으면 허용이다.
        """
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("INSERT OR IGNORE INTO ip_usage(ip_key, used) VALUES (?, 0)", (ip_key,))
                cursor = conn.execute(
                    "UPDATE ip_usage SET used = used + 1 WHERE ip_key = ? AND used < ?", (ip_key, limit)
                )
                allowed = cursor.rowcount == 1
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return allowed

    def refund_ip_quota(self, ip_key: str) -> None:
        """사용 횟수를 1 돌려준다(서버 오류로 답이 나가지 못했을 때). 0 아래로는 내려가지 않는다."""
        with closing(self._connect()) as conn:
            conn.execute("UPDATE ip_usage SET used = used - 1 WHERE ip_key = ? AND used > 0", (ip_key,))

    # ---- 리랭크 호출 수 ----

    def get_rerank_calls(self, month: str) -> int:
        """`month`("YYYY-MM")에 지금까지 시도한 리랭크 호출 수. 기록이 없으면 0."""
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT calls FROM rerank_calls WHERE month = ?", (month,)).fetchone()
        return row[0] if row else 0

    def incr_rerank_calls(self, month: str) -> int:
        """`month`의 호출 수를 1 올리고, 올린 뒤의 값을 돌려준다. 동시에 불러도 하나도 잃지 않는다."""
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO rerank_calls(month, calls) VALUES (?, 1) "
                    "ON CONFLICT(month) DO UPDATE SET calls = calls + 1",
                    (month,),
                )
                (calls,) = conn.execute("SELECT calls FROM rerank_calls WHERE month = ?", (month,)).fetchone()
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return calls
