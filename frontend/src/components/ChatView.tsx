// 채팅 화면: 질문 입력, 진행 상태, 답변 목록.
// 사용처: App.tsx(로그인한 뒤). 서버와의 통신은 api.ts의 streamChat이 하고, 상태 변화 계산은 chatState.ts가 한다.
//
// 핵심 개념 1 — 대화 이어가기: 첫 질문의 답(answer 이벤트)에 서버가 만든 thread_id가 들어 있다. 다음 질문에 그것을 실어 보내면
// 서버(LangGraph 체크포인트)가 앞선 대화를 기억해 "그럼 알바는요?" 같은 질문을 이해한다. "새 대화"는 이 번호를 지운다.
// 핵심 개념 2 — 답이 오는 동안 입력을 잠근다: 같은 대화(thread_id)에 질문이 겹쳐 들어가면 서버의 저장된 상태가 엉킬 수 있어서
// 한 번에 한 질문만 보낸다.
import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { ApiError, isAbortError, streamChat } from "../api";
import { applyEvent, newTurn, type Turn } from "../chatState";
import { MAX_QUESTION_CHARS } from "../texts";
import type { ChatEvent } from "../types";
import { AnswerView } from "./AnswerView";

interface ChatViewProps {
  /** 서버가 로그인이 풀렸다고(401) 알렸을 때 부른다. 상위 화면이 로그인 화면으로 돌려보낸다. */
  onSessionExpired: () => void;
}

export function ChatView({ onSessionExpired }: ChatViewProps) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);

  // 화면을 다시 그리지 않아도 되는 값은 ref에 둔다. threadId: 이어 가는 대화 번호, nextId: 턴 번호, abort: 진행 중 요청의 취소 장치.
  const threadIdRef = useRef<string | null>(null);
  const nextIdRef = useRef(1);
  const abortRef = useRef<AbortController | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  // 화면을 떠날 때(로그아웃 등) 진행 중인 요청을 취소한다.
  useEffect(() => () => abortRef.current?.abort(), []);

  // 새 내용이 생기면 맨 아래로 스크롤한다. (테스트 환경의 jsdom에는 scrollIntoView가 없어서 ?.로 건너뛴다)
  // 핵심 개념 — 스크롤 목표를 입력창 **뒤**에 둔 이유: 입력창(.composer)은 화면 아래에 고정(position: sticky)되어 그 위의 내용을 덮는다.
  // 목표가 입력창 앞(대화 목록 끝)에 있으면 그 지점을 화면 맨 아래에 맞추는 순간 마지막 답변의 아랫부분(출처 카드·법제처 표기·👍/👎)이
  // 입력창 밑에 깔린다(실제 브라우저에서 약 146px). 문서의 진짜 끝에 맞추면 입력창이 제 자리에 놓이고 내용은 그 위에 온전히 보인다.
  useEffect(() => {
    endRef.current?.scrollIntoView?.({ behavior: "smooth", block: "end" });
  }, [turns]);

  function updateTurn(id: number, event: ChatEvent) {
    setTurns((previous) => previous.map((turn) => (turn.id === id ? applyEvent(turn, event) : turn)));
  }

  async function send() {
    const question = input.trim();
    if (!question || streaming) return;

    const id = nextIdRef.current++;
    const controller = new AbortController();
    abortRef.current = controller;
    setTurns((previous) => [...previous, newTurn(id, question)]);
    setInput("");
    setStreaming(true);

    try {
      await streamChat(
        { thread_id: threadIdRef.current, question },
        (event) => {
          if (event.type === "answer") threadIdRef.current = event.data.thread_id;
          updateTurn(id, event);
        },
        controller.signal,
      );
    } catch (error) {
      if (isAbortError(error)) return; // 화면을 떠나며 일부러 취소한 것
      if (error instanceof ApiError && error.status === 401) {
        onSessionExpired(); // 로그인이 풀림: 오류 줄이 아니라 로그인 화면으로
        return;
      }
      // 서버가 준 문장(사용 횟수 초과, 잘못된 질문, 연결 끊김 등)은 그대로, 그 밖의 예외는 일반 문구로 보인다.
      const message = error instanceof ApiError ? error.message : "예상하지 못한 오류가 발생했습니다.";
      updateTurn(id, { type: "error", code: "internal", message });
    } finally {
      if (!controller.signal.aborted) setStreaming(false);
    }
  }

  function startNewConversation() {
    threadIdRef.current = null;
    setTurns([]);
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== "Enter" || event.shiftKey) return; // Shift+Enter는 줄바꿈
    // 한글은 글자를 "조합"하는 중에 Enter로 확정한다. 그 Enter를 보내기로 읽으면 마지막 글자가 남은 채 질문이 나가 버린다.
    // keyCode 229는 일부 브라우저(Safari)가 조합 확정 직후의 Enter에 붙이는 값이다.
    if (event.nativeEvent.isComposing || event.keyCode === 229) return;
    event.preventDefault();
    void send();
  }

  return (
    <section className="chat">
      <div className="conversation" aria-live="polite">
        {turns.length === 0 && (
          <p className="hint">근로·노동 문제를 질문해 보세요. 예) 주 15시간 일하면 주휴수당을 받을 수 있나요?</p>
        )}
        {turns.map((turn) => (
          <div key={turn.id} className="turn">
            <p className="question">{turn.question}</p>
            {turn.progress.length > 0 && (
              <ol className="progress" aria-label="진행 상태">
                {turn.progress.map((line, index) => {
                  const active = turn.status === "streaming" && index === turn.progress.length - 1;
                  return (
                    <li key={index} className={active ? "active" : undefined} aria-current={active ? "step" : undefined}>
                      <span className="step-label">{line.label}</span>
                      {line.detail && <span className="step-detail">{line.detail}</span>}
                    </li>
                  );
                })}
              </ol>
            )}
            {turn.error && (
              <p className="turn-error" role="alert">
                {turn.error}
              </p>
            )}
            {turn.answer && <AnswerView answer={turn.answer} turnId={turn.id} />}
          </div>
        ))}
      </div>

      <form
        className="composer"
        onSubmit={(event) => {
          event.preventDefault();
          void send();
        }}
      >
        <textarea
          aria-label="질문 입력"
          value={input}
          rows={3}
          maxLength={MAX_QUESTION_CHARS}
          disabled={streaming}
          placeholder="질문을 입력하세요. (Enter: 보내기, Shift+Enter: 줄바꿈)"
          onChange={(event) => setInput(event.target.value)}
          onKeyDown={onKeyDown}
        />
        <div className="composer-row">
          <span className="counter">
            {input.length} / {MAX_QUESTION_CHARS}
          </span>
          <button type="button" className="secondary" onClick={startNewConversation} disabled={streaming || turns.length === 0}>
            새 대화
          </button>
          <button type="submit" disabled={!input.trim() || streaming}>
            보내기
          </button>
        </div>
      </form>
      {/* 자동 스크롤의 목표. 입력창보다 뒤(문서의 진짜 끝)에 둔다 — 아래 useEffect의 설명 참고. */}
      <div ref={endRef} />
    </section>
  );
}
