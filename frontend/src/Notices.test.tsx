import { render, screen } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import { Notices } from "./components/Notices";

const DISCLAIMER = "법률 자문이 아닙니다. 출처의 원문을 확인하세요";
const SOURCE_NOTICE = "출처: 법제처 국가법령정보센터";
const PII_NOTICE = "개인정보를 입력하지 마세요";

describe("Notices", () => {
  // 고정 문구 세 개가 글자 그대로 보인다(계획서의 계약). 문구를 테스트 안에 따로 적은 것은 상수가 바뀌어도 잡아내기 위해서다.
  test("Notices shows the three fixed phrases", () => {
    render(<Notices />);
    [DISCLAIMER, SOURCE_NOTICE, PII_NOTICE].forEach((t) => expect(screen.getByText(t)).toBeInTheDocument());
  });

  // 화면 위쪽에는 자문 아님·개인정보 안내만 둔다(출처 표기는 답변 아래에 둔다).
  test("place=top shows only the disclaimer and the PII notice", () => {
    render(<Notices place="top" />);
    expect(screen.getByText(DISCLAIMER)).toBeInTheDocument();
    expect(screen.getByText(PII_NOTICE)).toBeInTheDocument();
    expect(screen.queryByText(SOURCE_NOTICE)).not.toBeInTheDocument();
  });

  // 답변 아래에는 법제처 출처 표기만 둔다(법제처 이용안내상 출처 표기는 의무).
  test("place=bottom shows only the source notice", () => {
    render(<Notices place="bottom" />);
    expect(screen.getByText(SOURCE_NOTICE)).toBeInTheDocument();
    expect(screen.queryByText(DISCLAIMER)).not.toBeInTheDocument();
    expect(screen.queryByText(PII_NOTICE)).not.toBeInTheDocument();
  });
});
