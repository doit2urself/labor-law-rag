# 코드 지도 — 처음 읽는 사람을 위한 안내

이 프로젝트는 "근로 문제를 물으면, 법령 조문을 찾아서 출처와 함께 설명해 주는" 웹 서비스입니다.
이 문서는 소스를 읽으며 배우려는 사람을 위한 지도입니다. 모든 설명은 실제 파일을 열어 확인한 내용이고, 파일 이름 옆에 근거 위치를 적었습니다.

## 1. 질문 하나가 지나가는 길

동네 도서관에 비유하면 이렇습니다. 사서(그래프)가 질문을 듣고 → 책장에서 후보를 꺼내 → 가장 알맞은 몇 권만 고르고 → 그 책에 적힌 내용만으로 답을 쓰고 → 다른 사서가 "이 문장이 정말 그 책에 있나요?" 하고 확인한 뒤에야 손님에게 내보냅니다.

```
[브라우저]  ChatView.tsx → api.ts(streamChat)         질문을 POST /api/chat 으로 보낸다
     │
[웹서버]    Caddy (deploy/Caddyfile)                  /api/* 는 8000번으로 넘기고, 나머지는 화면 파일을 준다
     │
[API]       app/api/routes.py  chat()
     │         ① 로그인 확인  ② 질문 검증(빈 질문·1,000자 초과는 422)
     │         ③ test 계정이면 IP별 사용 횟수 소비(초과 시 429)  ④ run_chat 실행
     │
[그래프]    app/graph/runner.py run_chat → build.py 의 그래프
     │
     │   analyze ─ 질문을 검색용 문장으로 다시 쓰고, 4가지로 분류한다 (nodes_analyze.py, LLM 1회)
     │      │      범위 밖·수집 범위 밖이면 곧바로 refuse(거절 안내)로 간다
     │      ▼
     │   retrieve ─ 질문을 숫자(벡터)로 바꾸고(Gemini 임베딩) OpenSearch에 하이브리드 검색 → 후보 20개
     │      ▼        (nodes_search.py → adapters/gemini.py, adapters/opensearch_store.py)
     │   rerank ─ 후보 20개를 Cohere로 다시 줄 세워 상위 5개. 월 한도·오류면 RRF 순위 그대로 상위 5개
     │      ▼        (retrieval/rerank_gate.py, adapters/cohere_rerank.py)
     │   sufficiency ─ 근거가 충분한가? 리랭크 1위 점수 ≥ 기준(0.15)이면 충분 (nodes_search.py)
     │      │      부족하면 refine(검색어 바꾸기) → retrieve 로 되돌아간다(최대 2회)
     │      ▼
     │   generate ─ 5개 조문을 "데이터"로 넣고 LLM이 문장마다 인용 번호를 붙여 쓴다 (nodes_answer.py, LLM 1회)
     │      ▼
     │   check ─ 코드 검사: 없는 인용 번호? 근거 문장인데 인용이 없나? (check_citations, LLM 호출 없음)
     │      ▼
     │   verify ─ 다른 LLM 호출이 "각 문장이 인용한 조문으로 뒷받침되는가"를 확인 (nodes_verify.py, LLM 1회)
     │      ▼
     │   decide ─ 통과 → finalize / 다시 쓰기 → generate (최대 2회) / 다시 찾기 → refine / 거절 → refuse
     │
[API]       routes.py 가 그래프 이벤트를 SSE 로 바꿔 흘려보낸다 (progress, detail, delta, answer, error)
     │
[브라우저]  sse.ts(해석) → chatState.ts(상태 계산) → AnswerView.tsx · SourceCard.tsx
```

- 정상 경로는 LLM 호출 3번(분석, 생성, 검증)입니다. 평가에서 질문당 평균 2.95회였습니다(`docs/results.md`).
- 어느 경로든 질문당 LLM 호출은 8번을 넘지 못합니다(`app/graph/budget.py`, 설정 `max_llm_calls`).
- 검증을 통과하기 전의 초안은 화면으로 나가지 않습니다. 화면에 나가는 글은 `finalize`와 `refuse` 두 노드만 보냅니다(`build.py`의 `_emit_final`).

## 2. 파일별 역할 (한 줄)

### 서버 본체 `app/`

