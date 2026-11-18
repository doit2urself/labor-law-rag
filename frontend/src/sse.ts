// SSE(서버가 보내는 이벤트 스트림) 해석기.
// 역할: 네트워크로 조금씩 도착하는 글자 조각을 이어 붙여, 이벤트 하나(이름 + JSON 데이터)가 완성될 때마다 알려 준다.
// 사용처: api.ts의 streamChat.
//
// SSE 형식(백엔드 app/api/routes.py의 sse()): 이벤트는 빈 줄로 끝나고, 줄은 `event: 이름`, `data: JSON`이다.
// 핵심 개념 — 조각 경계: 네트워크는 이벤트를 아무 데서나 끊어 보낸다(한 줄의 중간, 빈 줄 사이 등).
// 그래서 받은 글을 버퍼에 쌓아 두고, 빈 줄이 완성된 부분만 이벤트로 꺼낸다.

export interface SseMessage {
  event: string;
  data: unknown;
}

export interface SseParser {
  /** 새로 받은 글 조각을 넣는다. 완성된 이벤트마다 onMessage가 불린다. */
  push(chunk: string): void;
  /** 스트림이 끝났을 때 부른다. 빈 줄로 끝나지 않은(반쯤 온) 이벤트는 SSE 규격대로 버린다. */
  end(): void;
}

export function createSseParser(onMessage: (message: SseMessage) => void): SseParser {
  let buffer = "";

  /** 이벤트 한 덩어리(빈 줄 앞까지)를 읽어 이름과 데이터를 꺼낸다. */
  function dispatch(block: string): void {
    let event = "";
    const dataLines: string[] = [];
    for (const line of block.split("\n")) {
      if (line === "" || line.startsWith(":")) continue; // 빈 줄·주석(연결 유지 신호)
      const colon = line.indexOf(":");
      const field = colon === -1 ? line : line.slice(0, colon);
      let value = colon === -1 ? "" : line.slice(colon + 1);
      if (value.startsWith(" ")) value = value.slice(1); // 규격: 콜론 뒤 공백 하나는 값이 아니다
      if (field === "event") event = value;
      else if (field === "data") dataLines.push(value);
      // retry·id 같은 나머지 필드는 쓰지 않는다
    }
    if (!event || dataLines.length === 0) return; // 이름이나 데이터가 없는 덩어리는 쓸 수 없다

    let data: unknown;
    try {
      data = JSON.parse(dataLines.join("\n")); // data 줄이 여럿이면 줄바꿈으로 이어 붙인다(규격)
    } catch {
      // 깨진 이벤트 하나가 스트림 전체를 죽이지 않게 그 이벤트만 버린다.
      console.warn(`SSE 이벤트의 데이터를 읽을 수 없어 건너뜁니다: ${event}`);
      return;
    }
    onMessage({ event, data }); // 콜백의 오류가 위의 try에 잡혀 "JSON 오류"로 둔갑하지 않도록 밖에서 부른다
  }

  return {
    push(chunk) {
      // \r\n을 \n으로 통일한다. 조각 경계에서 \r과 \n이 갈라져 와도 이어 붙인 뒤에 바꾸므로 안전하다.
      buffer = (buffer + chunk).replace(/\r\n/g, "\n");
      for (let end = buffer.indexOf("\n\n"); end !== -1; end = buffer.indexOf("\n\n")) {
        dispatch(buffer.slice(0, end));
        buffer = buffer.slice(end + 2);
      }
    },
    end() {
      buffer = "";
    },
  };
}
