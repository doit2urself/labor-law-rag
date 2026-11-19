"""배포된 서버를 밖(이 PC)에서 끝에서 끝까지 점검한다 (Task 18 Step 7).

사용법(사용자의 터미널, 프로젝트 루트에서):
    .venv\\Scripts\\python.exe scripts\\verify_deploy.py

무엇을 하나:
  1) /api/health로 서버와 OpenSearch가 살아 있는지 본다.
  2) `test` 계정으로 질문 7번을 보낸다. 5번째까지는 답이 나오고 6·7번째는 "횟수 초과"(429)로 막혀야 한다.
     그중 3번은 가짜 `X-Forwarded-For` 헤더를 붙인다. 서버가 이 헤더를 믿으면 횟수가 다른 주소로 세어져서 제한이 뚫린다.
  3) `admin` 계정으로 질문 7번을 보낸다. 횟수 제한이 없으므로 모두 답이 나와야 한다.
비밀번호는 화면에 보이지 않게(getpass) 받고 어디에도 저장·출력하지 않는다. 비워 두고 Enter를 치면 그 계정 점검은 건너뛴다.
서버 주소는 `--host`가 없으면 ssh 설정(`ssh -G contabo`)의 hostname을 읽는다. 출력에서는 주소를 `<ip>`로 가린다.

소모: 질문은 일부러 범위 밖 질문이다(분석 단계에서 거절되어 LLM 호출 1회). 그래도 `test` 계정의 **이 PC 주소 사용 횟수 5회는
소진된다**(누적이라 초기화되지 않는다). 시험이 끝나면 서버의 `ip_usage` 표를 비워 되돌린다.

종료 코드: 0 = 실행한 점검이 모두 합격, 1 = 불합격이 있음, 2 = 준비 오류(주소를 못 찾음 등).
"""

from __future__ import annotations

import argparse
import getpass
import re
import ssl
import subprocess
import sys
import time
from collections.abc import Sequence

# 범위 밖 질문(스펙 2절의 예). 서버가 분석 단계에서 거절하므로 LLM을 한 번만 쓴다.
QUESTION = "전세 보증금 돌려받는 법"

# 질문 7번의 계획. None = 헤더 없음, 문자열 = 그 값을 `X-Forwarded-For`로 위조해 붙인다.
# 위조값 3개는 서로 다르게 했다(같으면 "한 칸에 쌓인다"와 구별되지 않는다). 문서·예제용으로 예약된 대역(RFC 5737)이라 실제 누구의 주소도 아니다.
QUESTION_PLAN: list[str | None] = [None, None, "198.51.100.7", None, "198.51.100.8", None, "203.0.113.99"]


def parse_event_names(body: str) -> list[str]:
    """SSE 본문에서 이벤트 이름(`event: 이름` 줄)을 순서대로 뽑는다. 데이터 줄의 글자는 이름으로 읽지 않는다."""
    return re.findall(r"(?m)^event: (\w+)$", body)


def judge_test_limit(statuses: list[int], limit: int = 5) -> tuple[bool, str]:
    """`test` 계정 시험 결과를 판정한다: 앞의 `limit`번은 200, 나머지는 429여야 합격이다.

    불합격 이유를 세 갈래로 나눠 알려 준다. 이 판정이 틀리면 위조 헤더로 뚫린 서버를 "정상"이라고 말하게 된다.
    - 200·429 밖의 코드(로그인 실패 401, 서버 오류 500 등): 시험 자체가 성립하지 않았다.
    - 제한에 걸리기 전에 막힘: 이 주소의 횟수가 이미 쓰였다. 서버가 아니라 시험 조건의 문제다.
    - 제한 뒤에도 답이 나감: 횟수가 다른 주소로 세어졌다는 뜻이다. 위조 `X-Forwarded-For`를 서버가 믿는 것으로 의심한다.
    """
    expected = [200] * limit + [429] * (len(QUESTION_PLAN) - limit)
    if len(statuses) != len(expected):
        return False, f"응답 {len(statuses)}개를 받았습니다(기대 {len(expected)}개)."
    if statuses == expected:
        return True, f"앞의 {limit}번은 답변, 그 뒤는 횟수 초과로 막혔습니다. 위조 X-Forwarded-For 3번도 무시되었습니다."
    unexpected = sorted({code for code in statuses if code not in (200, 429)})
    if unexpected:
        return False, f"예상 밖의 응답 코드 {unexpected}가 있습니다(로그인 실패·서버 오류 등). 응답 목록: {statuses}"
    if 429 in statuses[:limit]:
        return False, f"{limit}번을 채우기 전에 미리 막혔습니다. 이 주소의 test 횟수가 이미 쓰였을 수 있습니다. 응답 목록: {statuses}"
    return False, (
        f"제한({limit}회) 뒤에도 답변이 나갔습니다. 서버가 위조된 X-Forwarded-For를 믿어 횟수를 다른 주소로 센 것으로 의심됩니다. "
        f"응답 목록: {statuses}"
    )


def judge_admin_run(statuses: list[int]) -> tuple[bool, str]:
    """`admin` 계정은 횟수 제한이 없다: 모든 질문이 200(답변)이어야 합격이다."""
    if statuses and all(code == 200 for code in statuses):
        return True, f"질문 {len(statuses)}번 모두 답변이 나왔습니다(제한 없음)."
    return False, f"막히거나 실패한 질문이 있습니다. 응답 목록: {statuses}"


