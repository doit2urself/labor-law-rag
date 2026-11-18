"""로그인 비밀번호의 bcrypt 해시를 만들어 `.env`에 붙여 넣을 한 줄로 출력한다 (Task 14).

사용법(프로젝트 루트에서, 사용자가 **자기 PC의 터미널**에서 직접):
    .venv\\Scripts\\python.exe scripts\\make_password_hash.py --account admin
    .venv\\Scripts\\python.exe scripts\\make_password_hash.py --account test
비밀번호는 화면에 보이지 않게 두 번 입력받는다. 출력된 줄(`LABOR_ADMIN_PASSWORD_HASH='$2b$12$…'`)을 서버의 `.env`에 넣고 서버를
다시 시작하면 그 해시가 계정에 반영된다. **비밀번호 원문은 어디에도 저장·출력하지 않는다**(해시만 출력). 이 출력도 대화나
저장소에 붙여 넣지 말고 서버 `.env`에 직접 옮긴다.

값을 작은따옴표로 감싸 출력하는 이유: 해시에 `$`가 들어 있어서다. python-dotenv(설정 읽기)로는 따옴표가 있든 없든 그대로
읽히는 것을 확인했다(tests/test_make_password_hash.py). 서버를 띄우는 systemd가 같은 줄을 어떻게 읽는지는 Task 18에서 확인한다.

종료 코드: 0 성공, 1 입력 문제(비어 있음·두 번 입력이 다름·72바이트 초과), 2 인자 오류.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

# `python scripts/make_password_hash.py`로 직접 실행하면 sys.path[0]이 scripts/ 폴더라서 `import app`이 안 된다.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.api.auth import hash_password  # noqa: E402

# 이보다 짧으면 경고한다(막지는 않는다). 사용자가 `test` 비밀번호를 짧게 정했고 그 위험을 수용했기 때문이다.
WEAK_PASSWORD_CHARS = 8


def main(
    argv: Sequence[str] | None = None,
    *,
    ask: Callable[[str], str] = getpass.getpass,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """스크립트 본체. ask(입력 함수)·out·err는 테스트에서 바꿔 끼운다(없으면 터미널 입력, 표준 출력, 표준 오류)."""
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    parser = argparse.ArgumentParser(description="로그인 비밀번호의 bcrypt 해시를 .env 한 줄로 출력한다")
    parser.add_argument("--account", required=True, choices=["admin", "test"], help="어느 계정의 비밀번호인지")
    args = parser.parse_args(argv)

    password = ask("새 비밀번호: ")
    if not password:
        print("비밀번호가 비어 있습니다.", file=err)
        return 1
    if ask("한 번 더: ") != password:
        print("두 값이 다릅니다. 아무것도 만들지 않았습니다.", file=err)
        return 1
    try:
        hashed = hash_password(password)
    except ValueError as exc:  # 72바이트 초과
        print(str(exc), file=err)
        return 1
    if len(password) < WEAK_PASSWORD_CHARS:
        print(f"경고: 비밀번호가 {WEAK_PASSWORD_CHARS}자보다 짧습니다. 공개된 서버에서는 쉽게 추측될 수 있습니다.", file=err)

    print(f"LABOR_{args.account.upper()}_PASSWORD_HASH='{hashed}'", file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