| 파일 | 역할 |
|---|---|
| `config.py` | 모델 이름·호출 한도·서버 주소 같은 설정. `.env`와 환경 변수(접두어 `LABOR_`)에서 읽는다 |
| `texts.py` | 화면과 답변에 들어가는 고정 문구("법률 자문이 아닙니다" 등) |
| `scope.py` | v1이 다루는 법령 목록 |
| `ports.py` | 외부 서비스와 만나는 부분의 "약속"(인터페이스). 로직은 이 약속만 안다 |
| `container.py` | 설정으로 진짜 부품(Gemini·OpenSearch·Cohere)을 만들어 그래프에 끼우는 유일한 곳 |
| `domain/models.py`, `domain/errors.py` | 서비스 전체가 주고받는 데이터 모양과 예외 |
| `adapters/gemini.py` | 임베딩(`GeminiEmbedder`)과 대화 모델(`GeminiLlm`) |
| `adapters/opensearch_store.py` | 색인 매핑, RRF 파이프라인, 하이브리드 검색 쿼리, `OpenSearchStore` |
| `adapters/cohere_rerank.py` | 후보를 질문과의 관련성으로 다시 줄 세우기 |
| `adapters/sqlite_store.py` | 계정, IP별 사용 횟수, 달별 리랭크 호출 수 |
| `adapters/law_api.py` | 법제처 Open API 클라이언트(v1은 쓰지 않는다. 파일로 수집했다) |
| `retrieval/rerank_gate.py` | 리랭크 월 호출 한도(950회) 문지기. 한도가 차도 답변은 계속 나간다 |
| `ingest/rtf.py`, `ingest/parse_law.py` | 법령 파일(RTF)에서 글을 뽑아 조문 단위 조각(청크)으로 나눈다 |
| `ingest/indexer.py` | 청크를 임베딩해서 OpenSearch 색인에 넣는다 |
| `graph/state.py` | 그래프 노드들이 주고받는 상태와, 매 턴 시작 때 쓰는 초기화 값 |
| `graph/build.py` | 노드를 연결해 그래프를 만든다. 진행 이벤트도 여기서 낸다 |
| `graph/nodes_analyze.py` | 질문 분석(재작성 + 분류) |
| `graph/nodes_search.py` | 검색 → 리랭크 → 충분성 판단 → 검색어 다시 만들기, 그리고 갈림길 |
| `graph/nodes_answer.py` | 답변 생성, 인용 코드 검사 |
| `graph/nodes_verify.py` | LLM 근거 검증, 통과·재생성·재검색·거절 결정, 거절 문구 |
| `graph/runner.py` | 질문 한 턴을 돌리고 화면으로 보낼 이벤트만 걸러 내보낸다 |
| `graph/budget.py`, `graph/deps.py` | LLM 호출 예산 계산, 노드가 쓰는 부품 묶음 |
| `api/main.py` | FastAPI 앱 만들기. 시작할 때 설정을 검사한다 |
| `api/routes.py` | 로그인·채팅(SSE)·피드백·`/api/health` |
| `api/auth.py` | 비밀번호 해시, 세션 쿠키, 현재 사용자 찾기 |
| `api/limits.py` | 접속자 IP 읽기(위조된 헤더 무시), 로그인 시도 제한 |
| `observability/langfuse_setup.py`, `observability/masking.py` | Langfuse 기록, 개인정보(주민번호·전화·이메일) 가리기 |

### 도구 `scripts/`, 평가 `eval/`

| 파일 | 역할 |
|---|---|
| `scripts/build_index.py` | 법령 파일을 읽어 임베딩하고 OpenSearch에 넣는다 |
| `scripts/make_password_hash.py` | 로그인 비밀번호의 bcrypt 해시를 `.env`에 붙일 한 줄로 만든다 |
| `scripts/prune_checkpoints.py` | 오래된 대화의 체크포인트를 지운다 |
| `scripts/step0_probe.py`, `scripts/collect.py`, `scripts/envfile.py` | 무료 한도 측정, API 수집(쓰지 않음), `.env` 읽기 |
| `eval/evalset.jsonl`, `eval/evalset_hard.jsonl` | 평가 질문 40개, 어려운 거절 질문 13개 |
| `eval/run_retrieval.py` | 검색 품질(Recall@k, MRR) 측정 |
| `eval/run_eval.py` | 서비스 전체를 끝까지 돌려 답변 단계 지표 측정 |
| `eval/metrics.py`, `eval/report.py`, `eval/labels.py`, `eval/drafts.py` | 지표 계산, 리포트 만들기, 사람 검수 시트, 평가셋 초안 |

### 화면 `frontend/src/`

| 파일 | 역할 |
|---|---|
| `main.tsx`, `App.tsx` | 시작점. 로그인 여부에 따라 로그인 화면이나 채팅 화면을 보여 준다 |
| `api.ts`, `sse.ts` | 서버 호출, 서버가 조금씩 보내는 이벤트 글자를 이어 붙여 해석 |
| `chatState.ts` | 이벤트 하나가 오면 "질문 한 턴"의 상태를 어떻게 바꿀지 계산하는 순수 함수 |
| `components/` | `LoginForm`, `ChatView`, `AnswerView`, `SourceCard`, `Notices` |
| `texts.ts`, `types.ts`, `format.ts` | 고정 문구(백엔드와 한 글자도 달라서는 안 됨), 서버와 주고받는 값의 모양, 출처 이름 만들기 |

