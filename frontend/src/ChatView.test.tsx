import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { ApiError, sendFeedback, streamChat } from "./api";
import { ChatView } from "./components/ChatView";
import { makeAnswer, SOURCES, THREAD_ID } from "./test-fixtures";
import type { ChatEvent } from "./types";

// 네트워크를 쓰는 두 함수만 가짜로 바꾸고, ApiError 같은 나머지는 진짜를 그대로 쓴다.
vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return { ...actual, streamChat: vi.fn(), sendFeedback: vi.fn() };
});

const streamChatMock = vi.mocked(streamChat);
const sendFeedbackMock = vi.mocked(sendFeedback);

/** 다음 질문에서 서버가 보낼 이벤트들을 정해 둔다(질문마다 한 번씩 소비). */
function serverSends(...events: ChatEvent[]) {
  streamChatMock.mockImplementationOnce(async (_body, onEvent) => {
    events.forEach((e) => onEvent(e));
  });
}

const PROGRESS: ChatEvent[] = [
  { type: "progress", stage: "analyze", label: "질문을 분석하는 중" },
  { type: "progress", stage: "search", label: "법령 조문을 찾는 중" },
  { type: "detail", stage: "search", text: "근로기준법 제55조, 제56조 …" },
];

function renderChat(onSessionExpired = vi.fn()) {
  const user = userEvent.setup();
  render(<ChatView onSessionExpired={onSessionExpired} />);
  return { user, onSessionExpired };
}

async function ask(user: ReturnType<typeof userEvent.setup>, text: string) {
  await user.type(screen.getByLabelText("질문 입력"), text);
  await user.click(screen.getByRole("button", { name: "보내기" }));
}

beforeEach(() => {
  streamChatMock.mockReset();
  sendFeedbackMock.mockReset();
});

describe("ChatView — 질문과 답변", () => {
  // 질문을 보내면 진행 단계(요약 포함), 근거 문장(번호 표시), 출처 카드, 법제처 출처 표기가 차례로 보인다.
  test("shows progress, the cited answer, the source card and the source notice", async () => {
    serverSends(...PROGRESS, { type: "answer", data: makeAnswer() });
    const { user } = renderChat();
    await ask(user, "주휴수당이 뭐야?");

    expect(await screen.findByText("주휴수당이 뭐야?")).toBeInTheDocument(); // 내 질문
    const steps = screen.getByRole("list", { name: "진행 상태" });
    expect(within(steps).getByText("질문을 분석하는 중")).toBeInTheDocument();
    expect(within(steps).getByText("근로기준법 제55조, 제56조 …")).toBeInTheDocument();

    const answer = screen.getByRole("article", { name: "답변" });
    expect(within(answer).getByText(/유급휴일을 보장하여야 합니다/)).toBeInTheDocument();
    expect(within(answer).getByRole("link", { name: "출처 1" })).toHaveTextContent("[1]"); // 근거 문장의 인용 번호
    const card = within(answer).getByRole("listitem", { name: /출처 1/ });
    expect(within(card).getByText("근로기준법 제55조")).toBeInTheDocument();
    const original = within(card).getByRole("link", { name: "법제처 원문 보기" });
    expect(original).toHaveAttribute("href", SOURCES[0].url);
    // 새 탭으로 열되, 열린 페이지가 이 화면을 조작하지 못하게 noopener를 붙인다.
    expect(original).toHaveAttribute("target", "_blank");
    expect(original.getAttribute("rel")).toContain("noopener");
    expect(within(card).getByText(SOURCES[0].text)).toBeInTheDocument();
    expect(within(answer).getByText("출처: 법제처 국가법령정보센터")).toBeInTheDocument(); // 답변 아래 출처 표기
  });

  // 인용이 없는 일반 설명 문장에는 번호를 붙이지 않는다(근거처럼 보이면 안 된다).
  test("general sentences carry no citation marker", async () => {
    serverSends({ type: "answer", data: makeAnswer() });
    const { user } = renderChat();
    await ask(user, "q");
    const general = await screen.findByText(/구체적으로 적용되는지는 확인되지 않습니다/);
    expect(within(general.closest("p") as HTMLElement).queryByRole("link")).not.toBeInTheDocument();
  });

  // 사건 판단 요청의 서두 문장은 답변 맨 위에 따로 보인다.
  test("shows the preface above the sentences", async () => {
    serverSends({ type: "answer", data: makeAnswer({ preface: "사건 판단은 하지 않고 관련 근거를 보여 드립니다." }) });
    const { user } = renderChat();
    await ask(user, "제가 부당해고 맞죠?");
    expect(await screen.findByText("사건 판단은 하지 않고 관련 근거를 보여 드립니다.")).toBeInTheDocument();
  });

  // 거절 답변은 안내 문구만 보이고 출처 카드는 없다. 그래도 법제처 출처 표기는 그대로 있다.
  test("a refusal shows only its message, still with the source notice", async () => {
    serverSends({ type: "answer", data: makeAnswer({ refused: true, preface: "근로 분야 질문이 아닙니다.", sentences: [], sources: [] }) });
    const { user } = renderChat();
    await ask(user, "전세 보증금");
    const answer = await screen.findByRole("article", { name: "답변" });
    expect(within(answer).getByText("근로 분야 질문이 아닙니다.")).toBeInTheDocument();
    expect(within(answer).queryByRole("listitem", { name: /출처/ })).not.toBeInTheDocument();
    expect(within(answer).getByText("출처: 법제처 국가법령정보센터")).toBeInTheDocument();
  });
});

