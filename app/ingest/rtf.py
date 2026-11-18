"""RTF에서 글을 뽑는 작은 해석기 (표준 라이브러리만 사용).

역할: 국가법령정보센터에서 "DOC"로 저장한 파일은 이름만 `.doc`이고 실제로는 RTF(서식 있는 텍스트)다
(첫 글자가 `{\\rtf1`, 만든 도구는 iText — docs/step0-report.md 4절). 여기서 본문 글을 문단 단위로 뽑는다.
사용처: app/ingest/parse_law.py. 입력: 파일 바이트. 출력: 비어 있지 않은 줄 목록(문단 하나 = 한 줄).

RTF를 아주 짧게 설명하면: 글은 그대로 쓰이고, 서식은 역슬래시로 시작하는 제어어(`\\par` = 문단 끝)로 적는다.
중괄호 `{ }`는 묶음(그룹)이고, 한글 같은 비ASCII 글자는 `\\u54620?`처럼 유니코드 번호로 적힌다(뒤의 `?`는
이 번호를 못 읽는 프로그램을 위한 대체 글자라서 건너뛴다).

이 해석기가 하는 일은 두 가지뿐이다: ① 글을 복원하고 ② 본문이 아닌 묶음(글꼴표, 그림 등)은 버린다.
글자 크기·굵기 같은 서식은 모두 무시한다.
"""

from __future__ import annotations

import re

_BS = "\\"

# 이 이름으로 시작하는 묶음은 본문이 아니다(글꼴 목록, 그림, 문서 정보 등). `{\*\...}`(무시 가능한 목적지)도 버린다.
_SKIP_GROUPS = frozenset(
    {"fonttbl", "colortbl", "stylesheet", "pict", "info", "listtable", "listoverridetable", "shppict", "nonshppict"}
)

_GROUP_START_RE = re.compile(r"(\\\*)?\\([a-zA-Z]+)")
_CONTROL_WORD_RE = re.compile(r"\\([a-zA-Z]+)(-?[0-9]+)? ?")  # 제어어 뒤의 공백 하나는 구분자라서 함께 삼킨다
_HEX_ESCAPE_RE = re.compile(r"\\'([0-9a-fA-F]{2})")

# 역슬래시 뒤에 기호 하나가 오는 이스케이프.
_SYMBOLS = {"\\": "\\", "{": "{", "}": "}", "~": " ", "_": "-", "-": ""}


def rtf_to_text(raw: bytes | str) -> str:
    """RTF를 글로 바꾼다. 문단은 줄바꿈(`\\n`)으로 나뉜다."""
    src = raw.decode("latin-1") if isinstance(raw, bytes) else raw  # RTF 제어 문자열은 ASCII라서 latin-1이면 안전하다
    out: list[str] = []
    pending = bytearray()  # `\'b0\'a1`처럼 두 바이트로 한 글자를 이루는 16진 이스케이프를 모아 두는 곳
    stack: list[bool] = []  # 묶음마다 "버리는 묶음인가"를 기억한다(묶음이 닫히면 바깥 상태로 돌아간다)
    skipping = False
    unicode_fallback = 1  # `\uN` 뒤에 건너뛸 대체 글자 수(`\ucN`으로 바뀔 수 있다)
    i, n = 0, len(src)

    def flush_pending() -> None:
        if pending:
            out.append(bytes(pending).decode("cp949", errors="replace"))
            pending.clear()

    while i < n:
        ch = src[i]
        if ch == "{":
            stack.append(skipping)
            i += 1
            m = _GROUP_START_RE.match(src, i)
            if m and (m.group(1) or m.group(2) in _SKIP_GROUPS):
                skipping = True
        elif ch == "}":
            skipping = stack.pop() if stack else False
            i += 1
        elif ch == _BS:
            m = _HEX_ESCAPE_RE.match(src, i)
            if m:
                if not skipping:
                    pending.append(int(m.group(1), 16))
                i = m.end()
                continue
            flush_pending()
            m = _CONTROL_WORD_RE.match(src, i)
            if m:
                word, arg = m.group(1), m.group(2)
                i = m.end()
                if word == "bin" and arg is not None:
                    i += int(arg)  # 이진 데이터(그림)는 묶음 안에서도 선언된 길이만큼 건너뛴다. 안 그러면 쓰레기 글자가 섞인다.
                elif skipping:
                    continue
                elif word in ("par", "line"):
                    out.append("\n")
                elif word == "tab":
                    out.append("\t")
                elif word == "uc" and arg is not None:
                    unicode_fallback = int(arg)
                elif word == "u" and arg is not None:
                    code = int(arg)
                    out.append(chr(code + 65536 if code < 0 else code))  # 32767을 넘는 글자는 음수로 적힌다
                    i += unicode_fallback
                continue
            # 역슬래시 뒤 기호 하나(`\{`, `\}`, `\\`, `\~` 등)
            if i + 1 < n:
                symbol = src[i + 1]
                if not skipping:
                    out.append("\n" if symbol in "\r\n" else _SYMBOLS.get(symbol, ""))
                i += 2
            else:
                i += 1
        else:
            if not skipping and ch not in "\r\n":  # 소스 안의 줄바꿈은 의미가 없다(문단은 `\par`가 정한다)
                flush_pending()
                out.append(ch)
            i += 1
    flush_pending()
    return "".join(out)


def rtf_to_lines(raw: bytes | str) -> list[str]:
    """RTF의 문단을 줄 목록으로 돌려준다. 줄 앞뒤 공백은 지우고 빈 줄은 뺀다."""
    return [line.strip() for line in rtf_to_text(raw).split("\n") if line.strip()]
