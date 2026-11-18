"""근로·노동 상담 RAG 서비스의 애플리케이션 패키지.

하위 패키지 안내(읽는 순서 추천은 Task 18의 docs/code-map.md에 정리한다):
- domain: 데이터 모델과 예외 (다른 모든 곳이 가져다 쓰는 가장 바닥 층)
- ports.py: 외부와 만나는 부분의 인터페이스(포트). 구현체(어댑터)는 adapters/에 둔다.
- config.py, texts.py, scope.py: 설정, 화면 고정 문구, 수집 대상 법령 목록
"""
