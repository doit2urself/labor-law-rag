import { describe, expect, test } from "vitest";
import { applyEvent, newTurn } from "./chatState";
import { makeAnswer } from "./test-fixtures";

describe("applyEvent", () => {
  // 새 질문은 "진행 중" 상태로 시작하고 아직 답도 오류도 없다.
  test("a new turn starts streaming with nothing else", () => {
    expect(newTurn(1, "주휴수당?")).toEqual({
      id: 1,
      question: "주휴수당?",
      status: "streaming",
      progress: [],
      answer: null,
      error: null,
    });
  });

  // 진행 이벤트는 단계 줄을 하나씩 쌓는다(원본 turn은 바뀌지 않는다).
  test("progress events append lines without mutating the old turn", () => {
    const before = newTurn(1, "q");
    const after = applyEvent(before, { type: "progress", stage: "analyze", label: "질문을 분석하는 중" });
    expect(after.progress).toEqual([{ stage: "analyze", label: "질문을 분석하는 중" }]);
    expect(before.progress).toEqual([]);
  });

  // 요약(detail)은 같은 단계의 가장 최근 줄 밑에 붙는다. 재검색으로 같은 단계가 두 번 나오면 두 번째 줄에 붙는다.
  test("detail attaches to the latest line of the same stage", () => {
    let turn = newTurn(1, "q");
    turn = applyEvent(turn, { type: "progress", stage: "search", label: "법령 조문을 찾는 중" });
    turn = applyEvent(turn, { type: "detail", stage: "search", text: "찾은 조문 없음" });
    turn = applyEvent(turn, { type: "progress", stage: "search", label: "검색어를 바꿔 다시 찾는 중 (1/2)" });
    turn = applyEvent(turn, { type: "detail", stage: "search", text: "근로기준법 제55조 …" });
    expect(turn.progress).toEqual([
      { stage: "search", label: "법령 조문을 찾는 중", detail: "찾은 조문 없음" },
      { stage: "search", label: "검색어를 바꿔 다시 찾는 중 (1/2)", detail: "근로기준법 제55조 …" },
    ]);
  });

  // 해당 단계가 아직 없는데 요약이 오면(순서가 어긋난 경우) 조용히 버린다. 화면이 깨지지 않는 쪽을 택한다.
  test("detail without a matching line is ignored", () => {
    const turn = applyEvent(newTurn(1, "q"), { type: "detail", stage: "verify", text: "x" });
    expect(turn.progress).toEqual([]);
  });

  // 글 조각(delta)은 곧 이어 오는 answer에 같은 내용이 들어 있어서 화면 상태를 바꾸지 않는다.
  test("delta events do not change the turn", () => {
    const turn = newTurn(1, "q");
    expect(applyEvent(turn, { type: "delta", part: "sentence", index: 0, text: "…" })).toBe(turn);
  });

  // 최종 답이 오면 완료 상태가 되고 답을 가진다.
  test("answer completes the turn", () => {
    const answer = makeAnswer();
    const turn = applyEvent(newTurn(1, "q"), { type: "answer", data: answer });
    expect(turn.status).toBe("done");
    expect(turn.answer).toBe(answer);
    expect(turn.error).toBeNull();
  });

  // 서버가 준 오류 문장은 가공하지 않고 그대로 보관한다(한도 소진 안내 등).
  test("error event stores the server message verbatim", () => {
    const turn = applyEvent(newTurn(1, "q"), { type: "error", code: "quota", message: "오늘의 무료 사용 한도가 소진되었습니다. 나중에 다시 시도해 주세요" });
    expect(turn.status).toBe("error");
    expect(turn.error).toBe("오늘의 무료 사용 한도가 소진되었습니다. 나중에 다시 시도해 주세요");
    expect(turn.answer).toBeNull();
  });
});
