"""법령 글을 조문(필요하면 항) 단위 청크로 나눈다.

역할: RTF에서 뽑은 법령 글(app/ingest/rtf.py)을 읽어 `Chunk` 목록으로 바꾼다. 청크가 검색의 최소 단위다.
사용처: Task 7의 색인 파이프라인(scripts/build_index.py → app/ingest/indexer.py).
입력: 법령 글(머리글 + 본문). 출력: 현행 조문 청크 목록.

파일 하나의 모양(docs/step0-report.md 4절)
    법제처 국가법령정보센터근로기준법                      ← 머리글(잡음)
    [시행 2026. 10. 8.] [법률 제21533호, 2026. 4. 7., 일부개정]   ← 시행일자·공포번호·공포일자·개정구분
    고용노동부(근로기준정책과) 044-202-7534                ← 담당 부서(잡음)
    제1장 총칙
    제1조(목적) 이 법은 …                                  ← 조문. 항(①), 호(1.), 목은 같은 조문 안의 줄이다
    …
    부칙 <제21857호,…>                                     ← 부칙(다른 법률 개정 부칙 포함): 청크로 만들지 않는다

**시행예정 조문을 거르는 규칙 (이 파서에서 가장 중요한 부분)**
국가법령정보센터의 "현행" 파일 안에는 앞으로 시행될 조문이 같이 들어 있다(근로기준법에 19곳). 이 파일을 그대로 쓰면
"노동감독관"처럼 아직 시행되지 않은 문구가 현행 조문처럼 검색된다. 파일에서 확인한 규칙은 다음과 같다.
1. 같은 조문번호가 두 번 나오면 **첫 번째가 현행**, 뒤는 시행예정 버전이다(`제13조`, `제54조`, `제101조`, `제116조`).
2. 시행예정 블록의 끝에는 `[시행일: 2026. 12. 8.] 제13조` 표시가 붙는다. 조문 번호가 **하나뿐인데** 조 전체를 가리키는
   표시(`제44조의4`)가 붙으면 아직 시행 전인 신설 조문이다 → 뺀다.
3. `제116조제1항제2호`, `제60조제8항`처럼 **일부만** 가리키는 표시는 조문 자체가 현행이므로 조문을 빼지 않는다.
   (이때 현행 블록에 시행 전 항·호가 섞여 있을 수 있다 — v1에서는 받아들이는 한계다.)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from app.domain.models import Chunk
from app.ingest.rtf import rtf_to_lines

HEADER_PREFIX = "법제처 국가법령정보센터"  # 머리글 줄 앞에 붙는 사이트 이름. 법령명 앞에서 떼어 낸다.

# [시행 2026. 10. 8.] [법률 제21533호, 2026. 4. 7., 일부개정]
_META_RE = re.compile(
    r"^\[시행\s+(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.\]\s*"
    r"\[(\S+)\s+제(\d+)호,\s*(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.,\s*(.+?)\]"
)
# 조문 제목: `제1조(목적)`, `제1조 (목적)`(공백 있음), `제43조의2(…)`, 삭제된 조문 `제12조 삭제 <…>`.
_ARTICLE_RE = re.compile(r"^제(\d+)조(?:의(\d+))?\s*(?:\((.+?)\)|(삭제))")
_CHAPTER_RE = re.compile(r"^제(\d+)(장|절|관)(?:의\d+)?\s+\S")
_MARKER_RE = re.compile(r"^\[시행일:\s*\d{4}\.\s*\d{1,2}\.\s*\d{1,2}\.\]\s*(.*)$")
_WHOLE_ARTICLE_RE = re.compile(r"제(\d+)조(?:의(\d+))?")  # 마커가 조 전체를 가리키는 경우(`제44조의4`)
_HEADING_SPLIT_RE = re.compile(r"^(제\d+조(?:의\d+)?\s*\(.+?\))\s*(.*)$", re.S)

# 개정 이력 표기: 본문 안의 `<개정 2018. 3. 20.>`, `삭제<2009. 5. 21.>`와 줄 전체인 `[제목개정 …]`, `[본조신설 …]` 등.
# 검색·임베딩에는 잡음이라 지운다. 날짜만 적힌 `<2009. 5. 21.>`도 이력이다.
_INLINE_NOTE_RE = re.compile(
    r"\s*<(?:(?:개정|신설|전문개정|본조신설|제목개정|종전|삭제|일부개정)[^>]*|\d{4}\.[^>]*)>"
)
_NOTE_LINE_RE = re.compile(
    r"^(?:\[(?:본조신설|제목개정|전문개정|본조삭제|종전)[^\]]*\]"
    r"|\[(?:법률|대통령령|고용노동부령|노동부령|총리령) 제[^\]]*\]"
    r"|<[^>]*>)$"
)
_APPENDIX_RE = re.compile(r"^\[(?:별표|별지)")  # 별표·서식은 v1 범위 밖이라 거기서부터 다음 조문까지 버린다.


def article_url(law_name: str, article_no: str) -> str:
    """조문의 법제처 원문 주소. `article_no`는 `"55"` 또는 가지 조문 `"55의2"`.

    형식 `https://www.law.go.kr/법령/<법령명>/제<n>조`는 한 건에서 동작을 확인했다(스펙 6.5). 법령명에 공백이 있을 때
    `%20`로 바꿔도 되는지는 확인하지 못했다(미확인).
    """
    return f"https://www.law.go.kr/법령/{law_name.replace(' ', '%20')}/{_article_label(article_no)}"


def _article_label(article_no: str) -> str:
    """`"55"` → `"제55조"`, `"55의2"` → `"제55조의2"`."""
    main, _, branch = article_no.partition("의")
    return f"제{main}조의{branch}" if branch else f"제{main}조"


def _circled_to_int(ch: str) -> int | None:
    """항 번호 기호(①~㊿)를 숫자로. 항 기호가 아니면 None."""
    code = ord(ch)
    if 0x2460 <= code <= 0x2473:  # ①~⑳
        return code - 0x2460 + 1
    if 0x3251 <= code <= 0x325F:  # ㉑~㉟
        return code - 0x3251 + 21
    if 0x32B1 <= code <= 0x32BF:  # ㊱~㊿
        return code - 0x32B1 + 36
    return None


def _iso(year: str, month: str, day: str) -> str:
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


@dataclass
class _Block:
    """파일에서 만난 조문 한 덩어리. 같은 조문번호의 덩어리가 여러 개일 수 있다(현행 + 시행예정)."""

    number: str  # "55" 또는 "55의2"
    heading: str  # 제목(괄호 안). 삭제된 조문은 빈 문자열
    deleted: bool
    chapter: str | None
    lines: list[str] = field(default_factory=list)  # 첫 줄이 조문 제목 줄


def parse_law_file(path: str | Path, max_chars: int = 6000) -> list[Chunk]:
    """`.doc`(RTF) 법령 파일 하나를 청크 목록으로 읽는다."""
    return parse_law_text("\n".join(rtf_to_lines(Path(path).read_bytes())), max_chars)


def parse_law_text(text: str, max_chars: int = 6000) -> list[Chunk]:
    """법령 글 하나를 현행 조문 청크로 나눈다.

    max_chars: 조문이 이 길이를 넘으면 항 단위로 나눈다. 6000은 임베딩 입력 한도(8,192토큰)를 넘지 않게 잡은
    보수적 시작값이며 평가셋으로 조정한다(계획 Task 4).
    """
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    meta_at = next((i for i, ln in enumerate(lines) if _META_RE.match(ln)), None)
    if meta_at is None:
        raise ValueError("머리글에서 `[시행 …] [법률 제N호, …]` 줄을 찾지 못했습니다. 법령 파일이 맞는지 확인하세요.")
    m = _META_RE.match(lines[meta_at])
    assert m is not None
    law_name = _law_name(lines, meta_at)
    effective_date = _iso(m.group(1), m.group(2), m.group(3))
    law_kind, promulgation_no = m.group(4), m.group(5)
    promulgation_date = _iso(m.group(6), m.group(7), m.group(8))
    amendment = m.group(9)
    # 법령ID가 없으므로 (법령명, 공포번호, 시행일자)로 원문 ID를 만든다. 개정되면 시행일자가 달라져 ID가 바뀌고,
    # 다시 색인할 때 옛 버전 청크가 "이번 입력에 없는 원문"으로 지워진다(Task 7).
    source_id = f"law:{law_name}:{promulgation_no}:{effective_date.replace('-', '')}"

    blocks, whole_future = _collect_blocks(lines[meta_at + 1 :])

    by_number: dict[str, list[_Block]] = {}
    for block in blocks:
        by_number.setdefault(block.number, []).append(block)

    chunks: list[Chunk] = []
    for number, group in by_number.items():
        first = group[0]  # 규칙 1: 같은 번호가 여럿이면 첫 번째가 현행
        if first.deleted:
            continue
        if len(group) == 1 and number in whole_future:
            continue  # 규칙 2: 시행 전 신설 조문
        chunks.extend(
            _block_to_chunks(first, law_name, source_id, effective_date, promulgation_date,
                             {"law_kind": law_kind, "promulgation_no": promulgation_no, "amendment": amendment},
                             max_chars)
        )
    return chunks


def _law_name(lines: list[str], meta_at: int) -> str:
    """머리글에서 법령명을 읽는다. `법제처 국가법령정보센터근로기준법`처럼 사이트 이름이 앞에 붙어 있다."""
    for line in lines[:meta_at]:
        if line.startswith(HEADER_PREFIX):
            return line[len(HEADER_PREFIX) :].strip()
    return lines[meta_at - 1] if meta_at > 0 else ""


def _collect_blocks(body: list[str]) -> tuple[list[_Block], set[str]]:
    """본문 줄을 조문 덩어리로 묶고, 조 전체를 가리키는 시행일 표시의 조문번호를 모은다."""
    blocks: list[_Block] = []
    whole_future: set[str] = set()
    chapter: str | None = None
    chapter_no: str | None = None
    block: _Block | None = None

    for line in body:
        if line.startswith("부칙"):
            break  # 부칙 이후(다른 법률 개정 부칙 포함)는 청크로 만들지 않는다.
        m = _CHAPTER_RE.match(line)
        if m:
            block = None
            # 장 제목이 현행/시행예정 두 가지로 나오면 첫 번째를 쓴다(같은 장 번호는 처음 것만 기록).
            if m.group(2) == "장" and m.group(1) != chapter_no:
                chapter_no, chapter = m.group(1), line
            continue  # 절·관 제목 줄은 조문 본문에 섞이지 않게 버린다.
        m = _MARKER_RE.match(line)
        if m:
            block = None
            for part in m.group(1).split(","):
                whole = _WHOLE_ARTICLE_RE.fullmatch(part.strip())
                if whole:
                    whole_future.add(whole.group(1) + (f"의{whole.group(2)}" if whole.group(2) else ""))
            continue
        if _APPENDIX_RE.match(line):
            block = None
            continue
        m = _ARTICLE_RE.match(line)
        if m:
            number = m.group(1) + (f"의{m.group(2)}" if m.group(2) else "")
            block = _Block(number=number, heading=m.group(3) or "", deleted=bool(m.group(4)), chapter=chapter, lines=[line])
            blocks.append(block)
            continue
        if block is not None and not _NOTE_LINE_RE.match(line):
            block.lines.append(line)
    return blocks, whole_future


def _block_to_chunks(
    block: _Block,
    law_name: str,
    source_id: str,
    effective_date: str,
    promulgation_date: str,
    base_extra: dict,
    max_chars: int,
) -> list[Chunk]:
    """조문 덩어리 하나를 청크 하나(짧을 때) 또는 항 단위 여러 개(길 때)로 만든다."""
    cleaned = [_INLINE_NOTE_RE.sub("", ln).strip() for ln in block.lines]
    cleaned = [ln for ln in cleaned if ln]
    full_text = "\n".join(cleaned)

    label = _article_label(block.number)
    title = f"{law_name} {label}({block.heading})" if block.heading else f"{law_name} {label}"
    url = article_url(law_name, block.number)
    extra = {**base_extra, "chapter": block.chapter}

    def make(paragraph: str | None, text: str) -> Chunk:
        return Chunk(
            chunk_id=f"{source_id}:{block.number}:{paragraph or 0}",
            doc_type="law",
            title=title,
            text=text,
            law_name=law_name,
            article_no=block.number,
            paragraph=paragraph,
            effective_date=effective_date,
            promulgation_date=promulgation_date,
            source_id=source_id,
            source_url=url,
            extra=dict(extra),
        )

    paragraphs = _split_paragraphs(cleaned)
    if len(full_text) <= max_chars or len(paragraphs) <= 1:
        return [make(None, full_text)]

    # 길면 항(①, ②, …) 단위로 나눈다. 조각만 따로 검색되어도 어느 조문인지 알 수 있게 각 조각 앞에 조문 제목을 붙인다.
    prefix = _HEADING_SPLIT_RE.match(cleaned[0])
    heading_prefix = prefix.group(1) if prefix else cleaned[0]
    chunks, used = [], set()
    for index, (number, body) in enumerate(paragraphs):
        key = str(number) if number is not None else None
        if key in used:  # 같은 항 번호가 두 번 나와도 chunk_id가 겹치지 않게 한다.
            key = f"{key}-{index}"
        used.add(key)
        chunks.append(make(key, f"{heading_prefix} {body}".strip()))
    return chunks


def _split_paragraphs(cleaned: list[str]) -> list[tuple[int | None, str]]:
    """정리된 조문 줄들을 항 단위로 묶는다. 호(`1.`)·목 줄은 앞의 항에 이어 붙는다.

    조문 제목 줄 자체가 `제2조(정의) ① …`처럼 첫 항을 담고 있어서, 제목 부분을 떼고 나머지를 첫 항으로 본다.
    """
    paragraphs: list[list] = []  # [항 번호 또는 None, 본문]
    head = _HEADING_SPLIT_RE.match(cleaned[0])
    rest = head.group(2) if head else ""
    if rest:
        paragraphs.append([_circled_to_int(rest[0]), rest])
    for line in cleaned[1:]:
        number = _circled_to_int(line[0])
        if number is not None:
            paragraphs.append([number, line])
        elif paragraphs:
            paragraphs[-1][1] += "\n" + line
        else:
            paragraphs.append([None, line])
    return [(p[0], p[1]) for p in paragraphs]
