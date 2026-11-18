// 대화 화면의 상태 변화를 계산하는 순수 함수들.
// 역할: 서버 이벤트(ChatEvent) 하나를 받아 "질문 한 턴"의 상태를 새 값으로 바꾼다. React 없이 시험할 수 있게 따로 뺐다.
// 사용처: components/ChatView.tsx.
import type { AnswerData, ChatEvent, Stage } from "./types";

/** 진행 상태의 한 줄: 단계가 시작될 때 생기고, 단계가 끝난 뒤의 요약(detail)이 붙는다. */
export interface ProgressLine {
  stage: Stage;
  label: string;
  detail?: string;
}

/** 질문 한 건과 그에 대한 서버의 반응 전체. */
export interface Turn {
  id: number;
  question: string;
  status: "streaming" | "done" | "error";
  progress: ProgressLine[];
  answer: AnswerData | null;
  error: string | null;
}

export function newTurn(id: number, question: string): Turn {
  return { id, question, status: "streaming", progress: [], answer: null, error: null };
}

/**
 * 이벤트 하나를 반영한 새 Turn을 돌려준다(원본은 건드리지 않는다. React는 새 객체가 와야 화면을 다시 그린다).
 *
 * - progress: 단계 줄을 하나 쌓는다. detail: 같은 단계의 가장 최근 줄에 요약을 붙인다(재검색으로 같은 단계가 또 나오면 두 번째 줄).
 * - delta: 곧 이어 오는 answer에 같은 문장이 모두 들어 있어서 화면 상태를 바꾸지 않는다(문장이 차례로 나타나는 효과는 CSS가 맡는다).
 * - answer: 검증을 통과한 최종 답. error: 서버가 준 오류 문장을 가공 없이 보관한다.
 */
export function applyEvent(turn: Turn, event: ChatEvent): Turn {
  switch (event.type) {
    case "progress":
      return { ...turn, progress: [...turn.progress, { stage: event.stage, label: event.label }] };
    case "detail": {
      const index = turn.progress.map((line) => line.stage).lastIndexOf(event.stage);
      if (index === -1) return turn; // 아직 시작 줄이 없는 단계의 요약은 버린다(화면이 깨지지 않는 쪽)
      const progress = turn.progress.map((line, i) => (i === index ? { ...line, detail: event.text } : line));
      return { ...turn, progress };
    }
    case "delta":
      return turn;
    case "answer":
      return { ...turn, status: "done", answer: event.data, error: null };
    case "error":
      return { ...turn, status: "error", error: event.message };
  }
}