describe("ChatView — 대화 이어가기", () => {
  // 첫 답이 알려 준 대화 번호(thread_id)를 다음 질문에 실어 보내고, "새 대화"를 누르면 다시 비워서 보낸다.
  test("carries thread_id to the next question until a new conversation starts", async () => {
    serverSends({ type: "answer", data: makeAnswer() });
    serverSends({ type: "answer", data: makeAnswer() });
    serverSends({ type: "answer", data: makeAnswer() });
    const { user } = renderChat();

    // 답을 받는 동안 입력창이 잠기므로, 다음 동작 전에 풀릴 때까지 기다린다(타이밍에 기대지 않는다).
    const waitUntilIdle = () => waitFor(() => expect(screen.getByLabelText("질문 입력")).toBeEnabled());

    await ask(user, "첫 질문");
    await screen.findByRole("article", { name: "답변" });
    await waitUntilIdle();
    await ask(user, "둘째 질문");
    await waitFor(() => expect(streamChatMock).toHaveBeenCalledTimes(2));
    await waitUntilIdle();
    await user.click(screen.getByRole("button", { name: "새 대화" }));
    await ask(user, "셋째 질문");
    await waitFor(() => expect(streamChatMock).toHaveBeenCalledTimes(3));

    expect(streamChatMock.mock.calls.map((c) => c[0])).toEqual([
      { thread_id: null, question: "첫 질문" },
      { thread_id: THREAD_ID, question: "둘째 질문" },
      { thread_id: null, question: "셋째 질문" },
    ]);
    expect(screen.queryByText("첫 질문")).not.toBeInTheDocument(); // 새 대화는 화면도 비운다
  });
});

describe("ChatView — 입력", () => {
  // 답이 오는 동안에는 입력창과 보내기 단추가 잠긴다(같은 대화에서 질문이 겹쳐 서버에서 엉키지 않게).
  test("locks the input while an answer is on its way", async () => {
    let finish: () => void = () => {};
    streamChatMock.mockImplementationOnce(
      (_body, onEvent) =>
        new Promise<void>((resolve) => {
          onEvent({ type: "progress", stage: "analyze", label: "질문을 분석하는 중" });
          finish = () => {
            onEvent({ type: "answer", data: makeAnswer() });
            resolve();
          };
        }),
    );
    const { user } = renderChat();
    await ask(user, "q");

    await screen.findByText("질문을 분석하는 중");
    expect(screen.getByLabelText("질문 입력")).toBeDisabled();
    expect(screen.getByRole("button", { name: "보내기" })).toBeDisabled();
    finish();
    await waitFor(() => expect(screen.getByLabelText("질문 입력")).toBeEnabled());
  });

  // 빈 질문이나 공백뿐인 질문은 보내지 않는다(서버도 422로 거절하지만 화면에서 먼저 막는다).
  test("does not send an empty or whitespace-only question", async () => {
    const { user } = renderChat();
    expect(screen.getByRole("button", { name: "보내기" })).toBeDisabled();
    await user.type(screen.getByLabelText("질문 입력"), "   ");
    expect(screen.getByRole("button", { name: "보내기" })).toBeDisabled();
    expect(streamChatMock).not.toHaveBeenCalled();
  });

  // 입력창은 서버와 같은 1,000자 상한을 두고 글자 수를 보여 준다.
  test("limits the input to 1000 characters and shows a counter", async () => {
    const { user } = renderChat();
    expect(screen.getByLabelText("질문 입력")).toHaveAttribute("maxlength", "1000");
    await user.type(screen.getByLabelText("질문 입력"), "가나다");
    expect(screen.getByText("3 / 1000")).toBeInTheDocument();
  });

  // Enter는 보내기, Shift+Enter는 줄바꿈이다. 한글은 글자를 조합하는 중에 Enter를 눌러 확정하므로(IME), 그때는 보내면 안 된다.
  test("Enter sends, Shift+Enter does not, and Enter while composing Korean does not", async () => {
    serverSends({ type: "answer", data: makeAnswer() });
    renderChat();
    const box = screen.getByLabelText("질문 입력");
    fireEvent.change(box, { target: { value: "주휴수당" } });

    fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
    fireEvent.keyDown(box, { key: "Enter", isComposing: true });
    expect(streamChatMock).not.toHaveBeenCalled();

    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() => expect(streamChatMock).toHaveBeenCalledTimes(1));
    expect(streamChatMock.mock.calls[0][0]).toEqual({ thread_id: null, question: "주휴수당" });
  });

  // 보낸 뒤에는 입력창이 비워진다.
  test("clears the input after sending", async () => {
    serverSends({ type: "answer", data: makeAnswer() });
    const { user } = renderChat();
    await ask(user, "q");
    await waitFor(() => expect(screen.getByLabelText("질문 입력")).toHaveValue(""));
  });
});

