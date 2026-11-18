// 로그인 화면. 계정은 두 개(admin, test)이고 비밀번호는 서버가 해시로만 안다(app/api/auth.py).
// 사용처: App.tsx(로그인하지 않았을 때). 성공하면 onLoggedIn으로 사용자 정보를 알린다.
import { useMutation } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { ApiError, login } from "../api";
import type { User } from "../types";

interface LoginFormProps {
  onLoggedIn: (user: User) => void;
}

export function LoginForm({ onLoggedIn }: LoginFormProps) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");

  // useMutation: "서버에 무언가를 시키는 요청"을 다루는 React Query 도구. 진행 중(isPending)과 실패(error) 상태를 대신 관리해 준다.
  const mutation = useMutation({
    mutationFn: (credentials: { username: string; password: string }) => login(credentials.username, credentials.password),
    onSuccess: onLoggedIn,
  });

  function submit(event: FormEvent) {
    event.preventDefault(); // 폼의 기본 동작(페이지 새로고침)을 막는다
    if (!username || !password || mutation.isPending) return;
    mutation.mutate({ username, password });
  }

  // 서버가 준 문장(아이디·비밀번호 오류, 시도 과다)을 그대로 보인다. 예상 밖의 오류는 일반 문구로 대신한다.
  const errorMessage = mutation.error ? (mutation.error instanceof ApiError ? mutation.error.message : "로그인하지 못했습니다.") : null;

  return (
    <form className="login-form" onSubmit={submit}>
      <h2>로그인</h2>
      <label>
        아이디
        <input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" maxLength={64} />
      </label>
      <label>
        비밀번호
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="current-password"
          maxLength={256}
        />
      </label>
      {errorMessage && (
        <p className="form-error" role="alert">
          {errorMessage}
        </p>
      )}
      <button type="submit" disabled={!username || !password || mutation.isPending}>
        {mutation.isPending ? "로그인 중…" : "로그인"}
      </button>
    </form>
  );
}
