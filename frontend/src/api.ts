// 서버(FastAPI, app/api/routes.py)를 부르는 함수 모음.
// 역할: 로그인·로그아웃·내 정보·피드백(보통 JSON 요청)과 채팅(SSE 스트림 읽기)을 맡고, 실패를 ApiError 하나로 모은다.
// 사용처: App.tsx(내 정보·로그인·로그아웃), components/ChatView.tsx(채팅·피드백).
//
// 오류 문장 규칙: 서버가 주는 오류 본문은 `{"detail": "화면에 그대로 보여 줄 문장"}`이다(401·422·429 등).
// 그 문장이 있으면 그대로 ApiError.message에 담고, 없으면(프록시의 HTML 오류 페이지 등) 내용을 보여 주지 않고 상태 번호만 붙인 일반 문구를 쓴다.
import { createSseParser } from "./sse";
import type { AnswerData, ChatEvent, ChatRequest, ErrorCode, Stage, User } from "./types";

/** 서버가 거절했거나(status 4xx/5xx) 서버에 닿지 못한(status 0) 경우. message는 사용자에게 그대로 보여 줄 문장이다. */
export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

const NETWORK_MESSAGE = "서버에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.";
const CUT_MESSAGE = "답변을 받는 도중에 연결이 끊겼습니다. 다시 질문해 주세요.";

/** 요청 취소(AbortController)로 생긴 예외인가. 화면을 떠날 때 일부러 취소하므로 오류로 보이면 안 된다. */
export function isAbortError(error: unknown): boolean {
  return typeof error === "object" && error !== null && (error as { name?: string }).name === "AbortError";
}

/** fetch를 부르되 네트워크 실패를 ApiError(0)로 바꾼다(취소는 그대로 던진다). 세션 쿠키는 같은 사이트 요청에 자동으로 실린다. */
async function request(url: string, init: RequestInit = {}): Promise<Response> {
  try {
    return await fetch(url, { credentials: "same-origin", ...init });
  } catch (error) {
    if (isAbortError(error)) throw error;
    throw new ApiError(0, NETWORK_MESSAGE);
  }
}

/** 실패한 응답에서 ApiError를 만든다. */
async function errorFrom(response: Response): Promise<ApiError> {
  let detail: string | null = null;
  try {
    const body: unknown = await response.json();
    const candidate = (body as { detail?: unknown } | null)?.detail;
    if (typeof candidate === "string") detail = candidate;
  } catch {
    // JSON이 아니면 아래의 일반 문구를 쓴다.
  }
  return new ApiError(response.status, detail ?? `서버에 문제가 있어 요청을 처리하지 못했습니다 (${response.status})`);
}

function postJson(url: string, body?: unknown, signal?: AbortSignal): Promise<Response> {
  return request(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  });
}

// ---- 계정 ----

/** 로그인. 성공하면 서버가 세션 쿠키를 심어 주고 사용자 정보를 돌려준다. 실패(401)·시도 과다(429)는 서버 문장이 든 ApiError. */
export async function login(username: string, password: string): Promise<User> {
  const response = await postJson("/api/login", { username, password });
  if (!response.ok) throw await errorFrom(response);
  return (await response.json()) as User;
}

export async function logout(): Promise<void> {
  const response = await postJson("/api/logout");
  if (!response.ok) throw await errorFrom(response);
}

/** 지금 로그인한 사용자. 로그인하지 않았으면(401) null이고, 그 밖의 실패는 오류로 던진다(서버 장애를 "로그아웃"으로 오해하지 않도록). */
export async function fetchMe(): Promise<User | null> {
  const response = await request("/api/me");
  if (response.status === 401) return null;
  if (!response.ok) throw await errorFrom(response);
  return (await response.json()) as User;
}

/** 답변에 대한 👍(1)/👎(-1). */
export async function sendFeedback(messageId: string, value: 1 | -1): Promise<void> {
  const response = await postJson("/api/feedback", { message_id: messageId, value });
  if (!response.ok) throw await errorFrom(response);
}

// ---- 채팅(SSE) ----

/** SSE 메시지 하나(이름 + JSON)를 화면이 쓰는 ChatEvent로 바꾼다. 모르는 이름이나 모양이 아닌 데이터는 null(무시). */
function toChatEvent(event: string, data: unknown): ChatEvent | null {
  if (typeof data !== "object" || data === null) return null;
  const d = data as Record<string, unknown>;
  switch (event) {
    case "progress":
      return { type: "progress", stage: d.stage as Stage, label: String(d.label) };
    case "detail":
      return { type: "detail", stage: d.stage as Stage, text: String(d.text) };
    case "delta":
      return { type: "delta", part: d.part as "preface" | "sentence", index: Number(d.index), text: String(d.text) };
    case "answer":
      return { type: "answer", data: data as AnswerData };
    case "error":
      return { type: "error", code: d.code as ErrorCode, message: String(d.message) };
    default:
      return null; // 서버가 나중에 이벤트를 더해도 옛 화면이 깨지지 않게 모르는 것은 무시한다
  }
}

/**
 * 질문을 보내고 서버가 흘려보내는 이벤트를 하나씩 onEvent로 전달한다. 스트림이 끝나면 Promise가 풀린다.
 *
 * - HTTP 오류(401 로그인 필요, 422 잘못된 질문, 429 사용 횟수 초과 등)는 스트림이 시작되기 전에 오므로 ApiError로 던진다.
 * - 스트림 안의 `error` 이벤트(한도 소진·서버 오류)는 던지지 않고 일반 이벤트로 전달한다(문장을 그대로 보여 주려고).
 * - 답(answer)이나 오류(error) 없이 스트림이 끝나면 중간에 끊긴 것이므로 ApiError(0)로 알린다.
 * - signal로 취소하면 AbortError를 그대로 던진다.
 *
 * 핵심 개념 — 바이트 조각과 한글: 네트워크는 UTF-8 바이트를 아무 데서나 끊는다. 한글 한 글자가 3바이트라 그 중간에서 끊길 수 있으므로
 * `TextDecoder`를 `stream: true`로 써서 모자란 바이트를 다음 조각과 이어 해석한다.
 */
export async function streamChat(
  body: ChatRequest,
  onEvent: (event: ChatEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await postJson("/api/chat", body, signal);
  if (!response.ok) throw await errorFrom(response);
  if (!response.body) throw new ApiError(0, CUT_MESSAGE);

  let finished = false;
  const parser = createSseParser((message) => {
    const event = toChatEvent(message.event, message.data);
    if (event === null) return;
    if (event.type === "answer" || event.type === "error") finished = true;
    onEvent(event);
  });

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  for (;;) {
    let chunk: ReadableStreamReadResult<Uint8Array>;
    try {
      chunk = await reader.read();
    } catch (error) {
      if (isAbortError(error)) throw error;
      throw new ApiError(0, CUT_MESSAGE); // 읽는 도중 연결이 끊김. (onEvent의 오류는 이 try 밖이라 여기서 둔갑하지 않는다)
    }
    if (chunk.done) break;
    parser.push(decoder.decode(chunk.value, { stream: true }));
  }
  parser.push(decoder.decode()); // 남은 바이트를 마저 해석
  parser.end();
  if (!finished) throw new ApiError(0, CUT_MESSAGE);
}
