// 브라우저가 가장 먼저 실행하는 파일: React 앱을 index.html의 #root에 붙인다.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./styles.css";

// React Query: 서버 데이터를 가져와 캐시하고, 창으로 돌아오면 다시 확인해 준다(로그인이 풀렸는지 알아채는 데 쓰인다).
// retry: false — 실패했을 때 몰래 되풀이하지 않고 곧바로 오류를 보여 준다(서버의 무료 호출 한도를 아끼려는 뜻도 있다).
const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false } },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </React.StrictMode>,
);
