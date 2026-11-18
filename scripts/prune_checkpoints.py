"""오래된 대화(thread)의 체크포인트를 지운다 (Task 13).

왜 필요한가: 체크포인트는 대화의 모든 단계 상태를 SQLite에 쌓아 두므로 질문이 늘수록 파일이 계속 커진다(스펙 6.4:
"체크포인트가 쌓이므로 주기적으로 정리한다"). 오래 쓰지 않은 대화만 골라 지워 파일 크기를 묶어 둔다.

사용법(프로젝트 루트에서):
    python scripts/prune_checkpoints.py --days 30 --dry-run     # 지울 대화만 보여 준다(아무것도 지우지 않음)
    python scripts/prune_checkpoints.py --days 30                # 마지막 활동이 30일보다 오래된 대화를 지운다
    python scripts/prune_checkpoints.py --days 30 --db data/checkpoints.db

대상 파일은 `--db`로 지정한다(기본 `data/checkpoints.db`). 체크포인터가 쓰는 SQLite 파일과 같아야 한다.
테이블은 설치된 langgraph-checkpoint-sqlite 소스(setup())에서 확인했다: `checkpoints`, `writes`. 삭제는 두 테이블을
직접 건드리지 않고 체크포인터의 공식 메서드 `delete_thread()`로 한다.

"오래됨"의 기준은 스레드의 **가장 최근 체크포인트**의 시각(체크포인트 안의 `ts`)이다. 오래전에 시작했어도 최근까지
이어진 대화는 지우지 않는다. 정확히 N일 된 것은 "N일보다 오래된" 것이 아니므로 지우지 않는다.

종료 코드: 0 성공, 1 대상 파일이 없음, 2 인자 오류(argparse). `--days`는 1 이상만 받는다 — 0이면 전부 지우는 사고가 난다.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# `python scripts/prune_checkpoints.py`로 직접 실행하면 sys.path[0]이 scripts/ 폴더라서 `import app`이 안 된다.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: E402

from app.graph.state import checkpoint_serde  # noqa: E402

DEFAULT_DB = ROOT / "data" / "checkpoints.db"


def _positive_int(text: str) -> int:
    """argparse용: 1 이상의 정수만 받는다."""
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("1 이상의 정수여야 합니다")
    return value


def prune_old_threads(
    db_path: str | Path,
    days: int,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
) -> list[str]:
    """마지막 활동이 `days`일보다 오래된 스레드를 지우고(dry_run이면 지우지 않고) 그 thread_id 목록을 돌려준다.

    now: 기준 시각(테스트용으로 바꿔 끼운다). 없으면 지금(UTC).
    체크포인트 테이블이 없는 파일은 새로 만들지 않고 빈 목록을 돌려준다.
    """
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    try:
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'checkpoints'"
        ).fetchone()
        if not has_table:
            return []
        saver = SqliteSaver(conn, serde=checkpoint_serde())
        # 체크포인트 ID는 시간순으로 커지는 ID(uuid6)라서 스레드별 MAX가 가장 최근 것이다(라이브러리도 같은 가정으로 정렬한다).
        newest = conn.execute("SELECT thread_id, MAX(checkpoint_id) FROM checkpoints GROUP BY thread_id").fetchall()
        stale: list[str] = []
        for thread_id, checkpoint_id in newest:
            found = saver.get_tuple(
                {"configurable": {"thread_id": thread_id, "checkpoint_ns": "", "checkpoint_id": checkpoint_id}}
            )
            if found is None:
                continue  # 시각을 알 수 없는 것(예: 하위 그래프 전용 스레드)은 지우지 않는다.
            if datetime.fromisoformat(found.checkpoint["ts"]) < cutoff:
                stale.append(thread_id)
        stale.sort()
        if not dry_run:
            for thread_id in stale:
                saver.delete_thread(thread_id)
        return stale
    finally:
        conn.close()


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(description="오래된 대화의 체크포인트를 삭제한다")
    parser.add_argument("--days", type=_positive_int, required=True, help="마지막 활동이 이 일수보다 오래된 대화를 지운다")
    parser.add_argument("--db", default=str(DEFAULT_DB), help=f"체크포인트 SQLite 파일 (기본 {DEFAULT_DB})")
    parser.add_argument("--dry-run", action="store_true", help="지울 대상만 보여 주고 지우지 않는다")
    args = parser.parse_args(argv)

    if not Path(args.db).exists():
        print(f"체크포인트 파일을 찾을 수 없습니다: {args.db}", file=sys.stderr)
        return 1
    stale = prune_old_threads(args.db, args.days, now=now, dry_run=args.dry_run)
    verb = "삭제 대상(삭제하지 않음)" if args.dry_run else "삭제함"
    print(f"{verb}: 마지막 활동이 {args.days}일보다 오래된 대화 {len(stale)}개")
    for thread_id in stale:
        print(f"  - {thread_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
