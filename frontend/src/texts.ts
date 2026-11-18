// 화면에 늘 보이는 고정 문구와 입력 상한. 백엔드 app/texts.py·app/config.py와 **한 글자도 달라지면 안 된다**.
// 두 곳이 같은지는 파이썬 테스트(tests/test_frontend_texts.py)가 이 파일을 읽어 글자 단위로 확인한다.
// 출처 표기(SOURCE_NOTICE)는 법제처 이용안내상 의무이고, 틀리거나 빠지면 처벌 대상이 될 수 있다.

export const DISCLAIMER = "법률 자문이 아닙니다. 출처의 원문을 확인하세요";
export const SOURCE_NOTICE = "출처: 법제처 국가법령정보센터";
export const PII_NOTICE = "개인정보를 입력하지 마세요";

// 서버가 거절하는 질문 길이(Settings.max_question_chars)와 같다. 화면은 입력 단계에서 미리 막는다.
export const MAX_QUESTION_CHARS = 1000;
