// 빌드·개발 서버·테스트 설정을 한 파일에 둔다(vitest/config의 defineConfig는 vite의 것에 `test` 항목 타입을 더한 것이다).
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  server: {
    // 개발 중에는 /api 요청을 로컬 백엔드(8000)로 넘긴다. 운영에서는 웹서버(Task 18)가 같은 일을 한다.
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
  test: {
    environment: "jsdom", // 브라우저 대신 DOM을 흉내 내는 실행 환경
    setupFiles: ["./src/test-setup.ts"],
    css: false, // 테스트에서는 CSS를 처리하지 않는다
  },
});
