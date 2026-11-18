// 서버(app/api/routes.py, app/graph/runner.py)와 주고받는 값의 모양.
// 이 모양은 백엔드와의 약속이다. 백엔드의 Final.payload·SSE 이벤트가 바뀌면 여기도 함께 바꾼다.

/** 그래프의 여섯 단계. 진행 상태 줄이 어느 단계인지 나타낸다. */
export type Stage = "analyze" | "search" | "rerank" | "sufficiency" | "generate" | "verify";

/** 법령 출처 정보. 법령명·조문번호·가지번호(제55조의2의 "2")로 출처 이름을 만든다. */
export interface LawSourceInfo {
  doc_type: "law";
  law_name: string | null;
  article_no: string | null;
  branch_no: string | null;
}

/** 고용노동부 법령해석 출처 정보. v1은 해석을 색인하지 않지만(스펙 개정 1) 계획서의 계약이라 형식은 남겨 둔다. */
export interface InterpretationSourceInfo {
  doc_type: "interpretation";
  case_no: string;
  date: string;
}

export type SourceInfo = LawSourceInfo | InterpretationSourceInfo;

/** 답변이 인용한 출처 한 건: 이름을 만드는 정보 + 카드에 보일 값(문서 ID, 서버가 만든 라벨, 원문 링크, 조문 본문). */
export type Source = SourceInfo & {
  id: string;
  label: string;
  url: string | null;
  text: string;
};

/** 답변의 문장 한 개. kind가 evidence면 citation_ids로 출처를 가리키고, general이면 인용이 없는 일반 설명이다. */
export interface AnswerSentence {
  text: string;
  citation_ids: string[];
  kind: "evidence" | "general";
}

/** `answer` 이벤트의 데이터: 검증을 통과한 최종 답변(또는 거절) + 피드백용 message_id + 대화를 잇는 thread_id. */
export interface AnswerData {
  refused: boolean;
  preface: string | null;
  sentences: AnswerSentence[];
  sources: Source[];
  notice: string[];
  message_id: string;
  thread_id: string;
}

/** 스트림 안의 error 이벤트 종류. quota(무료 한도 소진)와 internal(서버 오류)이 실제로 오고, 나머지는 HTTP 상태로 먼저 거절되는 경우의 이름이다. */
export type ErrorCode = "quota" | "internal" | "limit" | "invalid";

/** 서버가 SSE로 보내는 이벤트를 종류별로 나눈 것. */
export type ChatEvent =
  | { type: "progress"; stage: Stage; label: string }
  | { type: "detail"; stage: Stage; text: string }
  | { type: "delta"; part: "preface" | "sentence"; index: number; text: string }
  | { type: "answer"; data: AnswerData }
  | { type: "error"; code: ErrorCode; message: string };

/** 질문 요청 본문. thread_id가 null이면 새 대화다(서버가 번호를 만들어 answer 이벤트로 돌려준다). */
export interface ChatRequest {
  thread_id: string | null;
  question: string;
}

export interface User {
  username: string;
  role: "admin" | "test";
}
