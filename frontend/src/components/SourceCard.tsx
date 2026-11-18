// 출처 카드 한 장: 답변이 근거로 인용한 조문 하나.
// 사용처: AnswerView.tsx. 번호(number)는 답변 문장 뒤의 [n] 표시와 같다.
import { formatSource } from "../format";
import type { Source } from "../types";

interface SourceCardProps {
  source: Source;
  /** 문장 뒤 [n]과 같은 번호(1부터). */
  number: number;
  /** 문장 뒤 [n] 링크가 가리키는 위치(id). 대화의 턴마다 달라서 부모가 정해 준다. */
  anchorId: string;
}

export function SourceCard({ source, number, anchorId }: SourceCardProps) {
  return (
    <li id={anchorId} className="source-card" aria-label={`출처 ${number}`}>
      <h4 className="source-title">
        <span className="source-number">[{number}]</span> {formatSource(source)}
      </h4>
      {source.url && (
        // 새 탭으로 열되 noopener: 열린 페이지가 이 화면을 조작하지 못하게 한다.
        <a className="source-link" href={source.url} target="_blank" rel="noopener noreferrer">
          법제처 원문 보기
        </a>
      )}
      <details className="source-body">
        <summary>조문 내용 보기</summary>
        <p className="source-text">{source.text}</p>
      </details>
    </li>
  );
}
