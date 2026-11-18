# RAG v1 설계서 검토 — 근거 기록 (2026-11-17)

대상: 사용자가 작성한 「RAG v1 설계서 (LangGraph + Langfuse)」 §0~§11.

**조사 방법**
- 2026-11-17에 Firecrawl(MCP 우선, 실패하면 curl)로 공식 문서, 논문(arXiv), PyPI, GitHub 원문을 조회했습니다.
- 예외는 한 곳입니다. Chroma 청킹 리포트는 Firecrawl 두 경로가 모두 antibot으로 실패해서 WebFetch로 읽었습니다. WebFetch는 요약을 거친 결과라서 인용문을 원문과 다시 대조해야 합니다.
- 표기 규칙:
  - 「추정」: 인용한 출처에서 끌어낸 판단입니다.
  - UNVERIFIED: 이번 조사에서 확인하지 못한 항목입니다.

---

## 요약 — 설계를 바꿀 만한 발견 (영향 큰 순)

1. **재검색 판단이 너무 늦습니다.** 지금 설계는 답변을 다 만든 뒤 검증 단계(§6)에서야 "근거 부족 → 재검색"을 판정합니다.
   - CRAG와 LangGraph 공식 RAG 튜토리얼은 **생성 전에 검색 문서부터 채점**합니다.
   - 생성 전에 문서 충분성 판정을 넣으면 쓸모없는 LLM 호출이 줄어듭니다(「추정」).
2. **무료 셀프 호스팅 Langfuse에는 보관 기간·프로젝트 권한·서버측 마스킹이 없습니다.** 세 기능 모두 EE(유료)입니다.
   - 셀프 호스팅 데이터는 기본적으로 무기한 보관됩니다.
   - 그래서 §8의 "보관 기간, 접근 권한 설정"은 무료판에서 그대로는 불가능합니다. 마스킹은 SDK 쪽(MIT)에서 해야 합니다.
3. **문서를 통한 프롬프트 인젝션 대책이 설계서에 없습니다.** OWASP LLM01(2025, 2026판 모두)에 해당합니다.
4. **`langchain-community`가 아카이브되었습니다(2026-06-24).** `BM25Retriever`는 이 패키지 안에만 남아 있습니다.
   - 설계는 OpenSearch가 하이브리드 검색을 직접 하므로, LangChain 검색 래퍼 대신 OpenSearch 클라이언트를 노드에서 직접 호출하는 편이 안전합니다(「추정」).
   - 관리되는 LangChain용 OpenSearch 통합 패키지가 있는지는 UNVERIFIED입니다.
5. **후보 수가 작을 수 있습니다.**
   - Anthropic 실험: 리랭커 입력 150개 → 출력 20개가 가장 좋았습니다. 5, 10, 20개를 비교했고 20개가 최고였습니다.
   - OpenSearch 문서: 하위 쿼리 크기를 100~200으로 권장합니다.
   - LangChain 문서는 20 → 5를 권장 예로 듭니다.
   - 근거가 엇갈리므로 평가셋으로 정해야 합니다.
6. **임베딩 모델이 청크 길이 상한을 정합니다.** multilingual-e5-large는 입력 512토큰에서 잘리고, `query: ` / `passage: ` 접두어가 필수입니다. bge-m3는 8192토큰까지 받습니다.
7. **Nori는 OpenSearch 기본 배포판에 들어 있지 않습니다.** 플러그인을 따로 설치해야 하고, Docker라면 이미지를 직접 빌드해야 합니다.
8. **한 서버에 다 올리기 빠듯합니다.**
   - Langfuse docker-compose 권장 사양: 4코어 / 16GiB.
   - OpenSearch Docker 최소: 호스트 메모리 4GB.
   - 여기에 로컬 임베딩·리랭커 모델까지 한 VM에 올리면 메모리 계획이 필요합니다.
9. **Langfuse가 토큰을 대신 세어 주지 않습니다.** 자동 토큰 계산 기능이 2026-11-06에 폐지되었으므로 LLM 응답에 usage가 들어 있어야 합니다.
   - SDK v5는 서버 v4.59.0 이상이 필요합니다.

---

## A. 설계서 주장별 판정

### §0 확정 사항

| 주장 | 판정 | 근거 |
|---|---|---|
| OpenSearch는 무료, Apache 2.0 | **확인됨** | https://github.com/opensearch-project/OpenSearch/blob/main/LICENSE.txt "Apache License Version 2.0, January 2004". 최신 3.9.0 (2026-09-29, https://opensearch.org/releases/) |
| Elasticsearch 무료판은 RRF 미지원 | **확인됨 (설계보다 더 강함)** | https://www.elastic.co/subscriptions 원본 HTML: "Reciprocal Rank Fusion (RRF) for hybrid search" basic=hyphen, platinum=hyphen, enterprise=check-icon. Basic만이 아니라 Platinum에도 없습니다. Elastic Cloud 요금제는 UNVERIFIED |
| (참고) Elasticsearch 라이선스 | — | https://www.elastic.co/pricing/faq/licensing "adding the … AGPLv3 license as an option alongside SSPL and our Elastic license" / "Our releases will continue to be under the Elastic License." |

### §1 색인

