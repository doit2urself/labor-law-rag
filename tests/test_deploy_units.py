"""체크포인트 정리용 systemd 유닛(deploy/labor-rag-prune.service·.timer)이 실제 스크립트와 맞는지 시험한다 (최종 검토 지적 I-3).

왜 필요한가: 스펙 6.4는 "체크포인트가 쌓이므로 주기적으로 정리한다"고 했는데, 정리 스크립트(scripts/prune_checkpoints.py)를 서버에서 주기적으로
돌리는 장치가 없었다. `data/checkpoints.db`에는 질문 원문과 단계마다의 후보 본문이 쌓이므로 정리하지 않으면 계속 커지고 질문 원문이 기한 없이 남는다.
유닛 파일은 서버에 올려서 처음 켜 보기 전까지는 틀린 인자나 권한 실수를 알 수 없으므로, 파일 안의 인자를 **진짜 스크립트에 넣어** 확인해 둔다.
"""

from __future__ import annotations

import configparser
import shlex
from pathlib import Path

import pytest

from app.container import make_checkpointer
from scripts import prune_checkpoints

REPO = Path(__file__).resolve().parent.parent
DEPLOY = REPO / "deploy"


def read_unit(name: str) -> configparser.ConfigParser:
    """systemd 유닛 파일(INI 비슷한 형식)을 읽는다. 값 안의 `%`를 해석하지 않고 대소문자를 그대로 둔다."""
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    assert (DEPLOY / name).is_file(), f"{name} 파일이 없습니다"
    parser.read(DEPLOY / name, encoding="utf-8")
    return parser


# 정리 작업은 한 번 실행하고 끝나는 일(oneshot)이고, 앱과 **같은 계정**으로 돈다. root로 돌면 앱이 쓰는 SQLite 보조 파일(-wal, -shm)이
# root 소유로 생겨서 앱이 그 파일에 쓰지 못한다.
def test_prune_service_is_a_oneshot_run_as_the_app_user():
    prune = read_unit("labor-rag-prune.service")["Service"]
    app = read_unit("labor-rag.service")["Service"]
    assert prune["Type"] == "oneshot"
    assert prune["User"] == app["User"] and prune["Group"] == app["Group"]
    assert prune["WorkingDirectory"] == app["WorkingDirectory"]


# 유닛에 적힌 실행 명령의 인자를 진짜 스크립트에 넣어 본다(--dry-run으로: 아무것도 지우지 않는다). 인자가 틀렸으면 argparse가 멈춘다.
def test_prune_exec_arguments_are_accepted_by_the_real_script(tmp_path, capsys):
    tokens = shlex.split(read_unit("labor-rag-prune.service")["Service"]["ExecStart"])
    assert tokens[0] == "/opt/labor-rag/.venv/bin/python"  # 앱과 같은 가상환경
    assert tokens[1] == "scripts/prune_checkpoints.py" and (REPO / tokens[1]).is_file()
    db = tmp_path / "checkpoints.db"
    make_checkpointer(str(db))  # 체크포인트 파일을 실제 형식으로 만든다
    assert prune_checkpoints.main([*tokens[2:], "--dry-run", "--db", str(db)]) == 0
    assert "삭제 대상(삭제하지 않음)" in capsys.readouterr().out


# 보관 기간은 30일이다: Langfuse 무료판의 보관 기간(30일, 스펙 6.7)과 맞췄다. 대화를 이어 가려면 최근 대화만 있으면 된다.
def test_prune_keeps_thirty_days():
    tokens = shlex.split(read_unit("labor-rag-prune.service")["Service"]["ExecStart"])
    assert tokens[tokens.index("--days") + 1] == "30"


# 앱 유닛과 같은 보호 설정: 쓸 수 있는 곳은 data 폴더뿐이다.
def test_prune_service_is_sandboxed_like_the_app():
    prune = read_unit("labor-rag-prune.service")["Service"]
    assert prune["ProtectSystem"] == "strict" and prune["NoNewPrivileges"] == "true"
    assert prune["ReadWritePaths"] == "/opt/labor-rag/data"


# 타이머: 매일 한 번, 서버가 꺼져 있던 시간에 놓친 실행은 켜질 때 따라잡는다(Persistent). 같은 이름의 서비스를 깨운다.
def test_prune_timer_runs_daily_and_catches_up():
    timer = read_unit("labor-rag-prune.timer")
    assert timer["Timer"]["OnCalendar"] == "daily"
    assert timer["Timer"]["Persistent"] == "true"
    assert timer["Install"]["WantedBy"] == "timers.target"
    assert timer["Timer"].get("Unit", "labor-rag-prune.service") == "labor-rag-prune.service"
    # 정각에 몰리지 않게 조금 흩뜨린다(설정이 있으면 됨, 값은 자유).
    assert "RandomizedDelaySec" in timer["Timer"]
