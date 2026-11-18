"""화면과 답변에 들어가는 고정 문구.

왜 한곳에 모았나: 이 문구들은 한 글자도 달라지면 안 된다.
- DISCLAIMER·PII_NOTICE: 서비스 성격(법률 자문 아님, 개인정보 주의)을 알리는 안내.
- SOURCE_NOTICE: 법제처 데이터를 쓸 때 출처 표기는 법제처 이용안내상 의무이고, 틀리면 처벌 대상이 될 수 있다.
프런트엔드(Task 15)도 같은 문자열을 쓰고, 테스트(tests/test_texts.py)가 글자 단위로 확인한다.
"""

DISCLAIMER = "법률 자문이 아닙니다. 출처의 원문을 확인하세요"
SOURCE_NOTICE = "출처: 법제처 국가법령정보센터"
PII_NOTICE = "개인정보를 입력하지 마세요"

# Gemini·Cohere의 무료 호출 한도가 소진됐을 때 사용자에게 보여 주는 문구(스택 트레이스는 절대 보이지 않는다).
QUOTA_MESSAGE = "오늘의 무료 사용 한도가 소진되었습니다. 나중에 다시 시도해 주세요"


def limit_exceeded_message(limit: int) -> str:
    """`test` 계정이 같은 IP에서 사용 횟수(limit)를 넘겼을 때의 안내 문구를 만든다."""
    return f"이 IP의 사용 횟수({limit}회)를 초과했습니다."
