import { afterEach, describe, expect, test, vi } from "vitest";
import { ApiError, fetchMe, login, logout, sendFeedback, streamChat } from "./api";
import realServerResponse from "./fixtures/chat-sse.txt?raw";
import type { ChatEvent } from "./types";

const encoder = new TextEncoder();

/** 바이트 조각들을 차례로 흘려보내는 SSE 응답을 만든다(조각 경계를 마음대로 정해 네트워크 분할을 흉내 낸다). */
function sseResponse(chunks: Uint8Array[], init: ResponseInit = { status: 200 }): Response {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      chunks.forEach((c) => controller.enqueue(c));
      controller.close();
    },
  });
  return new Response(stream, { ...init, headers: { "Content-Type": "text/event-stream" } });
}

function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const PROGRESS = 'event: progress\ndata: {"stage":"analyze","label":"질문을 분석하는 중"}\n\n';
const ANSWER_DATA = { refused: false, preface: null, sentences: [], sources: [], notice: [], message_id: "m1", thread_id: "t1" };
const ANSWER = `event: answer\ndata: ${JSON.stringify(ANSWER_DATA)}\n\n`;

function stubFetch(impl: (...args: Parameters<typeof fetch>) => Promise<Response>) {
  const mock = vi.fn(impl);
  vi.stubGlobal("fetch", mock);
  return mock;
}