describe("ChatView — 오류", () => {
  // 서버가 준 오류 문장(예: 무료 한도 소진)을 그대로 보여 주고, 입력은 다시 열린다.
  test("shows a server error event verbatim and re-enables the input", async () => {
    serverSends(...PROGRESS.slice(0, 1), {
      type: "error",
      code: "quota",
      message: "오늘의 무료 사용 한도가 소진되었습니다. 나중에 다시 시도해 주세요",
    });
    const { user } = renderChat();
    await ask(user, "q");
    expect(await screen.findByRole("alert")).toHaveTextContent("오늘의 무료 사용 한도가 소진되었습니다. 나중에 다시 시도해 주세요");
    expect(screen.getByLabelText("질문 입력")).toBeEnabled();
    expect(screen.queryByRole("article", { name: "답변" })).not.toBeInTheDocument();
  });

  // test 계정의 사용 횟수 초과(HTTP 429)도 서버가 준 문장을 그대로 보여 준다.
  test("shows the server sentence for an HTTP 429", async () => {
    streamChatMock.mockRejectedValueOnce(new ApiError(429, "이 IP의 사용 횟수(5회)를 초과했습니다."));
    const { user } = renderChat();
    await ask(user, "q");
    expect(await screen.findByRole("alert")).toHaveTextContent("이 IP의 사용 횟수(5회)를 초과했습니다.");
  });

  // 잘못된 질문(HTTP 422)의 안내 문장도 그대로 보여 준다.
  test("shows the server sentence for an HTTP 422", async () => {
    streamChatMock.mockRejectedValueOnce(new ApiError(422, "질문은 1자 이상 1000자 이하로 입력해 주세요."));
    const { user } = renderChat();
    await ask(user, "q");
    expect(await screen.findByRole("alert")).toHaveTextContent("질문은 1자 이상 1000자 이하로 입력해 주세요.");
  });

  // 로그인이 풀렸으면(HTTP 401) 오류 줄을 보이는 대신 상위 화면에 알려 로그인 화면으로 돌아가게 한다.
  test("tells the parent when the session has expired", async () => {
    streamChatMock.mockRejectedValueOnce(new ApiError(401, "로그인이 필요합니다."));
    const { user, onSessionExpired } = renderChat();
    await ask(user, "q");
    await waitFor(() => expect(onSessionExpired).toHaveBeenCalledTimes(1));
  });

  // 서버에 닿지 못했을 때의 문장(ApiError 상태 0)도 보이고, 다시 질문할 수 있다.
  test("shows a network failure message", async () => {
    streamChatMock.mockRejectedValueOnce(new ApiError(0, "서버에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요."));
    const { user } = renderChat();
    await ask(user, "q");
    expect(await screen.findByRole("alert")).toHaveTextContent("서버에 연결하지 못했습니다");
    expect(screen.getByLabelText("질문 입력")).toBeEnabled();
  });
});

