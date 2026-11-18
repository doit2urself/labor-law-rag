// 테스트가 함께 쓰는 예시 데이터. 백엔드(app/graph/runner.py의 answer_payload)가 실제로 보내는 모양을 그대로 따른다.
import { DISCLAIMER, SOURCE_NOTICE } from "./texts";
import type { AnswerData, Source } from "./types";

export const SOURCES: Source[] = [
  {
    id: "doc1",
    label: "근로기준법 제55조",
    doc_type: "law",
    law_name: "근로기준법",
    article_no: "55",
    branch_no: null,
    url: "https://www.law.go.kr/법령/근로기준법/제55조",
    text: "제55조(휴일) ① 사용자는 근로자에게 1주에 평균 1회 이상의 유급휴일을 보장하여야 한다.",
  },
  {
    id: "doc2",
    label: "근로기준법 시행령 제30조",
    doc_type: "law",
    law_name: "근로기준법 시행령",
    article_no: "30",
    branch_no: null,
    url: "https://www.law.go.kr/법령/근로기준법%20시행령/제30조",
    text: "제30조(휴일) ① 법 제55조제1항에 따른 유급휴일은 1주 동안의 소정근로일을 개근한 자에게 주어야 한다.",
  },
];

export const THREAD_ID = "11111111-1111-4111-8111-111111111111";

/** 통과한 답변 한 건. 근거 문장 1개(출처 1번 인용) + 일반 설명 문장 1개. overrides로 필요한 부분만 바꾼다. */
export function makeAnswer(overrides: Partial<AnswerData> = {}): AnswerData {
  return {
    refused: false,
    preface: null,
    sentences: [
      { text: "사용자는 근로자에게 1주에 평균 1회 이상의 유급휴일을 보장하여야 합니다.", citation_ids: ["doc1"], kind: "evidence" },
      { text: "질문하신 근무 형태에 구체적으로 적용되는지는 확인되지 않습니다.", citation_ids: [], kind: "general" },
    ],
    sources: [SOURCES[0]],
    notice: [DISCLAIMER, SOURCE_NOTICE],
    message_id: "message-1",
    thread_id: THREAD_ID,
    ...overrides,
  };
}
