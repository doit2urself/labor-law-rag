import { afterEach, describe, expect, test, vi } from "vitest";
import { createSseParser, type SseMessage } from "./sse";

/** 해석기를 만들고, 받은 메시지를 모아 두는 도우미. */
function setup() {
  const messages: SseMessage[] = [];
  const parser = createSseParser((m) => messages.push(m));
  return { messages, parser };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("createSseParser", () => {
  // 빈 줄로 끝나는 이벤트 하나를 이름과 JSON 데이터로 풀어낸다(백엔드 app/api/routes.py의 sse() 형식).
  test("parses one event", () => {
    const { messages, parser } = setup();
    parser.push('event: progress\ndata: {"stage":"search","label":"찾는 중"}\n\n');
    expect(messages).toEqual([{ event: "progress", data: { stage: "search", label: "찾는 중" } }]);
  });

  // 네트워크는 이벤트를 아무 데서나 끊어 보낸다. 한 줄의 중간에서 끊겨도 이어 붙여 같은 결과가 나와야 한다.
  test("joins an event split across chunks at any position", () => {
    const text = 'event: detail\ndata: {"stage":"search","text":"근로기준법 제55조"}\n\n';
    for (let cut = 1; cut < text.length; cut++) {
      const { messages, parser } = setup();
      parser.push(text.slice(0, cut));
      parser.push(text.slice(cut));
      expect(messages, `cut=${cut}`).toEqual([{ event: "detail", data: { stage: "search", text: "근로기준법 제55조" } }]);
    }
  });

  // 한 번에 여러 이벤트가 와도 순서대로 모두 전달한다.
  test("delivers several events from one chunk in order", () => {
    const { messages, parser } = setup();
    parser.push('event: a\ndata: {"n":1}\n\nevent: b\ndata: {"n":2}\n\n');
    expect(messages.map((m) => m.event)).toEqual(["a", "b"]);
  });

  // 윈도우식 줄바꿈(\r\n)도 같게 읽는다.
  test("accepts CRLF line endings", () => {
    const { messages, parser } = setup();
    parser.push('event: a\r\ndata: {"n":1}\r\n\r\n');
    expect(messages).toEqual([{ event: "a", data: { n: 1 } }]);
  });

  // 주석 줄(`:`로 시작)과 모르는 필드는 무시한다. 연결 유지용 신호가 이렇게 온다.
  test("ignores comment lines and unknown fields", () => {
    const { messages, parser } = setup();
    parser.push(': keep-alive\n\nretry: 3000\nevent: a\nid: 7\ndata: {"n":1}\n\n');
    expect(messages).toEqual([{ event: "a", data: { n: 1 } }]);
  });

  // data 줄이 여럿이면 줄바꿈으로 이어 붙인 뒤 JSON으로 읽는다(SSE 규격).
  test("joins multiple data lines with a newline", () => {
    const { messages, parser } = setup();
    parser.push('event: a\ndata: {"n":\ndata: 1}\n\n');
    expect(messages).toEqual([{ event: "a", data: { n: 1 } }]);
  });

  // 줄을 이을 때 사이에 줄바꿈을 넣는지(붙여 쓰지 않는지) 가르는 입력: `1`과 `2`를 붙이면 12가 되어 읽히지만, 규격대로 "1\n2"로 이으면 JSON이 아니라 버려진다.
  test("joins data lines with a newline rather than gluing them together", () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const { messages, parser } = setup();
    parser.push("event: a\ndata: 1\ndata: 2\n\n");
    expect(messages).toEqual([]);
  });

  // JSON이 깨진 이벤트 하나가 스트림 전체를 죽이면 안 된다. 그 이벤트만 버리고(경고를 남기고) 다음 이벤트를 계속 읽는다.
  test("skips an event with invalid JSON and keeps going", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const { messages, parser } = setup();
    parser.push("event: a\ndata: {broken\n\n");
    parser.push('event: b\ndata: {"ok":true}\n\n');
    expect(messages).toEqual([{ event: "b", data: { ok: true } }]);
    expect(warn).toHaveBeenCalledTimes(1);
  });

  // 끝나지 않은(빈 줄이 안 온) 이벤트는 반쯤 온 것이므로 전달하지 않는다.
  test("drops an unterminated event at the end of the stream", () => {
    const { messages, parser } = setup();
    parser.push('event: a\ndata: {"n":1}\n');
    parser.end();
    expect(messages).toEqual([]);
  });

  // event 줄이 없는 메시지는 이름이 없어 쓸 수 없으므로 버린다.
  test("drops a message without an event name", () => {
    const { messages, parser } = setup();
    parser.push('data: {"n":1}\n\n');
    expect(messages).toEqual([]);
  });
});
