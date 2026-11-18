"""RTF 변환기(app/ingest/rtf.py)를 검증한다.

국가법령정보센터의 `.doc` 저장 파일은 실제로는 RTF다. 한글은 `\\u<숫자>?` 유니코드 이스케이프로 들어 있고,
머리·바닥글에는 그림(이진 데이터)이 들어 있어서 그림을 건너뛰지 못하면 글 속에 쓰레기 글자가 섞인다.
테스트용 RTF는 문자열로 직접 만든다(저작물인 실제 파일을 저장소에 넣지 않는다).
"""

from app.ingest.rtf import rtf_to_lines

BS = "\\"


def uni(text: str) -> str:
    """한글을 RTF 유니코드 이스케이프(`\\u번호?`)로 바꾼다. 법제처 파일과 같은 방식이다."""
    return "".join(f"{BS}u{ord(ch)}?" if ord(ch) > 127 else ch for ch in text)


def make_rtf(*paragraphs: str, extra_header: str = "") -> bytes:
    body = "".join(f"{BS}pard {uni(p)}{BS}par " for p in paragraphs)
    return ("{" + BS + "rtf1" + BS + "ansi" + BS + "ansicpg1252 " + extra_header + body + "}").encode("latin-1")


# 문단(\par)마다 한 줄이 되고, 한글이 유니코드 이스케이프에서 복원된다.
def test_paragraphs_become_lines_and_korean_is_decoded():
    assert rtf_to_lines(make_rtf("제1조(목적) 이 법은 근로조건을 정한다.", "제2조(정의) ...")) == [
        "제1조(목적) 이 법은 근로조건을 정한다.",
        "제2조(정의) ...",
    ]


# 글꼴표·색표·스타일시트 같은 설정 그룹의 글자는 본문에 섞이지 않는다.
def test_font_table_and_stylesheet_groups_are_skipped():
    header = "{" + BS + "fonttbl{" + BS + "f0 Times New Roman;}}{" + BS + "stylesheet {" + BS + "s0 Normal;}}"
    assert rtf_to_lines(make_rtf("본문", extra_header=header)) == ["본문"]


# 그림 그룹 안의 이진 데이터(중괄호·역슬래시 모양 바이트 포함)를 길이만큼 건너뛴다. 못 건너뛰면 쓰레기 글자가 섞인다.
def test_picture_binary_is_skipped_by_declared_length():
    picture = "{" + BS + "*" + BS + "shppict{" + BS + "pict" + BS + "jpegblip" + BS + "bin5 }{" + BS + "u1?}}"
    assert rtf_to_lines(make_rtf("앞", extra_header=picture)) == ["앞"]


# 16진 이스케이프(\'b0\'a1)는 cp949로 읽는다(옛 한글 RTF 방식). `가`는 cp949에서 b0 a1이다.
def test_hex_escape_is_decoded_as_cp949():
    raw = ("{" + BS + "rtf1" + BS + "ansi " + BS + "'b0" + BS + "'a1" + BS + "par }").encode("latin-1")
    assert rtf_to_lines(raw) == ["가"]


# 이스케이프된 중괄호와 역슬래시는 글자로 남는다.
def test_escaped_braces_and_backslash_are_literal():
    raw = ("{" + BS + "rtf1 a" + BS + "{b" + BS + "}c" + BS + BS + "d" + BS + "par }").encode("latin-1")
    assert rtf_to_lines(raw) == ["a{b}c" + BS + "d"]


# 빈 문단은 줄로 내지 않고, 줄 앞뒤 공백은 지운다.
def test_blank_paragraphs_are_dropped_and_lines_are_trimmed():
    assert rtf_to_lines(make_rtf("  앞뒤 공백  ", "", "  ")) == ["앞뒤 공백"]


# 음수 유니코드 값(32767 초과 문자)은 65536을 더해 복원한다.
def test_negative_unicode_value_is_wrapped():
    raw = ("{" + BS + "rtf1 " + BS + "u-10179?" + BS + "par }").encode("latin-1")  # -10179 + 65536 = 55357
    assert rtf_to_lines(raw) == [chr(55357)]
