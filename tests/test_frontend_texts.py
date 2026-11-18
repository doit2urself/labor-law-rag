"""프런트엔드(frontend/src)가 백엔드와 같은 문구·같은 필드 이름을 쓰는지 확인한다 (Task 15).

왜 필요한가: 화면의 고정 문구(법제처 출처 표기 등)와 서버 응답의 모양은 두 언어(Python, TypeScript)로 따로 적혀 있다. 한쪽만 바뀌면
화면이 조용히 틀린 문구를 보이거나 필드를 못 읽는다. 이 파일이 TypeScript 소스를 읽어 백엔드의 값과 글자 단위로 맞춰 본다.
(TypeScript 파일을 정규식으로 읽는 단순한 방식이다. 파일 모양이 크게 바뀌면 이 테스트가 먼저 실패해서 알려 준다.)
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.config import Settings
from app.domain.models import GeneratedAnswer, SentenceOut
from app.graph.runner import answer_payload, refusal_payload
from app.texts import DISCLAIMER, PII_NOTICE, SOURCE_NOTICE
from tests.conftest import sample_hits

FRONTEND_SRC = Path(__file__).resolve().parent.parent / "frontend" / "src"


def ts_constant(name: str) -> str | int:
    """texts.ts의 `export const NAME = "문자열" | 숫자;` 값을 읽는다. 없으면 테스트를 실패시킨다."""
    source = (FRONTEND_SRC / "texts.ts").read_text(encoding="utf-8")
    match = re.search(rf'export const {name} = (?:"([^"]*)"|(\d+));', source)
    assert match, f"texts.ts에서 {name}을(를) 찾을 수 없습니다"
    return match.group(1) if match.group(1) is not None else int(match.group(2))


def ts_interface_fields(name: str) -> set[str]:
    """types.ts의 `export interface NAME { ... }` 안에 선언된 속성 이름을 읽는다."""
    source = (FRONTEND_SRC / "types.ts").read_text(encoding="utf-8")
    body = re.search(rf"export interface {name} \{{(.*?)\n\}}", source, re.DOTALL)
    assert body, f"types.ts에서 interface {name}을(를) 찾을 수 없습니다"
    return set(re.findall(r"^\s{2}(\w+)\??:", body.group(1), re.MULTILINE))


# 화면의 세 고정 문구와 입력 상한이 백엔드 값과 글자 단위로 같다(법제처 출처 표기는 틀리면 처벌 대상이 될 수 있다).
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("DISCLAIMER", DISCLAIMER),
        ("SOURCE_NOTICE", SOURCE_NOTICE),
        ("PII_NOTICE", PII_NOTICE),
        ("MAX_QUESTION_CHARS", Settings.model_fields["max_question_chars"].default),
    ],
)
def test_frontend_constants_match_the_backend(name, expected):
    assert ts_constant(name) == expected


# 화면이 읽는 답변(AnswerData)의 필드 이름이, 서버가 실제로 보내는 값(answer_payload 또는 거절 + message_id, thread_id)과 정확히 같다.
def test_answer_data_fields_match_what_the_server_sends():
    server_keys = set(refusal_payload("x")) | {"message_id", "thread_id"}
    assert ts_interface_fields("AnswerData") == server_keys


# 출처 카드가 읽는 필드(법령 정보 + id·label·url·text)가, 서버가 인용된 문서마다 보내는 필드와 같다.
def test_source_fields_match_what_the_server_sends():
    answer = GeneratedAnswer(sentences=[SentenceOut(text="문장", citation_ids=["doc1"], kind="evidence")])
    payload = answer_payload({"reranked": sample_hits(), "answer": answer, "qclass": "counsel"})
    server_source_keys = set(payload["sources"][0])
    frontend_keys = ts_interface_fields("LawSourceInfo") | {"id", "label", "url", "text"}
    assert frontend_keys == server_source_keys


# ---- 실제 서버 출력과 프런트 테스트 데이터의 일치 ----

SSE_FIXTURE = FRONTEND_SRC / "fixtures" / "chat-sse.txt"
UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


async def real_server_sse(tmp_path) -> str:
    """가짜 부품 그래프로 만든 진짜 API 앱에 질문을 보내 받은 SSE 본문을 돌려준다. 매번 달라지는 UUID는 자리표시자로 바꾼다."""
    from tests.api_support import login_cookie, make_app, post_chat
    from tests.conftest import ADMIN_PASSWORD

    app = make_app(tmp_path)
    cookie = await login_cookie(app, "admin", ADMIN_PASSWORD)
    response = await post_chat(app, cookie, "주휴수당이 뭐야?")
    assert response.status_code == 200
    return UUID_PATTERN.sub("<uuid>", response.text)


# frontend/src/fixtures/chat-sse.txt는 백엔드가 실제로 보낸 SSE 본문이다. 프런트 테스트(api.test.ts)가 이 파일을 읽어 streamChat이
# 실제 서버 형식을 해석하는지 확인한다. 이 테스트는 파일이 지금의 서버 출력과 같은지 확인해서, 서버 형식이 바뀌면 낡은 파일이 남지 않게 한다.
# 서버 출력을 일부러 바꿨다면 `UPDATE_FRONTEND_FIXTURE=1 pytest tests/test_frontend_texts.py`로 파일을 다시 만든다.
async def test_sse_fixture_matches_the_real_server_output(tmp_path):
    import os

    actual = await real_server_sse(tmp_path)
    if os.environ.get("UPDATE_FRONTEND_FIXTURE") == "1":
        SSE_FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        SSE_FIXTURE.write_text(actual, encoding="utf-8", newline="\n")
    assert SSE_FIXTURE.read_text(encoding="utf-8") == actual
