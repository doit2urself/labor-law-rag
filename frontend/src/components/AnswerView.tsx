// 답변 한 건을 보여 주는 카드: 서두, 문장(근거 문장 뒤에 [n] 번호), 근거 조문 카드들, 법제처 출처 표기, 👍/👎.
// 사용처: ChatView.tsx. 답변은 서버가 검증을 통과시킨 것만 오므로(스펙 6.4) 여기서는 그대로 보여 주기만 한다.
//
// 문장이 차례로 나타나는 효과: 서버는 검증이 끝난 뒤에야 답을 주기 때문에 문장이 모두 한꺼번에 도착한다. 그래서 문장마다
// CSS 애니메이션 지연(animationDelay)을 조금씩 늘려 위에서부터 차례로 나타나게 한다(진짜 토큰 스트리밍이 아니다. styles.css의 .reveal).
import { useState } from "react";
import { sendFeedback } from "../api";
import { citationNumbers } from "../format";
import type { AnswerData } from "../types";
import { Notices } from "./Notices";
import { SourceCard } from "./SourceCard";

/** 문장 하나가 나타나는 간격(초). 너무 길면 답을 읽는 데 방해가 된다. 측정한 값이 아닌 시작값이다. */
const REVEAL_STEP_S = 0.18;

interface AnswerViewProps {
  answer: AnswerData;
  /** 대화 안에서 이 답이 몇 번째 턴인지. 출처 카드의 id를 턴마다 다르게 만드는 데 쓴다(같은 id가 겹치면 [n] 링크가 엉뚱한 곳으로 간다). */
  turnId: number;
}

export function AnswerView({ answer, turnId }: AnswerViewProps) {
  // 👍/👎 상태는 이 카드 안에서만 쓰이므로 여기서 관리한다. rating이 있으면 이미 보낸 것이다.
  const [rating, setRating] = useState<1 | -1 | null>(null);
  const [pending, setPending] = useState(false);
  const [feedbackError, setFeedbackError] = useState<string | null>(null);

  const anchorId = (number: number) => `turn-${turnId}-source-${number}`;
  const delay = (step: number) => ({ animationDelay: `${(step * REVEAL_STEP_S).toFixed(2)}s` });
  const prefaceSteps = answer.preface ? 1 : 0;
  const sourcesStep = prefaceSteps + answer.sentences.length;

  async function rate(value: 1 | -1) {
    if (rating !== null || pending) return;
    setPending(true);
    setFeedbackError(null);
    try {
      await sendFeedback(answer.message_id, value);
      setRating(value);
    } catch {
      // 실패하면 보낸 것으로 치지 않고 다시 누를 수 있게 둔다.
      setFeedbackError("피드백을 보내지 못했습니다. 잠시 후 다시 시도해 주세요.");
    } finally {
      setPending(false);
    }
  }

  const locked = rating !== null || pending;

  return (
    <article className="answer" aria-label="답변">
      {answer.preface && (
        <p className={`answer-preface reveal${answer.refused ? " answer-refusal" : ""}`} style={delay(0)}>
          {answer.preface}
        </p>
      )}
      {answer.sentences.map((sentence, index) => (
        <p key={index} className={`sentence sentence-${sentence.kind} reveal`} style={delay(prefaceSteps + index)}>
          {sentence.text}
          {citationNumbers(sentence.citation_ids, answer.sources).map((number) => (
            <a key={number} className="citation" href={`#${anchorId(number)}`} aria-label={`출처 ${number}`}>
              [{number}]
            </a>
          ))}
        </p>
      ))}

      {answer.sources.length > 0 && (
        <section className="sources reveal" style={delay(sourcesStep)} aria-label="근거 조문">
          <h3>근거 조문</h3>
          <ul>
            {answer.sources.map((source, index) => (
              <SourceCard key={source.id} source={source} number={index + 1} anchorId={anchorId(index + 1)} />
            ))}
          </ul>
        </section>
      )}

      <Notices place="bottom" />

      {!answer.refused && (
        <div className="feedback" role="group" aria-label="답변 평가">
          <button type="button" aria-label="도움이 됐어요" aria-pressed={rating === 1} disabled={locked} onClick={() => void rate(1)}>
            👍
          </button>
          <button type="button" aria-label="도움이 안 됐어요" aria-pressed={rating === -1} disabled={locked} onClick={() => void rate(-1)}>
            👎
          </button>
          {rating !== null && <span role="status">의견을 보내 주셔서 감사합니다.</span>}
          {feedbackError && <span role="status" className="feedback-error">{feedbackError}</span>}
        </div>
      )}
    </article>
  );
}
