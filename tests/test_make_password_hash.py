"""`scripts/make_password_hash.py`를 시험한다 (Task 14, 🧑 단계에서 사용자가 쓰는 도구).

비밀번호는 터미널에서 입력받는다. 시험에서는 입력 함수(ask)를 바꿔 끼워서, 터미널 없이 같은 흐름을 돌린다.
"""

from __future__ import annotations

import io
import os
import re

import pytest

from app.api.auth import looks_like_bcrypt, verify_password
from app.config import Settings
from scripts.make_password_hash import main
from tests.conftest import settings_factory

PASSWORD = "p4ss-sentinel-xyz"


def run(account: str, *answers: str) -> tuple[int, str, str]:
    """스크립트를 돌리고 (종료 코드, 표준 출력, 표준 오류)를 돌려준다. answers는 비밀번호 입력 두 번(처음, 확인)이다."""
    typed = iter(answers)
    out, err = io.StringIO(), io.StringIO()
    code = main(["--account", account], ask=lambda prompt: next(typed), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def hash_in(output: str, variable: str) -> str:
    """출력의 `NAME='해시'` 줄에서 해시만 꺼낸다."""
    match = re.search(rf"^{variable}='(.+)'$", output, re.MULTILINE)
    assert match, output
    return match.group(1)


# 출력 줄의 해시가 그 비밀번호와 맞고, 출력 어디에도 비밀번호 원문이 없다.
def test_prints_a_verifiable_env_line_and_never_the_password():
    code, out, err = run("admin", PASSWORD, PASSWORD)
    hashed = hash_in(out, "LABOR_ADMIN_PASSWORD_HASH")
    assert code == 0 and looks_like_bcrypt(hashed) and verify_password(PASSWORD, hashed)
    assert PASSWORD not in out and PASSWORD not in err


# --account test는 LABOR_TEST_PASSWORD_HASH 줄만 출력한다.
def test_test_account_uses_its_own_variable_name():
    code, out, _ = run("test", PASSWORD, PASSWORD)
    assert code == 0 and "LABOR_TEST_PASSWORD_HASH='" in out and "LABOR_ADMIN_PASSWORD_HASH" not in out


# 출력한 줄을 그대로 .env에 붙여 넣으면 서버 설정이 그 해시를 읽는다(`$`가 들어 있어도 변하지 않는다). 이 줄이
# 사용자가 서버 .env에 넣는 바로 그 형식이므로, 설정 읽기까지 이어서 확인한다.
def test_output_line_survives_the_env_file_roundtrip(tmp_path, monkeypatch):
    for name in [n for n in os.environ if n.startswith("LABOR_")]:
        monkeypatch.delenv(name)  # PC에 설정된 환경 변수가 .env보다 먼저 읽히지 않게 가린다
    _, out, _ = run("admin", PASSWORD, PASSWORD)
    env_file = tmp_path / ".env"
    other = {k: v for k, v in settings_factory().model_dump().items() if k in {
        "law_api_oc", "gemini_api_key", "cohere_api_key", "opensearch_password",
        "test_password_hash", "session_secret", "langfuse_public_key", "langfuse_secret_key", "langfuse_host",
    }}
    lines = [f"LABOR_{k.upper()}={v}" for k, v in other.items()]
    env_file.write_text("\n".join([*lines, out.strip()]) + "\n", encoding="utf-8")
    settings = Settings(_env_file=env_file)
    assert verify_password(PASSWORD, settings.admin_password_hash)


# 두 번 입력한 값이 다르면 아무것도 출력하지 않고 실패한다(오타로 만든 해시를 쓰지 않게).
def test_mismatched_confirmation_fails_without_output():
    code, out, err = run("admin", PASSWORD, PASSWORD + "x")
    assert code == 1 and out == "" and "다릅니다" in err


# 빈 비밀번호는 실패 코드 1과 안내만 남기고 아무것도 출력하지 않는다.
def test_empty_password_is_rejected():
    code, out, err = run("admin", "", "")
    assert code == 1 and out == "" and err


# bcrypt는 72바이트까지만 읽는다. 한글 25자(75바이트)는 거절하고 이유를 알려 준다.
def test_password_over_72_bytes_is_rejected_with_the_reason():
    code, out, err = run("admin", "가" * 25, "가" * 25)
    assert code == 1 and out == "" and "72" in err


# 사용자가 test 비밀번호를 '1'처럼 짧게 정했다(위험은 사용자가 수용). 막지 않고 경고만 한다.
def test_short_password_is_allowed_with_a_warning():
    code, out, err = run("test", "1", "1")
    assert code == 0 and verify_password("1", hash_in(out, "LABOR_TEST_PASSWORD_HASH"))
    assert "짧" in err


# admin/test가 아닌 계정 이름은 인자 오류(종료 코드 2)다.
def test_unknown_account_is_an_argument_error():
    with pytest.raises(SystemExit) as excinfo:
        main(["--account", "root"], ask=lambda prompt: "x", out=io.StringIO(), err=io.StringIO())
    assert excinfo.value.code == 2
