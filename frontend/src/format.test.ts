import { describe, expect, test } from "vitest";
import { citationNumbers, formatSource } from "./format";

describe("formatSource", () => {
  // 법령은 "법령명 제N조", 가지번호가 있으면 "제N조의M"으로 만든다. 해석은 안건번호와 날짜를 보인다(계획서의 계약).
  test("formatSource", () => {
    expect(formatSource({ doc_type: "law", law_name: "근로기준법", article_no: "55", branch_no: null })).toBe("근로기준법 제55조");
    expect(formatSource({ doc_type: "law", law_name: "근로기준법", article_no: "55", branch_no: "2" })).toBe("근로기준법 제55조의2");
    expect(formatSource({ doc_type: "interpretation", case_no: "근로기준과-1234", date: "2024-01-02" })).toBe(
      "고용노동부 법령해석 근로기준과-1234 (2024-01-02)",
    );
  });

  // 시행령처럼 법령명에 공백이 있어도 그대로 쓴다.
  test("keeps spaces inside the law name", () => {
    expect(formatSource({ doc_type: "law", law_name: "근로기준법 시행령", article_no: "30", branch_no: null })).toBe(
      "근로기준법 시행령 제30조",
    );
  });

  // 조문번호가 없는 청크(드문 경우)는 법령명만 보이고, 둘 다 없으면 빈 칸 대신 안내 문구를 보인다.
  test("degrades gracefully when parts are missing", () => {
    expect(formatSource({ doc_type: "law", law_name: "근로기준법", article_no: null, branch_no: null })).toBe("근로기준법");
    expect(formatSource({ doc_type: "law", law_name: null, article_no: null, branch_no: null })).toBe("출처 정보 없음");
  });
});

describe("citationNumbers", () => {
  const sources = [{ id: "doc4" }, { id: "doc1" }];

  // 문장이 인용한 문서 ID를 출처 카드 번호(출처 목록에서의 위치 + 1)로 바꾼다.
  test("maps citation ids to 1-based source numbers", () => {
    expect(citationNumbers(["doc1", "doc4"], sources)).toEqual([2, 1]);
  });

  // 목록에 없는 ID는 버리고, 같은 번호는 한 번만 보인다.
  test("drops unknown ids and duplicates", () => {
    expect(citationNumbers(["doc9", "doc1", "doc1"], sources)).toEqual([2]);
    expect(citationNumbers([], sources)).toEqual([]);
  });
});