### 배포 `deploy/`

`labor-rag.service`(API를 실행하는 systemd 유닛), `Caddyfile`(웹서버 설정), `web-server-notes.md`(웹서버·인증서 기록), `opensearch-notes.md`(OpenSearch 설치 기록), `api-notes.md`(API 실행 방식과 이유).

## 3. 읽는 순서 추천

1. `app/texts.py` → `app/domain/models.py` → `app/ports.py` — 어떤 말과 어떤 모양의 데이터가 오가는지 먼저 안다. 짧다.
2. `app/graph/state.py` → `app/graph/build.py` — 질문이 지나가는 길 전체를 본다. 위 그림과 `build.py` 위쪽 도식을 같이 보면 좋다.
3. `app/graph/nodes_analyze.py` → `nodes_search.py` → `nodes_answer.py` → `nodes_verify.py` — 길 위의 정거장을 순서대로.
4. `app/adapters/opensearch_store.py` — 검색이 실제로 어떻게 만들어지는지(Nori, RRF).
5. `app/api/routes.py` → `app/api/limits.py` — 바깥 세상과 만나는 곳(로그인, 횟수 제한, SSE).
6. `frontend/src/sse.ts` → `chatState.ts` → `components/AnswerView.tsx` — 서버가 흘려보낸 이벤트가 화면이 되는 과정.
7. 마지막으로 `tests/`와 `eval/` — 테스트 함수 위의 한 줄 설명이 "이 코드가 무엇을 약속하는지"를 알려 준다.

## 4. 핵심 개념 세 가지

### (1) RRF — 점수가 아니라 "순위"로 합치기

한 질문을 세 가지 방식으로 검색합니다: 글자로 찾기(BM25, 한국어 형태소 분석기 Nori), 조문번호·법령명 정확 일치, 뜻으로 찾기(벡터). 문제는 세 방식의 점수 단위가 서로 달라서 그냥 더할 수 없다는 점입니다. 키 180cm와 몸무게 70kg을 더하는 것과 같습니다.

RRF(Reciprocal Rank Fusion)는 점수를 버리고 **순위**만 씁니다. 각 목록에서 r등이면 `1 / (60 + r)`점을 주고 목록마다 받은 점수를 더합니다. 여러 방식에서 위에 오른 조문이 위로 올라옵니다. 60은 `rank_constant`입니다. 이 계산은 우리 코드가 아니라 OpenSearch 검색 파이프라인이 합니다(`opensearch_store.py`의 `build_rrf_pipeline_body`).

### (2) 턴마다 상태 초기화 — "지난 질문의 흔적"이 남지 않게

같은 대화로 다시 질문하면 LangGraph는 저장해 둔 지난 상태를 불러와 그 위에 새 입력을 덮습니다. 새 입력에 적지 않은 값은 **지난 값 그대로** 남습니다. 지난 질문에서 "다시 찾기"를 2번 썼다면, 새 질문이 처음부터 "더 못 찾음" 상태로 시작하는 사고가 납니다.

그래서 매 턴의 첫 입력에 카운터와 검색 결과를 모두 비우는 값을 넣습니다(`state.py`의 `reset_turn_update`). 대화 이력(`messages`)만 일부러 남겨서 "그럼 알바는요?" 같은 이어지는 질문을 이해합니다. 쌓아 가는 규칙이 붙은 필드(`tried_queries`)는 `[]`를 넣어도 지워지지 않아서 `Overwrite([])`를 씁니다.

### (3) 인용 검증 — 두 겹의 확인

- **1겹, 코드가 확인한다** (`nodes_answer.py`의 `check_citations`): 문서 목록에 없는 번호를 인용했는가(지어낸 출처), 근거 문장인데 인용이 하나도 없는가, 문장이 하나도 없는가. LLM을 부르지 않아 빠르고 틀리지 않는다.
- **2겹, 다른 LLM 호출이 확인한다** (`nodes_verify.py`의 `verify_node`): 각 문장이 인용한 조문의 내용으로 정말 뒷받침되는가, 그리고 사용자의 사정에 대한 결론("받으실 수 있습니다")을 내리지 않았는가. 걸리면 다시 쓰거나(최대 2회) 거절합니다.

1겹이 걸리면 2겹은 건너뜁니다(호출을 아끼려고). 2겹은 LLM이라 틀릴 수 있습니다. 그래서 정확도를 사람이 붙인 라벨로 재 보려 했지만 사람 라벨은 0건이라 **검증기의 정확도는 아직 모릅니다**(`docs/results.md`의 "사람이 확인한 건수: 0").
