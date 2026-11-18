// 모든 테스트 파일이 시작하기 전에 한 번 실행된다.
import "@testing-library/jest-dom/vitest"; // toBeInTheDocument 같은 화면 검사용 단언을 vitest에 더한다
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// 테스트마다 화면에 그린 것을 지운다(전역 afterEach가 없는 설정이라 직접 등록해야 한다).
afterEach(() => {
  cleanup();
});