async function collect(body = { thread_id: null as string | null, question: "q" }): Promise<ChatEvent[]> {
  const events: ChatEvent[] = [];
  await streamChat(body, (e) => events.push(e));
  return events;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("streamChat", () => {
  // 서버가 보낸 이벤트를 종류별 객체로 바꿔 순서대로 전달한다.
  test("delivers typed events in order", async () => {
    const detail = 'event: detail\ndata: {"stage":"search","text":"근로기준법 제55조"}\n\n';
    const delta = 'event: delta\ndata: {"part":"sentence","index":0,"text":"문장"}\n\n';
    stubFetch(async () => sseResponse([encoder.encode(PROGRESS + detail + delta + ANSWER)]));
    const events = await collect();
    expect(events.map((e) => e.type)).toEqual(["progress", "detail", "delta", "answer"]);
    expect(events[0]).toEqual({ type: "progress", stage: "analyze", label: "질문을 분석하는 중" });
    expect(events[3]).toEqual({ type: "answer", data: ANSWER_DATA });
  });

  // 한글 한 글자는 UTF-8로 3바이트다. 그 중간에서 네트워크 조각이 끊겨도 글자가 깨지면 안 된다.
  test("decodes Korean text split in the middle of a character", async () => {
    const bytes = encoder.encode(PROGRESS + ANSWER);
    const cut = encoder.encode('event: progress\ndata: {"stage":"analyze","label":"질').length + 1; // '질'의 첫 바이트 뒤
    stubFetch(async () => sseResponse([bytes.slice(0, cut), bytes.slice(cut)]));
    const events = await collect();
    expect(events[0]).toEqual({ type: "progress", stage: "analyze", label: "질문을 분석하는 중" });
  });

  // 실제 백엔드가 보낸 응답(fixtures/chat-sse.txt, 파이썬 테스트가 서버 출력과 같은지 확인한다)을 읽어 모든 이벤트 종류가 나온다.
  test("reads the response the real backend produced", async () => {
    stubFetch(async () => sseResponse([encoder.encode(realServerResponse)]));
    const events = await collect();
    expect(events[0]).toEqual({ type: "progress", stage: "analyze", label: "질문을 분석하는 중" });
    expect(new Set(events.map((e) => e.type))).toEqual(new Set(["progress", "detail", "delta", "answer"]));
    const last = events[events.length - 1];
    expect(last.type).toBe("answer");
    if (last.type === "answer") {
      expect(last.data.sources[0]).toMatchObject({ law_name: "근로기준법", article_no: "51" });
      expect(last.data.sentences[0].kind).toBe("evidence");
      expect(last.data.message_id).toBe("<uuid>"); // 파이썬 테스트가 매번 달라지는 UUID를 자리표시자로 바꿔 저장했다
    }
  });

  // 같은 응답을 바이트 하나씩 쪼개 보내도(최악의 네트워크 분할, 한글 3바이트의 중간 포함) 결과가 똑같다.
  test("gives identical events when the real response arrives one byte at a time", async () => {
    stubFetch(async () => sseResponse([encoder.encode(realServerResponse)]));
    const whole = await collect();
    const bytes = encoder.encode(realServerResponse);
    stubFetch(async () => sseResponse(Array.from(bytes, (_, i) => bytes.slice(i, i + 1))));
    expect(await collect()).toEqual(whole);
  });

  // 오류 이벤트는 예외가 아니라 일반 이벤트로 전달한다(화면이 문구를 그대로 보여 주려고).
  test("delivers an error event as an event, not an exception", async () => {
    const err = 'event: error\ndata: {"code":"quota","message":"오늘의 무료 사용 한도가 소진되었습니다. 나중에 다시 시도해 주세요"}\n\n';
    stubFetch(async () => sseResponse([encoder.encode(PROGRESS + err)]));
    const events = await collect();
    expect(events[1]).toEqual({ type: "error", code: "quota", message: "오늘의 무료 사용 한도가 소진되었습니다. 나중에 다시 시도해 주세요" });
  });

  // 모르는 종류의 이벤트는 무시한다(서버가 나중에 이벤트를 더해도 옛 화면이 깨지지 않게).
  test("ignores unknown event names", async () => {
    stubFetch(async () => sseResponse([encoder.encode('event: surprise\ndata: {"x":1}\n\n' + ANSWER)]));
    expect((await collect()).map((e) => e.type)).toEqual(["answer"]);
  });

  // 요청 모양: POST, JSON 본문, 쿠키(세션)를 같은 사이트 요청으로 함께 보낸다.
  test("posts the JSON body with the session cookie", async () => {
    const fetchMock = stubFetch(async () => sseResponse([encoder.encode(ANSWER)]));
    await collect({ thread_id: "abc", question: "주휴수당?" });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/chat");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(init?.body as string)).toEqual({ thread_id: "abc", question: "주휴수당?" });
    expect((init?.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
    expect(init?.credentials ?? "same-origin").toBe("same-origin");
  });

  // 사용 횟수 초과(429)·로그인 필요(401)·잘못된 질문(422)은 서버가 준 문장을 담은 ApiError로 던진다.
  test.each([
    [429, "이 IP의 사용 횟수(5회)를 초과했습니다."],
    [401, "로그인이 필요합니다."],
    [422, "질문은 1자 이상 1000자 이하로 입력해 주세요."],
  ])("HTTP %i becomes an ApiError carrying the server sentence", async (status, detail) => {
    stubFetch(async () => jsonResponse({ detail }, status));
    const error = await collect().catch((e) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status, message: detail });
  });

  // 서버가 JSON이 아닌 응답(프록시의 502 페이지 등)을 주면, 그 내용을 보여 주지 않고 일반 문구에 상태 번호만 붙인다.
  test("non-JSON error body becomes a generic message with the status", async () => {
    stubFetch(async () => new Response("<html>Bad Gateway</html>", { status: 502 }));
    const error = await collect().catch((e) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(502);
    expect(error.message).toContain("502");
    expect(error.message).not.toContain("Bad Gateway");
  });

  // 서버에 닿지 못한 경우(네트워크 끊김)는 상태 번호 0의 ApiError다.
  test("network failure becomes ApiError with status 0", async () => {
    stubFetch(async () => {
      throw new TypeError("Failed to fetch");
    });
    const error = await collect().catch((e) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(0);
  });

  // 답도 오류도 없이 스트림이 끝났다면 중간에 끊긴 것이다. 조용히 끝내지 않고 알린다.
  test("a stream that ends without answer or error is reported", async () => {
    stubFetch(async () => sseResponse([encoder.encode(PROGRESS)]));
    const error = await collect().catch((e) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(0);
    expect(error.message).toContain("끊");
  });

  // 사용자가 화면을 떠나 요청을 취소하면 AbortError 그대로 던진다(호출한 쪽이 조용히 무시하도록 ApiError로 감싸지 않는다).
  test("abort rethrows the AbortError untouched", async () => {
    const controller = new AbortController();
    stubFetch(async () => {
      controller.abort();
      throw new DOMException("aborted", "AbortError");
    });
    const error = await streamChat({ thread_id: null, question: "q" }, () => {}, controller.signal).catch((e) => e);
    expect(error).not.toBeInstanceOf(ApiError);
    expect(error.name).toBe("AbortError");
  });
});

describe("account and feedback calls", () => {
  // 로그인 성공은 사용자 정보를, 실패(401)는 서버 문장이 든 ApiError를 준다.
  test("login returns the user or throws the server sentence", async () => {
    stubFetch(async () => jsonResponse({ username: "test", role: "test" }, 200));
    expect(await login("test", "pw")).toEqual({ username: "test", role: "test" });
    stubFetch(async () => jsonResponse({ detail: "아이디 또는 비밀번호가 올바르지 않습니다." }, 401));
    await expect(login("test", "x")).rejects.toMatchObject({ status: 401, message: "아이디 또는 비밀번호가 올바르지 않습니다." });
  });

  // 로그인 여부 확인: 401은 "로그인 안 함"(null)이고 그 밖의 실패는 오류로 던진다.
  test("fetchMe returns null when not logged in", async () => {
    stubFetch(async () => jsonResponse({ detail: "로그인이 필요합니다." }, 401));
    expect(await fetchMe()).toBeNull();
    stubFetch(async () => new Response("boom", { status: 500 }));
    await expect(fetchMe()).rejects.toBeInstanceOf(ApiError);
  });

  // 로그아웃과 피드백은 각자의 주소로 POST하고, 피드백 본문에 message_id와 값(1 또는 -1)이 실린다.
  test("logout and feedback post to their endpoints", async () => {
    const fetchMock = stubFetch(async () => jsonResponse({ status: "ok" }, 200));
    await logout();
    await sendFeedback("m1", -1);
    expect(fetchMock.mock.calls.map((c) => c[0])).toEqual(["/api/logout", "/api/feedback"]);
    expect(JSON.parse(fetchMock.mock.calls[1][1]?.body as string)).toEqual({ message_id: "m1", value: -1 });
  });
});
