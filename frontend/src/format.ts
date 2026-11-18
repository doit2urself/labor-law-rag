// 출처를 사람이 읽는 이름으로 바꾸는 순수 함수들.
// 사용처: SourceCard(카드 제목), AnswerView(문장 뒤 인용 번호).
import type { SourceInfo } from "./types";

/**
 * 출처의 표시 이름. 법령은 "법령명 제N조"(가지번호가 있으면 "제N조의M"), 해석은 안건번호와 날짜.
 * 일부 정보가 비어 있어도 빈 문자열을 돌려주지 않고, 있는 만큼만 보이거나 "출처 정보 없음"으로 대신한다.
 */
export function formatSource(source: SourceInfo): string {
  if (source.doc_type === "interpretation") {
    return `고용노동부 법령해석 ${source.case_no} (${source.date})`;
  }
  const lawName = source.law_name ?? "";
  if (!source.article_no) return lawName || "출처 정보 없음";
  const article = `제${source.article_no}조${source.branch_no ? `의${source.branch_no}` : ""}`;
  return lawName ? `${lawName} ${article}` : article;
}

/**
 * 문장이 인용한 문서 ID들을 출처 카드 번호(출처 목록에서의 위치 + 1)로 바꾼다.
 * 목록에 없는 ID는 버리고, 같은 번호는 한 번만 센다. 서버는 출처를 "처음 인용된 순서"로 주므로 번호는 읽는 순서와 맞는다.
 */
export function citationNumbers(ids: string[], sources: { id: string }[]): number[] {
  const numbers: number[] = [];
  for (const id of ids) {
    const index = sources.findIndex((s) => s.id === id);
    if (index !== -1 && !numbers.includes(index + 1)) numbers.push(index + 1);
  }
  return numbers;
}
