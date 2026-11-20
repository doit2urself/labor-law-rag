# llm_portfolio — 프로젝트 지침

이 파일은 새 세션이 시작될 때마다 자동으로 읽힙니다. 짧게 유지합니다.
세션을 이어가는 방법은 한 줄입니다: `세션종료` → `/exit` 또는 `/clear` → 새 대화에서 `세션시작`.

## 프로젝트

- 근로·노동 상담 RAG 웹 서비스(LangGraph + OpenSearch + Gemini·Cohere·Langfuse Cloud 무료). 월 청구액 0원.
- 스펙(승인됨): `docs/superpowers/specs/2026-11-18-labor-rag-design.md`
- 계획(18 Task, Native 실행): `docs/superpowers/plans/2026-11-18-labor-rag-v1.md`
- 근거 자료: `docs/research/2026-11-17-rag-v1-design-review.md`
- 기억 파일(비밀 없음): 자동으로 읽히는 `MEMORY.md`가 가리키는 `llm-portfolio-rag-project.md`
- git: 18개 Task가 모두 끝나 `v1`은 `main`에 병합되고 삭제되었다(2026-11-20). 이후 변경은 `main`에서 새 브랜치를 따서 한다. 이 저장소 안에서 작성자는 `doit2urself`.
- 상태: 서버(Contabo)에 HTTPS로 배포되어 운영 중이다. 서버 기록은 `deploy/web-server-notes.md`, 측정은 `docs/results.md`, 판단 131건은 `docs/rulings.md`.
  원격 저장소를 만들거나 push하는 일은 사용자가 이름과 공개 여부를 말하기 전에는 하지 않는다.

## 항상 지킬 규칙

1. **한글 주석은 필수다 — 소스를 구현할 때마다 잊지 않는다.** 이 프로젝트는 포트폴리오만큼 **사용자의 학습**이 중요하다(사용자가 소스를 읽으며 배운다).
   - 코드 주석·docstring은 한글, 식별자·외부 API 이름은 영어.
   - 모듈 맨 위에 역할·사용처·입출력 docstring, 공개 함수·클래스마다 한글 docstring(무엇을/왜).
   - 개념이 나오는 곳(LangGraph 상태 초기화, RRF, Nori, 인용 검증 등)에는 "왜 이렇게 했는지"를 한글로 적는다. 코드가 이미 말하는 것을 되풀이하는 주석은 달지 않는다. 테스트 함수 위에는 무엇을 검증하는지 한글 한 줄.
   - 각 Task를 끝내면 5줄 이내의 한글 학습 메모(만든 파일, 읽는 순서, 핵심 개념 2~3개)를 남기고, 마지막에 `docs/code-map.md`를 만든다.
   - 커밋하기 전에 새로 쓴 소스에 한글 주석이 빠진 곳이 없는지 확인한다.
2. 스킬은 `superpowers:*`와 Claude Code 내장 스킬만 쓴다.
3. 비밀(API 키, 비밀번호, 서버 접속 정보)은 대화·파일·커밋에 적지 않는다.
4. 실행은 `superpowers:executing-plans`로 한다. 사용자 입력이 필요한 단계(🔑 🧑 🚦)에서는 멈춘다.
5. 서버(SSH) 작업은 명령마다 사용자 승인을 받는다.

## 세션종료

사용자가 `세션종료`라고 하면 이 순서로 한다.

1. 지금이 Task 경계인지 확인한다. 경계가 아니면 **테스트가 통과한 변경만** 커밋하고, 미완 변경은 커밋하지 않는다.
2. 원장(`.superpowers/sdd/2026-11-18-labor-rag-v1/progress.md`, 정확한 경로는 `sdd-workspace` 스크립트가 출력)에 기록한다.
   끝난 Task는 executing-plans 형식으로, 중간이면 `Task N: in progress — 다음: Step k, 미커밋 파일: …`.
3. 기억 파일의 "NEXT STEP"과 외부 상태(API 승인 여부, 키를 어디에 넣었는지(값 제외), 사용자가 정한 수치, 라벨 진행도)를 고친다.
4. 사용자에게 5줄 이내로 보고한다: 끝난 일, 다음 Task, 사용자가 미리 해 둘 일, "이제 `/exit` 또는 `/clear` 하셔도 됩니다".
5. push와 원격 저장소 생성은 하지 않는다.

## 세션시작

사용자가 `세션시작`(또는 "이어서 진행해줘")이라고 하면 이 순서로 한다.

1. 기억 파일 → 스펙 → 계획 순서로 읽는다. 계획 전체는 한 번만 읽고, Task를 시작할 때는 그 Task의 brief만 읽는다.
2. `git status`와 `git log --oneline -5`로 지금 브랜치가 무엇인지(v1은 병합·삭제되었으므로 보통 `main`), 미커밋 변경이 있는지 확인한다. 예상과 다르면 사용자에게 먼저 알린다.
3. `superpowers:executing-plans`를 호출해 원장에서 `Task N: complete`가 없는 첫 Task부터 이어간다. 기억과 원장·`git log`가 다르면 원장과 `git log`를 믿는다.
4. 그 Task가 사용자 준비물(🔑 🧑)을 필요로 하면 작업 전에 먼저 알린다. 모든 Task가 끝났으면 최종 검토 단계로 간다.

## 세션을 끊는 위치 (제안)

Task 경계에서만 끊는다. 관문(Task 2, 9, 17, 18)과 Task 7, 13 뒤가 자연스럽다. 컨텍스트 사용량은 수치로 볼 수 없어서 대략적인 기준이다.
