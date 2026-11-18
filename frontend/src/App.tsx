// 앱의 뼈대: 로그인 여부에 따라 로그인 화면 또는 채팅 화면을 보여 준다.
// 사용처: main.tsx. 화면 맨 위의 고정 안내(자문 아님·개인정보 주의)는 로그인 전후와 상관없이 늘 보인다.
//
// 핵심 개념 — 로그인 여부는 "서버에 물어서" 안다: 세션 쿠키는 브라우저의 스크립트가 읽을 수 없는 HttpOnly 쿠키라서(app/api/auth.py),
// 화면은 /api/me를 불러 확인한다. React Query의 useQuery가 그 결과를 캐시하고, 로그인·로그아웃 때 우리가 캐시를 직접 바꾼다.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, fetchMe, logout } from "./api";
import { ChatView } from "./components/ChatView";
import { LoginForm } from "./components/LoginForm";
import { Notices } from "./components/Notices";
import type { User } from "./types";

const ME_KEY = ["me"] as const;

export default function App() {
  const queryClient = useQueryClient();
  const me = useQuery({ queryKey: ME_KEY, queryFn: fetchMe });
  const setUser = (user: User | null) => queryClient.setQueryData(ME_KEY, user);

  const logoutMutation = useMutation({
    mutationFn: logout,
    // 서버에서 로그아웃에 성공한 뒤에만 화면을 바꾼다. 실패했는데 로그아웃된 것처럼 보이면 쿠키가 남아 있어 거짓 화면이 된다.
    onSuccess: () => setUser(null),
  });

  let content;
  if (me.data === undefined) {
    // 아직 모른다: 확인 중이거나, 서버에 닿지 못해 실패했다. 후자를 "로그아웃 상태"로 보여 주면 거짓이다.
    content = me.isError ? (
      <div className="status-box">
        <p role="alert">{me.error instanceof ApiError ? me.error.message : "로그인 상태를 확인하지 못했습니다."}</p>
        <button type="button" onClick={() => void me.refetch()}>
          다시 시도
        </button>
      </div>
    ) : (
      <p className="status-box">불러오는 중…</p>
    );
  } else if (me.data === null) {
    content = <LoginForm onLoggedIn={setUser} />;
  } else {
    content = <ChatView key={me.data.username} onSessionExpired={() => setUser(null)} />;
  }

  return (
    <div className="app">
      <header className="app-header">
        <h1>근로·노동 상담</h1>
        {me.data && (
          <div className="account">
            <span className="account-name">{me.data.username}</span>
            <button type="button" className="secondary" onClick={() => logoutMutation.mutate()} disabled={logoutMutation.isPending}>
              로그아웃
            </button>
          </div>
        )}
      </header>
      <Notices place="top" />
      {logoutMutation.isError && (
        <p className="form-error" role="alert">
          {logoutMutation.error instanceof ApiError ? logoutMutation.error.message : "로그아웃하지 못했습니다."}
        </p>
      )}
      <main>{content}</main>
    </div>
  );
}
