"""체크포인트 정리 스크립트 테스트: 임시 SQLite 파일에 대화(스레드)를 만들어 놓고, 오래된 스레드만 지워지는지 확인한다.

스키마(checkpoints, writes 두 테이블)는 설치된 langgraph-checkpoint-sqlite 소스(setup())에서 확인한 것이다.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.sqlite import SqliteSaver

from app.graph.build import build_graph
from app.graph.runner import run_chat
from app.graph.state import checkpoint_serde
from scripts import prune_checkpoints as script
from tests.conftest import FakeLlm, deps_with

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


def add_checkpoint(saver: SqliteSaver, thread_id: str, age_days: float, parent_config: dict | None = None) -> dict:
    """스레드에 체크포인트 하나를 저장한다. `age_days`일 전에 만들어진 것으로 기록한다(체크포인트 안의 ts 값)."""
    checkpoint = empty_checkpoint()
    checkpoint["ts"] = (NOW - timedelta(days=age_days)).isoformat()
    config = parent_config or {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    return saver.put(config, checkpoint, {"source": "input", "step": 0, "parents": {}}, {})


def table_threads(db: Path, table: str) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        return {row[0] for row in conn.execute(f"SELECT DISTINCT thread_id FROM {table}")}
    finally:
        conn.close()


@pytest.fixture
def db(tmp_path):
    """오래된 스레드, 최근 스레드, (처음은 오래됐지만 최근까지 이어진) 스레드가 든 임시 DB."""
    path = tmp_path / "checkpoints.db"
    conn = sqlite3.connect(path, check_same_thread=False)
    saver = SqliteSaver(conn, serde=checkpoint_serde())
    old = add_checkpoint(saver, "old", age_days=40)
    saver.put_writes(old, [("answer", "x")], task_id="t1")  # writes 테이블에도 기록을 남긴다
    add_checkpoint(saver, "recent", age_days=1)
    first = add_checkpoint(saver, "mixed", age_days=60)
    add_checkpoint(saver, "mixed", age_days=2, parent_config=first)  # 같은 스레드의 더 최근 체크포인트
    conn.close()
    return path


# 마지막 활동이 N일보다 오래된 스레드만 지워진다. checkpoints와 writes 두 테이블 모두에서.
def test_only_old_threads_are_deleted(db):
    removed = script.prune_old_threads(db, days=30, now=NOW)
    assert removed == ["old"]
    assert table_threads(db, "checkpoints") == {"recent", "mixed"}
    assert table_threads(db, "writes") == set()


# 스레드의 나이는 "가장 최근 체크포인트" 기준이다: 오래전에 시작했어도 최근까지 이어 온 대화는 지우지 않는다.
def test_age_is_measured_by_the_newest_checkpoint(db):
    script.prune_old_threads(db, days=30, now=NOW)
    assert "mixed" in table_threads(db, "checkpoints")


# 기준일을 줄이면 더 많이 지워지고, 기준일 경계에서는 지우지 않는다(정확히 N일 된 것은 "N일보다 오래됨"이 아니다).
def test_cutoff_boundaries(db):
    assert script.prune_old_threads(db, days=40, now=NOW, dry_run=True) == []  # old는 정확히 40일
    assert script.prune_old_threads(db, days=39, now=NOW, dry_run=True) == ["old"]
    assert script.prune_old_threads(db, days=1, now=NOW, dry_run=True) == ["mixed", "old"]


# dry_run은 대상만 알려 주고 아무것도 지우지 않는다.
def test_dry_run_deletes_nothing(db):
    assert script.prune_old_threads(db, days=30, now=NOW, dry_run=True) == ["old"]
    assert table_threads(db, "checkpoints") == {"old", "recent", "mixed"}


# 지울 것이 없을 때 다시 실행해도 안전하다(멱등).
def test_running_twice_is_safe(db):
    script.prune_old_threads(db, days=30, now=NOW)
    assert script.prune_old_threads(db, days=30, now=NOW) == []


# 진짜 그래프가 저장한 대화(우리 도메인 객체가 든 체크포인트)도 읽어서 나이를 판단한다. 최근 대화는 남는다.
def test_real_graph_thread_is_dated_and_kept(tmp_path):
    path = tmp_path / "real.db"
    conn = sqlite3.connect(path, check_same_thread=False)
    graph = build_graph(deps_with(FakeLlm.script()), SqliteSaver(conn, serde=checkpoint_serde()))
    tid = str(uuid.uuid4())
    list(run_chat(graph, tid, "주휴수당?"))
    conn.close()
    assert script.prune_old_threads(path, days=30) == []  # 방금 만든 대화는 최근이다
    assert script.prune_old_threads(path, days=1, now=datetime.now(timezone.utc) + timedelta(days=2)) == [tid]


# 테이블이 없는 DB(체크포인트를 쓴 적 없는 파일)는 아무것도 만들거나 지우지 않고 빈 결과를 낸다.
def test_database_without_checkpoint_tables_is_left_untouched(tmp_path):
    path = tmp_path / "other.db"
    sqlite3.connect(path).close()
    assert script.prune_old_threads(path, days=30, now=NOW) == []
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT name FROM sqlite_master").fetchall() == []  # 테이블을 새로 만들지 않았다
    conn.close()


# --- 명령줄 ------------------------------------------------------------------------------------------


# 없는 파일을 가리키면 새 DB를 만들지 않고 종료 코드 1로 알린다(경로 실수를 숨기지 않는다).
def test_cli_missing_database_fails_without_creating_it(tmp_path, capsys):
    path = tmp_path / "nope.db"
    assert script.main(["--days", "30", "--db", str(path)]) == 1
    assert not path.exists() and "찾을 수 없" in capsys.readouterr().err


# 0일이나 음수는 "전부 삭제"가 될 수 있어 거절한다.
@pytest.mark.parametrize("bad", ["0", "-5", "abc"])
def test_cli_rejects_non_positive_days(bad, tmp_path):
    with pytest.raises(SystemExit) as info:
        script.main(["--days", bad, "--db", str(tmp_path / "x.db")])
    assert info.value.code == 2


def test_cli_dry_run_reports_without_deleting(db, capsys):
    assert script.main(["--days", "30", "--db", str(db), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "old" in out and "삭제하지 않" in out
    assert table_threads(db, "checkpoints") == {"old", "recent", "mixed"}


def test_cli_prunes_for_real(db, capsys):
    # 시각을 100일 뒤로 주입한다(실제 시계에 기대지 않는다): 세 스레드가 모두 1일보다 오래된 것이 된다.
    assert script.main(["--days", "1", "--db", str(db)], now=NOW + timedelta(days=100)) == 0
    assert table_threads(db, "checkpoints") == set()
    assert "3개" in capsys.readouterr().out