def find_server_host() -> str:
    """ssh 설정의 `contabo` 별칭에서 서버 주소(hostname)를 읽는다. 못 찾으면 안내하고 끝낸다(종료 코드 2)."""
    try:
        result = subprocess.run(["ssh", "-G", "contabo"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SystemExit(f"ssh 설정을 읽지 못했습니다({type(error).__name__}). --host로 주소를 직접 주세요.") from error
    for line in result.stdout.splitlines():
        if line.startswith("hostname "):
            return line.split()[1]
    raise SystemExit("ssh 설정에서 hostname을 찾지 못했습니다. --host로 주소를 직접 주세요.")


def make_client(host: str):
    """서버에 HTTPS로 말을 거는 클라이언트. 인증서는 **끄지 않고** 검증한다.

    Windows 인증서 저장소로 검증한다(`truststore`). 파이썬에 딸린 인증서 묶음(certifi)에는 새로 생긴 Let's Encrypt 루트가
    없을 수 있어서다. 이 점검이 통과하면 "실제 사용자의 브라우저도 경고 없이 열 수 있다"는 뜻이다.
    """
    import httpx
    import truststore

    context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return httpx.Client(base_url=f"https://{host}", verify=context, timeout=150.0)


def _say(text: str, host: str) -> None:
    """출력한다. 서버 주소는 어디에 있든 `<ip>`로 가린다."""
    print(text.replace(host, "<ip>"), flush=True)


def _run_plan(client, host: str) -> list[int]:
    """질문 계획(QUESTION_PLAN)대로 질문을 보내고, 요청마다 응답 코드를 모아 돌려준다. 진행 상황은 한 줄씩 보여 준다."""
    statuses: list[int] = []
    for number, forged in enumerate(QUESTION_PLAN, start=1):
        headers = {"X-Forwarded-For": forged} if forged else {}
        started = time.monotonic()
        response = client.post("/api/chat", json={"question": QUESTION}, headers=headers)
        seconds = time.monotonic() - started
        statuses.append(response.status_code)
        if response.status_code == 200:
            names = parse_event_names(response.text)
            detail = "이벤트 " + ", ".join(sorted(set(names)))
        else:
            detail = response.text.strip()[:120]
        label = f"위조 {forged}" if forged else "헤더 없음"
        _say(f"  질문 {number} ({label}): HTTP {response.status_code}, {seconds:.1f}초 — {detail}", host)
    return statuses


def _check_account(host: str, username: str, judge) -> bool | None:
    """한 계정을 점검한다. 합격 True, 불합격 False, 건너뜀(비밀번호를 비워 둠) None."""
    password = getpass.getpass(f"{username} 계정 비밀번호(건너뛰려면 그냥 Enter): ")
    if not password:
        _say(f"{username} 점검을 건너뜁니다.", host)
        return None
    with make_client(host) as client:
        login = client.post("/api/login", json={"username": username, "password": password})
        if login.status_code != 200:
            _say(f"{username} 로그인 실패: HTTP {login.status_code} {login.text.strip()[:100]} → 비밀번호를 확인하세요.", host)
            return False
        _say(f"{username} 로그인 성공. 질문 {len(QUESTION_PLAN)}번을 보냅니다.", host)
        statuses = _run_plan(client, host)
        client.post("/api/logout")
    passed, message = judge(statuses)
    _say(f"  → {username} 점검 {'합격' if passed else '불합격'}: {message}", host)
    return passed


def main(argv: Sequence[str] | None = None) -> int:
    """점검을 실행한다. 종료 코드: 0 합격, 1 불합격이 있음, 2 준비 오류."""
    parser = argparse.ArgumentParser(description="배포된 서버를 밖에서 끝에서 끝까지 점검한다")
    parser.add_argument("--host", help="서버 주소(없으면 ssh 설정의 contabo 별칭에서 읽는다)")
    args = parser.parse_args(argv)
    host = args.host or find_server_host()

    _say("== 1) 상태 확인 (/api/health) ==", host)
    try:
        with make_client(host) as client:
            health = client.get("/api/health")
    except Exception as error:  # 인증서 오류·연결 실패 등 — 종류만 알려 준다(메시지에 주소가 있을 수 있어 가려서 출력)
        _say(f"서버에 연결하지 못했습니다: {type(error).__name__}: {error}", host)
        return 2
    _say(f"  HTTP {health.status_code} {health.text.strip()}", host)

    _say("== 2) test 계정: 5번 허용, 6·7번째 차단, 위조 헤더 무시 ==", host)
    results = [_check_account(host, "test", judge_test_limit)]
    _say("== 3) admin 계정: 제한 없음 ==", host)
    results.append(_check_account(host, "admin", judge_admin_run))

    ran = [r for r in results if r is not None]
    if any(r is False for r in ran):
        _say("결과: 불합격이 있습니다.", host)
        return 1
    _say(f"결과: 실행한 점검 {len(ran)}개 모두 합격." if ran else "결과: 실행한 점검이 없습니다.", host)
    return 0


if __name__ == "__main__":
    sys.exit(main())