| 주장 / 항목 | 판정 | 근거·보완 |
|---|---|---|
| 청크 크기·overlap은 정답이 없고 평가셋으로 정한다 | **확인됨** | Anthropic: "chunk size, chunk boundary, and chunk overlap can affect retrieval performance" (https://www.anthropic.com/news/contextual-retrieval). Chroma: 전략에 따라 recall이 "up to 9%" 차이 남, 크기 200·overlap 없음이 좋았음 (https://www.trychroma.com/research/evaluating-chunking, ⚠ WebFetch 경유라 원문 대조 필요) |
| 의미 기반(semantic) 청킹 | 설계에는 없음 (넣지 않아도 됨) | Qu 외 (arXiv 2410.13070): "costs associated with semantic chunking are not justified by consistent performance gains" |
| PDF 표·레이아웃 파싱 | 보완 | Docling (arXiv 2408.09869, MIT): "layout analysis (DocLayNet) and table structure recognition (TableFormer)" |
| 임베딩 후보 multilingual-e5 | **제약 추가** | https://huggingface.co/intfloat/multilingual-e5-large: "truncated to at most 512 tokens"이고 "query: " / "passage: " 접두어 필수. Mr. TyDi 한국어 MRR@10은 BM25 28.1 vs e5-large 61.6 |
| 임베딩 후보 BGE-M3 | 확인됨 | https://huggingface.co/BAAI/bge-m3: "more than 100 working languages", "up to 8192 tokens". 한국어 수치는 그림으로만 있어 UNVERIFIED |
| (선택) Contextual Retrieval | 설계에 없음 → v2 후보 | 청크마다 50~100토큰 설명문을 붙여 색인합니다. 실패율이 35% 감소했고(5.7%→3.7%), BM25도 적용하면 49%, 리랭커까지 더하면 67% 감소했습니다(같은 Anthropic 글). 대신 색인할 때 LLM 비용이 듭니다 |

### §2 검색 (Hybrid + RRF)

| 주장 | 판정 | 근거·보완 |
|---|---|---|
| BM25 + Vector를 한 인덱스에 | **확인됨** | https://docs.opensearch.org/latest/search-plugins/search-pipelines/normalization-processor/ 예제가 text 필드 + vector 필드를 한 인덱스에 두고 `hybrid`로 결합합니다. hybrid 하위 쿼리는 최대 5개("The maximum number of query clauses is 5") |
| RRF는 점수가 아니라 순위를 합친다 | **확인됨** | Cormack 외 SIGIR'09 (https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf): "combines ranks without regard to the arbitrary scores" |
| k=60은 출발점 | **확인됨** | 같은 논문: "k = 60 was near-optimal, but that the choice was not critical". OpenSearch `score-ranker-processor`(2.19 도입) `rank_constant` "Default is `60`". 기본값은 도구마다 다릅니다(Qdrant 2, Weaviate는 점수 기반 융합) → 설계서에 명시한 것이 맞습니다 |
| Top 20 | **보완** | OpenSearch는 하위 쿼리마다 샤드당 `size`개까지만 가져옵니다("each subquery returns up to `size` results from each shard", `pagination_depth`로 조절). normalization 문서는 하위 쿼리 크기 100~200을 권장합니다. 이 권장이 RRF에도 맞는지는 「추정」입니다. → 융합 **전** 깊이를 20보다 크게 잡으세요 |
| 권한 필터는 검색 쿼리 안에서 | **확인됨** | https://docs.opensearch.org/latest/vector-search/ai-search/hybrid-search/pre-filtering/ (3.0+): "The `filter` is applied during query execution to each subquery". 후처리 필터의 위험: "may return significantly fewer than `k` results" (https://docs.opensearch.org/latest/vector-search/filter-search-knn/index/). OWASP 2026 LLM09: "Enforce tenant scoping inside the index query, not as a post-retrieval filter", "Apply access control at the chunk level" |
| 색인과 검색에 같은 분석기 | **확인됨 (예외 1개)** | OpenSearch: "specifying a search analyzer that is different from the index analyzer is not necessary and could negatively impact search result relevance" (https://docs.opensearch.org/latest/analyzers/search-analyzers/). 예외는 동의어로, Elasticsearch synonym_graph 문서에 "designed to be used as part of a search analyzer only"라고 되어 있습니다. OpenSearch 동의어 문서는 UNVERIFIED |
| Nori | **보완** | 소스는 있음(OpenSearch `plugins/analysis-nori`). 하지만 core 플러그인은 "not bundled by default" → `opensearch-plugin install analysis-nori`로 설치하고, 버전은 OpenSearch와 같아야 합니다. Docker는 `RUN … opensearch-plugin install --batch` 줄을 넣어 이미지를 직접 빌드. OpenSearch 쪽 Nori 설정 문서는 찾지 못함(UNVERIFIED). 형태소 분석: "uses the mecab-ko-dic dictionary" (https://www.elastic.co/docs/reference/elasticsearch/plugins/analysis-nori) |
| 정확 일치 필드 | **확인됨 + 보완** | https://docs.opensearch.org/latest/mappings/supported-field-types/keyword/: "indexes the entire value as a single term". 단 "case sensitive unless you configure a normalizer" → 부품 번호에 normalizer를 거세요 |
| 한국어 BM25에는 형태소 분석이 필요 | 확인됨 (제3자 근거) | AutoRAG 토크나이저 벤치마크 BM25 top-5 recall: 공백 0.8509 / kiwi 0.9474 / okt 0.9649 (https://github.com/Marker-Inc-Korea/AutoRAG-example-tokenizer-benchmark). 데이터 규모가 공개되지 않아 일반화는 UNVERIFIED |

**k-NN 메모리** (https://docs.opensearch.org/latest/mappings/supported-field-types/knn-methods-engines/)
- 공식: `1.1 * (4 * dimension + 8 * m)` bytes/vector
- 문서 예시 256차원·m=16·100만 개 = 1.267GB. 직접 재계산해도 같은 값이 나왔습니다.
- 1024차원이라고 가정하면(「추정」: 모델 차원 수는 이번 조사에서 확인하지 않았습니다):
  - 청크 100만 개 ≈ 4.646GB
  - 청크 10만 개 ≈ 0.465GB
- 같은 문서: "Using a replica doubles the total number of vectors"

### §3 Reranker

| 주장 / 후보 | 판정 | 근거 |
|---|---|---|
| Retriever는 넓게, Reranker는 좁게 | 확인됨 | Anthropic: 리랭커 입력 "top 150" → 출력 "top 20". "We tried delivering 5, 10, and 20 chunks, and found using 20 to be the most performant". 다만 "worth experimenting on your use case" |
| 5개로 부족할 수 있다 | 근거로 뒷받침됨 | 위와 같은 Anthropic 실험. LangChain 문서는 반대로 "retrieve top-20 via embeddings, rerank down to top-5"를 권장 예로 듭니다 |
| bge-reranker-v2-m3 (로컬) | 일부 확인 | https://huggingface.co/BAAI/bge-reranker-v2-m3: 0.6B, "strong multilingual capabilities". 한국어 명시 여부와 라이선스는 UNVERIFIED |
| Cohere (API) | 확인됨 | https://docs.cohere.com/docs/rerank-overview: Rerank 5 (`rerank-v5.0-pro`), 지원 언어 표에 "ko \| Korean". 단 "performance may vary across languages" |
| (참고) Jina reranker v3 | 주의 | "CC BY-NC 4.0" — 비상업 용도로만 사용 가능 |
| API 리랭커는 문서를 외부로 보낸다 | 논리상 맞음 | 별도 근거 조사 안 함 |

### §4 Query Rewrite

| 주장 | 판정 | 근거 |
|---|---|---|
| 대화 이력을 반영한 재작성 | 확인됨 | CANARD (EMNLP 2019, https://aclanthology.org/D19-1605/): "rewrite a context-dependent into a self-contained question with the same answer" |
| 재작성 과정에서 원래 조건이 사라질 수 있다 → 원본도 넘긴다 | 근거로 뒷받침됨 | RAG-Fusion (arXiv 2402.03367): "some answers strayed off topic when the generated queries' relevance to the original query is insufficient". 증거로서는 약함(한 회사 데이터, 사람 채점) |
| HyDE (설계에는 없음) | 참고 | arXiv 2212.10496. 가상 문서가 "unreal and may contain false details"일 수 있습니다. 쓰더라도 벡터 쪽에만 적용합니다 |
| 재작성으로 늘어나는 지연 | UNVERIFIED | 수치 근거를 찾지 못했습니다 |

### §5 답변 생성 + Citation

| 주장 | 판정 | 근거 |
|---|---|---|
| Citation은 답변과 함께 생성한다 | **확인됨 (관행)** | Anthropic Citations: "each text block can contain a claim … and a list of citations" (https://platform.claude.com/docs/en/build-with-claude/citations) |
| 나중에 붙이면 정확도가 떨어진다 | **부분 확인** | ALCE (arXiv 2305.14627): "citation recall of ClosedBook+PostCite is lower than Vanilla by 47% on ASQA". 다만 이건 **문서 없이 쓴 답**에 나중에 출처를 붙인 경우입니다. 문서로 답한 뒤 사후 부착하는 경우는 UNVERIFIED → 문구를 좁혀 쓰세요 |
| LLM Citation을 그대로 믿지 않는다 | **확인됨** | Anthropic: "The API doesn't guarantee either." ALCE: "even the best models lack complete citation support 50% of the time" (ELI5) |
| 구조화 출력 | 확인됨 | LangChain v1: `with_structured_output(…, include_raw=True)`, 에이전트는 `response_format`. v1 문서에는 RAG 인용 how-to가 없고, v0.3 아카이브(`qa_citations.ipynb`, "Using tool-calling to cite document IDs")에만 있습니다 |

### §6 Answer Validation

| 주장 | 판정 | 근거 |
|---|---|---|
| 결정적 검사는 코드로 | **확인됨** | OWASP 2026: "validate every response in trusted application code … using structural validation rather than a second LLM call." |
| LLM 검증도 틀린다 → 사람 라벨로 확인 | **확인됨 (강하게)** | FaithBench (arXiv 2410.13210): "even the best hallucination detectors have near 50% accuracies" (탐지기끼리 판정이 엇갈린 어려운 사례만 모은 셋). LLM-judge 편향: "position, verbosity, and self-enhancement biases" (arXiv 2306.05685) |
| 검증기 대안 | 보완 | MiniCheck (arXiv 2404.10774): "GPT-4-level performance but for 400x lower cost". ALCE는 NLI 모델로 citation을 판정합니다 |
| 재검색 분기 | **위치 보완** | CRAG (arXiv 2401.15884)는 **답변이 아니라 검색 문서**를 채점합니다: "assess the overall quality of retrieved documents". LangGraph 공식 튜토리얼(https://docs.langchain.com/oss/python/langgraph/agentic-rag)도 `grade_documents` → `rewrite_question` 순서로, 생성 전에 판정합니다 |
| 거부 / 근거 부족 안내 | 확인됨 | Anthropic: "Explicitly give Claude permission to admit uncertainty." Sufficient Context (arXiv 2411.06037): 큰 모델은 문맥이 부족해도 "often output incorrect answers instead of abstaining" |
| 일부 근거만 있으면 나눠 답변 | 근거로 뒷받침됨 | Anthropic: "If it can't find a quote, it must retract the claim." (https://platform.claude.com/docs/en/test-and-evaluate/strengthen-guardrails/reduce-hallucinations) |

### §7 LangGraph 운영

| 주장 | 판정 | 근거·보완 |
|---|---|---|
| Checkpointer State가 다음 턴까지 남아서 초기화가 필요 | **확인됨** | "New input does not clear the thread. LangGraph applies it using the state's reducers." (https://docs.langchain.com/oss/python/langgraph/checkpointers). 누적형 reducer는 `[]`를 넣어도 비워지지 않으므로 `Overwrite([])`를 씁니다(graph-api-state#resetting-a-reducer-field). 새 턴은 `Command(update=…)`가 아니라 일반 dict로 호출합니다 |
| 운영은 PostgreSQL, 개발은 메모리 | **확인됨** | `InMemorySaver`가 정식 이름이고 `MemorySaver`는 호환용 별칭. "Only use `InMemorySaver` for debugging or testing". `langgraph-checkpoint-postgres` 3.2.1 (2026-11-14). 처음에 `setup()`을 호출해야 하며, 문서는 주기적 정리(pruning)를 권장합니다 |
| 다른 대화 참조는 Store | **확인됨 (문구 보정)** | "stores hold arbitrary key-value data accessible from any thread" (https://docs.langchain.com/oss/python/langgraph/stores). 보정: Checkpointer도 모든 thread를 **저장**하지만, 한 번의 실행은 자기 thread만 읽습니다 |
| 진행 상태만 스트리밍 | **확인됨 + 누출 주의** | `stream_mode="custom"` + `get_stream_writer`. `messages`·`values`·`updates` 모드를 클라이언트에 그대로 넘기면 검증 전 답변이 샙니다(「추정」, "Values streaming emits **all** state channels"). 생성 LLM에 `nostream` 태그를 달거나 custom 이벤트만 전달합니다. 신규 앱에는 v1.2 event streaming을 권장 |
| 재시도 루프 | **확인됨 + 보완** | recursion limit 기본값은 10,007 super-step(1.1.4+)이라 재시도 횟수 제한으로 쓸 수 없습니다 → state의 명시적 카운터로 제한합니다. 예외로 인한 재시도는 `RetryPolicy`로 따로 처리 |
| 대화별 thread ID | 확인됨 | `configurable.thread_id` 필수. "Keep `thread_id` values under 255 characters." |

### §8 Langfuse

| 주장 | 판정 | 근거·보완 |
|---|---|---|
| 구성: Web, Worker, Postgres, ClickHouse, Redis, S3 | **확인됨** | `langfuse-docs` 저장소 architecture 문서. AI Gateway(베타, 선택)가 추가로 있습니다 |
| 노드 입출력·LLM 호출·지연·에러 자동 수집 | **확인됨** | `from langfuse.langchain import CallbackHandler`. retriever 출력도 자동 기록. 에러: "`level` and `statusMessage` are automatically set" |
| 토큰 수 자동 | **조건부** | "We're retiring automatic tokenization in Langfuse" (changelog 2026-11-06) → LLM 응답에 usage가 있어야 합니다. 로컬 모델 서버가 usage를 돌려주는지 확인 필요 |
| 세부 로직·검증 결과는 직접 계측 | **확인됨** | 리랭커 결과가 자동 수집된다는 근거는 찾지 못함 → 직접 기록 |
| 비용은 모델 가격 정보가 필요 | 확인됨 | "calculates cost from submitted usage and the generation's `model` parameter". 사용자 정의 모델이 내장 가격보다 우선합니다 |
| 평가는 응답이 나간 뒤에 돈다 | **확인됨** | LLM-as-a-Judge: worker가 큐에서 처리하며 "Nothing runs inside your application". 트레이싱도 "non-blocking" |
| 마스킹, 보관 기간, 접근 권한 설정 | **무료판에서는 일부만 가능** | SDK 마스킹(`mask_otel_spans`)은 MIT로 무료. 서버측 마스킹, 보관 기간, **프로젝트 단위 RBAC는 EE(유료)**. "On self-hosted instances, data is stored indefinitely by default." |
| 프롬프트 버전 관리, 데이터셋, 실험 | 확인됨 | 단 fallback 프롬프트를 쓰면 "no link will be created" |
| "LLM용 Grafana/APM" | **절반만 맞음** | "Unlike general-purpose APM tools, Langfuse provides features specific to AI engineering"; Datadog 비교 문서: "Many teams run both". 추적과 대시보드는 비슷하지만 평가·프롬프트·데이터셋이 더 있고, 인프라 APM을 대체하지 않습니다 |
| (참고) 회사 소식 | — | "ClickHouse has acquired Langfuse." (2026-01-16), "There are no planned changes to licensing." |
| 셀프 호스팅 사양 | 보완 | docker-compose 권장 "at least 4 cores and 16 GiB of memory", 저장소 100GiB 예시. 단 "lacks high-availability, scaling capabilities, and backup functionality" |

### §9 품질 평가

| 항목 | 판정 | 근거 |
|---|---|---|
| Recall@20 | 확인됨 (같은 척도 선례) | Anthropic: "We use 1 minus recall@20 as our evaluation metric" |
| 평가셋은 처음에 | 근거로 뒷받침됨 | Anthropic "Always run evals"; Qdrant "use your eval set to choose between them" |
| Ragas | 버전 확인 | ragas 0.4.4 (2026-11-14). 옛 API는 "removed in version 1.0" 예정. Context Recall은 "always requires a reference" → 정답 답변 필수(설계에 이미 있음). Langfuse의 Ragas 가이드는 SDK v4 기준이라 오래되었을 수 있음 |

---

## B. 설계서 안의 불일치 (근거: 설계서 본문)

1. §4 Query Rewrite가 §10 개발 순서 어디에도 없습니다. 게다가 §4가 쓰는 대화 이력은 §10의 6단계에서야 생깁니다.
2. §9의 정답이 청크 ID라면, §1의 청크 크기 조정 때마다 정답표가 깨집니다. → 정답은 문서 ID + 페이지(또는 근거 문장 원문)로 적습니다.
3. §6에서 "검증기 정확도를 확인한다"고 했지만 §9 지표 표에는 해당 항목이 없습니다.
4. §6에서 재검색 한도는 2회로 정했지만 재생성 한도가 없고, "한도 초과"가 어느 한도인지 불분명합니다. → 질문당 LLM 호출 총량을 정합니다.
5. §0·§11에서 LLM이 미정인데, §5 구조화 출력과 §6 LLM 검증은 모델 성능에 달려 있습니다. LLM 선정이 §10 순서에 없습니다.
6. §1 갱신 처리는 "변경"만 다룹니다. 삭제된 문서를 인덱스에서 빼는 처리와, 옛 버전이 검색에 섞이는 것을 막는 규칙이 없습니다.
7. §5에서 계산 문장은 인용 면제인데, 계산에 들어간 입력값의 출처 규칙이 없습니다.

## C. 설계서에 없는 것 (근거 있는 것만)

- **생성 전 문서 충분성 판정**: CRAG, LangGraph agentic-rag 튜토리얼
- **간접 프롬프트 인젝션 대책**
  - OWASP LLM01 (2025): "Separate and clearly denote untrusted content"
  - OWASP 2026 (2026-08-04 공개): "no reliable prevention mechanism exists today", "Pass external content through a structurally separate, provenance-labeled channel"
- **검색 로그 보존**: OWASP LLM08:2025 "Maintain detailed immutable logs of retrieval activities". 2026판에서는 Vector & Embedding이 **LLM09:2026**으로 번호가 바뀌었습니다.
- **Nori 플러그인이 들어간 OpenSearch 이미지 빌드**
- **Python 버전**: LangChain은 3.10 이상이고, Langfuse SDK v5는 3.11 이상입니다. 그래서 프로젝트는 3.11 이상이어야 합니다(「추정」: 두 조건을 합친 결과).

## D. 버전 기준표 (2026-11-17 조회, PyPI·공식 릴리스)

| 패키지 | 버전 | 날짜 |
|---|---|---|
| langchain | 1.5.0 | 2026-11-11 |
| langchain-core | 1.8.1 | 2026-11-15 |
| langgraph | 1.2.22 | 2026-11-14 |
| langgraph-checkpoint-postgres | 3.2.1 | 2026-11-14 |
| langchain-classic | 1.0.8 | 2026-06-10 |
| langchain-text-splitters | 1.1.3 | 2026-10-02 |
| langchain-community | 0.4.2 (**아카이브 2026-06-24**) | 2026-05-22 |
| langfuse (Python SDK) | 5.2.0 (Python ≥3.11, 서버 ≥4.59.0, langchain-core ≥1) | 2026-11-05 |
| Langfuse 서버 | v4.81.0 | "16 Nov" (연도는 「추정」 2026) |
| ragas | 0.4.4 | 2026-11-14 |
| OpenSearch | 3.9.0 (3.10.0은 2026-11-17 예정, 출시 여부 UNVERIFIED) | 2026-09-29 |

참고로 기존 프로젝트 `py_saramin_search_llm/requirements.txt`는 `langgraph>=0.2`로 1.x 이전 표기입니다.

## E. 확인하지 못한 것 (UNVERIFIED)

- 관리되는 LangChain용 OpenSearch 통합 패키지가 있는지 (community 아카이브 이후)
- OpenSearch의 Nori 설정 문서, OpenSearch 동의어 문서
- bge-reranker-v2-m3의 한국어 명시 여부와 라이선스, bge-m3의 한국어 수치, 각 임베딩 모델의 차원 수
- 문서로 답한 뒤 사후에 인용을 붙이는 방식의 정확도
- 쿼리 재작성의 지연 수치
- Elastic Cloud 요금제의 RRF 포함 여부
- DeepEval·TruLens 비교, Langfuse–Ragas 연동이 SDK v5에서도 동작하는지

## F. 접속 실패 기록 (요약)

- **antibot 차단**:
  - langfuse.com 전반: GitHub `langfuse/langfuse-docs` 원문으로 대체
  - docs.voyageai.com: MongoDB의 Voyage 문서로 대체
  - genai.owasp.org: GitHub 원문으로 대체
  - docs.ragas.io 일부: GitHub 원문으로 대체
  - 일부 docs.langchain.com integration 페이지: GitHub `langchain-ai/docs` 원문으로 대체
  - trychroma.com: WebFetch로 대체
- **404**:
  - `docs.langchain.com/oss/python/integrations/retrievers/bm25`
  - `docs.opensearch.org/latest/analyzers/language-analyzers/korean/` (HTTP 200이지만 404 화면)
- **빈 검색 결과**: OWASP 2026, Nori, 대화형 재작성 벤더 문서 등. 일부는 검색어를 바꾸거나 직접 scrape해서 해결했고, 해결하지 못한 것은 E에 적었습니다.

---

## G. 문서(코퍼스) 후보 — 데이터 출처 확인 (2026-11-17, Firecrawl)

**국가건설기준센터** (https://www.kcsc.re.kr/standardCode/list/102041)
- 건축 구조기준(KDS 41)은 "전체 34 건"입니다.
- 코드 번호 예시: "KDS 41 30 10 건축물 강구조 설계기준"
- 개정 이력 표기: 2016·2018·2019·2022(·2024)
- 관련 단체 표기: "대한건축학회"
- 재배포 조건은 UNVERIFIED입니다.

**법제처 국가법령정보 공동활용** (https://open.law.go.kr/LSO/openApi/guideList.do)
- "총 191 건"의 API를 제공합니다.
- 범위: 법령 본문·조항호목·연혁, 행정규칙, 자치법규, 판례, 법령해석례, 위원회 결정문(개인정보보호위·공정위·노동위 등), 부처 1차 해석(고용노동부·국토교통부 등), 학칙·공단·공공기관 규정
- 이용하려면 API 신청이 필요합니다.

**법령해석례 본문 API** (https://open.law.go.kr/LSO/openApi/guideResult.do?htmlName=expcInfoGuide)
- 출력 필드: "질의요지", "회답", "이유", "해석기관명", "해석일자" 등
- 질문과 정답이 이미 짝지어 있으므로 평가셋 재료로 쓸 수 있습니다.
- 단, 부처 1차 해석 API(예: 고용노동부)의 필드는 UNVERIFIED입니다.

**고용노동부 법령해석 본문 API** (https://open.law.go.kr/LSO/openApi/guideResult.do?htmlName=cgmExpcMoelInfoGuide)
- `target=moelCgmExpc`로 요청합니다.
- 출력 필드: "질의요지", "회답", "이유", "관련법령", "해석일자", "데이터기준일시" 등
- 해석이 몇 건 있는지는 API 키가 있어야 확인할 수 있어 UNVERIFIED입니다.

**API 호출 조건** (2026-11-17, `OC=test`로 시험 호출한 결과)
- 응답 원문: "OPEN API 호출 시 사용자 검증을 위하여 정확한 서버장비의 IP주소 및 도메인주소를 등록해 주세요."
- 즉 신청할 때 호출할 서버의 IP나 도메인을 등록해야 합니다.

**저작권법 제7조** (https://www.law.go.kr/법령/저작권법/제7조)
- 보호받지 못하는 저작물로 다음을 규정합니다:
  - "헌법ㆍ법률ㆍ조약ㆍ명령ㆍ조례 및 규칙"
  - "국가 또는 지방자치단체의 고시ㆍ공고ㆍ훈령 그 밖에 이와 유사한 것"
  - "법원의 판결ㆍ결정ㆍ명령 및 심판이나 행정심판절차 … 의결ㆍ결정 등"
- 법령해석례와 KDS가 여기에 해당하는지는 UNVERIFIED입니다.

**공공데이터포털 e약은요** (https://www.data.go.kr/data/15075057/openapi.do)
- 비용은 "무료"이고, 이용허락범위는 "제한 없음"입니다.
- 일반의약품의 효능·사용법·주의사항·상호작용·부작용·보관법을 제공합니다.
- 개발계정 트래픽은 10,000입니다.
- 등록된 활용 사례가 9건 있습니다.

---

## H. 이 PC 자원 측정과 Langfuse 사양 원문 (2026-11-17~18)

**측정 (PowerShell)**
- CPU i5-1240P 12코어 / 16스레드, RAM 15.6GB
- 측정 시점의 메모리: 사용 중 10.1GB, 여유 5.5GB
  - 큰 프로세스: claude 1.81GB, msedge 1.66GB, Orca 1.06GB, MsMpEng 0.8GB
- GPU: Intel Iris Xe, Intel Arc A350M (NVIDIA 없음)
- 디스크 여유: C: 13.4GB, D: 171.3GB
- docker와 ollama는 설치되어 있지 않고, `~/.wslconfig`도 없습니다.
- `wsl --status`는 종료 코드 50으로 실패했습니다. 원인은 확인하지 못해서 WSL 설치 여부는 UNVERIFIED입니다.

**Langfuse 원문** (`langfuse/langfuse-docs` main, 2026-11-18 조회)
- `content/self-hosting/deployment/docker-compose.mdx`: "We recommend that you use at least 4 cores and 16 GiB of memory, e.g. a t3.xlarge on AWS." 한 대의 VM에 올리는 구성의 권장 사양입니다.
- `content/self-hosting/deployment/infrastructure/containers.mdx`: "For production environments, we recommend to use at least 2 CPUs and 4 GB of RAM for all containers."
  - Web/Worker 앱 컨테이너를 다루는 문서입니다.
  - "all containers"가 컨테이너마다인지 합계인지는 원문만으로 불분명합니다.
  - ClickHouse와 Postgres는 별도 문서(`infrastructure/clickhouse.mdx`, `postgres.mdx`)이며 이번에 읽지 않았습니다.

**WSL2 기본 메모리** (https://learn.microsoft.com/en-us/windows/wsl/wsl-config, 2026-10-16 수정)
- `memory`의 기본값은 "50% of total memory on Windows"입니다.
- 15.6GB의 절반이므로 약 7.8GB입니다(계산).
- Docker Desktop이 WSL2 백엔드를 쓰는지는 UNVERIFIED입니다.

**측정하지 않은 것**
- Langfuse와 OpenSearch가 실제로 쓰는 메모리
- 로컬 LLM·임베딩·리랭커 모델의 메모리와 속도

---

## I. 무료 LLM·임베딩·리랭커 조건 (2026-11-18, Firecrawl)

**Gemini API** (https://ai.google.dev/gemini-api/docs/pricing)
- 무료 요금제 표시: "Limited access to certain models", "Free input & output tokens", "Content used to improve our products"
- 무료 요금제에서 입력·출력이 "Free of charge"인 모델: `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite`, `gemini-3.8-flash`, `gemini-3.6-flash`, `gemini-4-hydrogen`, `gemini-embedding-001`, `gemini-embedding-2`
  - 모델 이름은 표 바로 앞에 나온 모델 코드로 짝지었습니다. 페이지 구조에서 끌어낸 추정입니다.
- 무료 요금제가 "Not available"인 모델: `gemini-4-carbon`, `gemini-4-argon`
- "Used to improve our products": 무료는 Yes, 유료는 No. 예: `gemini-3.8-flash`, `gemini-embedding-2`
- 무료 요금제의 RPM·RPD 숫자는 페이지에 없습니다(https://ai.google.dev/gemini-api/docs/rate-limits는 "View your active rate limits in AI Studio"로 안내). UNVERIFIED
- 무료 요금제의 자격: "Active project or free trial; some projects need verification"

**Cohere** (https://docs.cohere.com/docs/rate-limits)
- "evaluation keys (free but limited in usage)"
- "Trial keys … are limited to 1,000 API calls a month."
- 시험 키 한도: Chat 20 req/min, Rerank 10 req/min, Embed 2,000 inputs/min
- Rerank 5가 한국어를 지원한다는 근거는 앞선 조사(docs.cohere.com/docs/rerank-overview)에 있습니다.
- 가격 안내 페이지(https://docs.cohere.com/docs/how-does-cohere-pricing-work, WebFetch 요약): "trial API key usage is free, but limited", 시험 키로 "test different applications or build proofs of concept"를 할 수 있다고 합니다.
  - 시험 키를 상용·공개 서비스에 써도 되는지는 이 페이지에 적혀 있지 않습니다. UNVERIFIED
  - 월 호출 한도 숫자는 이 페이지에 없고, 위 rate-limits 페이지에만 있습니다.
- 계산: 시험 키로 질문 하나당 Rerank를 1번 부르면 한 달에 1,000개 질문입니다(1,000÷1). 평가 실험도 같은 한도를 씁니다.
- 로컬 `bge-reranker-v2-m3`는 0.6B 매개변수라서 가중치만 fp32 2.4GB, fp16 1.2GB, int8 0.6GB입니다(계산). CPU에서의 속도는 측정하지 않았습니다.

**Groq** (https://console.groq.com/docs/rate-limits)
- `openai/gpt-oss-120b`: 30 RPM / 1K RPD / 8K TPM / 200K TPD, `qwen/qwen3.8-27b`도 같은 값
- 페이지가 "the limits shown below are the base limits for the Developer plan"이라고 적어서, 이 값이 무료 요금제에도 해당하는지는 UNVERIFIED입니다.

**OpenRouter** (https://openrouter.ai/docs/api-reference/limits)
- 무료 모델의 분당·일일 한도 표가 스크랩에서 값이 비어 나왔습니다. UNVERIFIED
- 구매한 크레딧 양에 따라 일일 한도가 달라진다고만 확인했습니다.

**측정하지 않은 것**
- 위 서비스들의 한국어 법률 질문 답변 품질
- `langchain-google-genai`의 Python 요구 버전, Gemini 3.x 모델 이름 지원 여부

**LangChain 연동 패키지** (https://pypi.org/project/langchain-google-genai/)
- 최신 4.5.0, "Released: Nov 11, 2026"

**Gemini 임베딩** (https://ai.google.dev/gemini-api/docs/embeddings, 모델 페이지 aistudio.google.com/docs/models/gemini-embedding-001·-2, 2026-11-18)
- `gemini-embedding-2`: 입력 토큰 한도 8,192, 출력 차원 128~3072(권장 768·1536·3072)
  - 문서 설명: "across over 100 languages", 텍스트·이미지·영상·오디오·문서를 한 공간에 담는 멀티모달 모델
  - `task_type` 필드는 쓸 수 없고, 작업 지시를 프롬프트에 넣어야 합니다.
  - 줄인 차원은 자동으로 정규화됩니다.
- `gemini-embedding-001`: 입력 토큰 한도 2,048, 출력 차원 128~3072, `task_type` 지원
  - 3072가 아닌 차원은 직접 정규화해야 합니다.
- 두 모델 모두 MRL로 학습되어 차원을 줄여도 점수가 비슷하다고 문서가 설명합니다: "lower dimensions achieving scores comparable to their higher dimension counterparts"
- 무료 요금제는 입력이 "Free of charge"이고 "Used to improve our products"가 Yes입니다(앞선 가격표).
- 한국어 검색 성능 수치와 무료 호출 한도 숫자는 찾지 못했습니다. UNVERIFIED

**법제처 API 응답 필드** (https://open.law.go.kr/LSO/openApi/guideResult.do?htmlName=<가이드 이름>)
- 현행법령(시행일) 본문 `target=eflaw` (`lsEfYdInfoGuide`): 68개 필드
  - 조문번호, 조문가지번호, 조문제목, 조문내용
  - 항번호·항내용, 호번호·호내용, 목번호·목내용
  - 시행일자, 공포일자, 제개정구분
- 노동위원회 결정문 본문 `target=nlrc` (`nlrcInfoGuide`): 14개 필드
  - 사건번호, 판정사항, 판정요지, 판정결과, 내용 등
  - 결정문 한 건의 길이는 확인하지 못했습니다. UNVERIFIED

---

## J. 설치 조건 (2026-11-18, Firecrawl)

**OpenSearch Docker** (https://docs.opensearch.org/latest/install-and-configure/install-opensearch/docker/)
- "Docker Desktop users should set host memory utilization to a minimum of 4 GB"
- `vm.max_map_count=262144` 설정이 필요합니다. Windows에서는 Docker Desktop의 WSL 배포판 안에서 설정합니다.
- 단일 노드는 `discovery.type=single-node`로 띄웁니다.

**Docker Desktop 라이선스** (https://docs.docker.com/subscription/desktop-license/)
- "Docker Desktop is free for: Small businesses (fewer than 250 employees AND less than $10 million in annual revenue), Personal use"

**Docker Desktop Windows 요구 사항** (https://docs.docker.com/desktop/setup/install/windows-install/)
- "Windows 11 64-bit: Enterprise, Pro, or Education version 23H2 (build 22631) or higher."
- WSL 2 백엔드가 기본이며 "8GB system RAM"과 BIOS/UEFI의 하드웨어 가상화가 필요합니다.

**이 PC와 맞춰 본 결과**
- Windows 11 Pro 10.0.22631(환경 정보), RAM 15.6GB(측정) → 위 요건을 충족하는 것으로 보입니다.
- BIOS 가상화 설정은 확인하지 못했습니다.
- 가정용 IP가 고정인지, 법제처 API에 등록할 수 있는 IP 개수가 몇 개인지는 로그인 후에야 알 수 있습니다. UNVERIFIED
- 기존 VPS의 사양은 알 수 없습니다.

**Contabo Cloud VPS 4** (https://contabo.com/en/vps/, 2026-11-18, WebFetch 요약을 거친 값이라 원문과 대조가 필요합니다)
- 4 vCPU, RAM 8 GB, 100 GB SSD, 스냅샷 1개, 포트 200 Mbit/s, 트래픽 무제한(공정 사용 정책)
- 월 €4.40 표시. 원문: "The price displayed is the effective monthly rate for a 24-month subscription including applicable taxes."
- "Minimum initial contract length: 1 month"
- "A one-time setup fee applies to new Cloud VPS 4 instances (monthly contracts only)." 설정비 금액은 확인하지 못했습니다.
- 페이지에 설명 없는 "€5.50"도 같은 자리에 보였습니다. 의미는 UNVERIFIED입니다.
- 고정 공인 IP를 주는지, 이 서버가 이미 사용 중인지(다른 서비스와 메모리를 나눠 쓰는지)는 확인하지 못했습니다.
  - 이후 사용자가 이 서버는 이미 결제해 쓰는 이 프로젝트 전용 서버라고 답했습니다. 서버 안의 실제 상태는 아직 확인하지 않았습니다.

---

## K. HTTPS와 도메인 (2026-11-18, WebFetch·WebSearch)

**Let's Encrypt** (https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability, WebFetch 요약)
- 단기 인증서와 IP 주소 인증서가 정식 제공됩니다.
- "These certificates are valid for 160 hours, just over six days."
- "IP address certificates must be short-lived certificates." 클라이언트에서 "shortlived" 프로파일을 골라야 합니다.
- IPv4와 IPv6를 모두 지원합니다.
- 검증 방식(http-01, tls-alpn-01, DNS-01)은 이 페이지에 없습니다. 제3자 글은 DNS-01이 안 된다고 하지만 공식 확인은 못 했습니다. UNVERIFIED

**Caddy** (https://caddyserver.com/docs/automatic-https, WebFetch 요약)
- "Caddy serves IP addresses and local/internal hostnames over HTTPS using self-signed certificates"
- IP 주소는 공개 신뢰 인증서를 "generally" 받지 못하지만 "only from some CAs" 가능하다고 합니다. 어느 CA인지, Caddy의 어느 버전부터인지는 이 페이지에 없습니다.
- 외부 글 하나(https://laravel-news.com/frankenphp-https-ip-address)는 Caddy가 IP 식별자와 프로파일 선택을 지원한다고 쓰지만 공식 문서로는 확인하지 못했습니다. UNVERIFIED

**이전에 사용자가 적어 둔 메모** (전역 지침의 이전 판)
- Firecrawl 서버가 Nginx 뒤에서 Let's Encrypt 6일 IP 인증서를 자동 갱신으로 쓴다고 되어 있습니다. 그 서버의 현재 상태는 이번에 확인하지 않았습니다.

---

## L. OpenSearch를 Docker 없이 설치하는 방법 (2026-11-18, WebFetch 요약)

**tarball 설치** (https://docs.opensearch.org/latest/install-and-configure/install-opensearch/tar/, 문서 뒷부분 일부만 읽음)
- 배포판에 JDK가 들어 있습니다: "including an integrated Java Development Kit (JDK)."
- 예시 파일명이 `opensearch-3.10.0-linux-x64.tar.gz`입니다. 3.10.0이 최신이라는 직접적인 문장은 확인하지 못했습니다.
- 시작 전에 `OPENSEARCH_INITIAL_ADMIN_PASSWORD`를 설정해야 합니다(2.12 이상). 없으면 시작 스크립트가 그냥 끝납니다.
- 시스템 설정: `sudo swapoff -a`, `/etc/sysctl.conf`에 `vm.max_map_count=262144` 후 `sudo sysctl -p`
- 읽은 범위에는 systemd 서비스 등록과 `analysis-nori` 설치 설명이 없었습니다. UNVERIFIED
- Nori 설치 명령(`opensearch-plugin install analysis-nori`, 버전은 OpenSearch와 같아야 함)은 앞선 조사(A절)의 "Plugins" 문서 인용에 있습니다.

**Debian 패키지 설치** (https://docs.opensearch.org/latest/install-and-configure/install-opensearch/debian/)
- 페이지 앞부분이 메뉴로 채워져 있어 본문을 읽지 못했습니다. UNVERIFIED