describe("ChatView — 피드백", () => {
  // 👍를 누르면 message_id와 1을 서버로 보내고, 눌린 상태로 표시하며, 같은 답에 다시 누르지 못하게 한다.
  test("sends thumbs-up once and marks it pressed", async () => {
    sendFeedbackMock.mockResolvedValue(undefined);
    serverSends({ type: "answer", data: makeAnswer() });
    const { user } = renderChat();
    await ask(user, "q");
    const up = await screen.findByRole("button", { name: "도움이 됐어요" });
    await user.click(up);

    expect(sendFeedbackMock).toHaveBeenCalledWith("message-1", 1);
    await waitFor(() => expect(screen.getByRole("button", { name: "도움이 됐어요" })).toHaveAttribute("aria-pressed", "true"));
    expect(screen.getByRole("button", { name: "도움이 안 됐어요" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "도움이 됐어요" })).toBeDisabled();
  });

  // 👎는 -1을 보낸다.
  test("sends thumbs-down as -1", async () => {
    sendFeedbackMock.mockResolvedValue(undefined);
    serverSends({ type: "answer", data: makeAnswer() });
    const { user } = renderChat();
    await ask(user, "q");
    await user.click(await screen.findByRole("button", { name: "도움이 안 됐어요" }));
    expect(sendFeedbackMock).toHaveBeenCalledWith("message-1", -1);
  });

  // 전송이 실패하면 눌린 것으로 처리하지 않고 안내를 보인다(다시 누를 수 있다).
  test("a failed feedback is reported and can be retried", async () => {
    sendFeedbackMock.mockRejectedValueOnce(new ApiError(500, "서버에 문제가 있어 요청을 처리하지 못했습니다 (500)"));
    serverSends({ type: "answer", data: makeAnswer() });
    const { user } = renderChat();
    await ask(user, "q");
    await user.click(await screen.findByRole("button", { name: "도움이 됐어요" }));
    expect(await screen.findByText(/피드백을 보내지 못했습니다/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "도움이 됐어요" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "도움이 됐어요" })).toHaveAttribute("aria-pressed", "false");
  });

  // 거절 답변과 오류에는 피드백 단추가 없다(평가할 답이 없다).
  test("no feedback buttons for a refusal", async () => {
    serverSends({ type: "answer", data: makeAnswer({ refused: true, preface: "범위 밖입니다.", sentences: [], sources: [] }) });
    const { user } = renderChat();
    await ask(user, "q");
    await screen.findByText("범위 밖입니다.");
    expect(screen.queryByRole("button", { name: "도움이 됐어요" })).not.toBeInTheDocument();
  });
});

describe("ChatView — 자동 스크롤", () => {
  // jsdom에는 scrollIntoView가 없어서, 달린 곳(this)을 기록하는 가짜를 끼워 두고 시험이 끝나면 치운다.
  const scrolledTo: Element[] = [];
  beforeEach(() => {
    scrolledTo.length = 0;
    Object.defineProperty(Element.prototype, "scrollIntoView", {
      configurable: true,
      value: function (this: Element) {
        scrolledTo.push(this);
      },
    });
  });
  afterEach(() => {
    delete (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView;
  });

  // 입력창은 화면 아래에 고정(sticky)되어 그 위의 내용을 덮는다. 스크롤 목표가 입력창보다 **앞**에 있으면, 목표를 화면 맨 아래에 맞추는
  // 순간 마지막 답변의 아랫부분(출처 카드·법제처 표기·👍/👎)이 입력창 밑에 깔린다(실제 브라우저에서 약 146px, 2026-11-19 측정).
  // 그래서 스크롤 목표는 입력창과 같거나 그보다 **뒤**(문서의 진짜 끝)여야 한다.
  test("scrolls to the real end of the page, not to a spot the composer covers", async () => {
    serverSends(...PROGRESS, { type: "answer", data: makeAnswer() });
    const { user } = renderChat();
    await ask(user, "주휴수당이 뭐야?");
    await screen.findByRole("article", { name: "답변" });

    const composer = screen.getByLabelText("질문 입력").closest("form") as HTMLFormElement;
    expect(scrolledTo.length).toBeGreaterThan(0);
    const target = scrolledTo[scrolledTo.length - 1];
    const sameOrAfterComposer =
      target === composer || (composer.compareDocumentPosition(target) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0;
    expect(sameOrAfterComposer).toBe(true);
  });
});
