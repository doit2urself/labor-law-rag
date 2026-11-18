import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, test, vi } from "vitest";
import App from "./App";
import { ApiError, fetchMe, login, logout, streamChat } from "./api";
import type { User } from "./types";

// 서버를 부르는 함수를 모두 가짜로 바꾼다. ApiError 같은 나머지는 진짜를 쓴다.
vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return { ...actual, fetchMe: vi.fn(), login: vi.fn(), logout: vi.fn(), streamChat: vi.fn(), sendFeedback: vi.fn() };
});

const fetchMeMock = vi.mocked(fetchMe);
const loginMock = vi.mocked(login);
const logoutMock = vi.mocked(logout);
const streamChatMock = vi.mocked(streamChat);

const TEST_USER: User = { username: "test", role: "test" };

function renderApp() {
  // 테스트마다 새 캐시를 쓰고, 실패해도 자동으로 다시 시도하지 않게 한다(실패 화면을 바로 볼 수 있게).
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const user = userEvent.setup();
  render(
    <QueryClientProvider client={client}>
      <App />
    </QueryClientProvider>,
  );
  return { user };
}

async function submitLogin(user: ReturnType<typeof userEvent.setup>, username = "test", password = "pw") {
  await user.type(screen.getByLabelText("아이디"), username);
  await user.type(screen.getByLabelText("비밀번호"), password);
  await user.click(screen.getByRole("button", { name: "로그인" }));
}

beforeEach(() => {
  fetchMeMock.mockReset();
  loginMock.mockReset();
  logoutMock.mockReset();
  streamChatMock.mockReset();
});

describe("App — 로그인 상태", () => {
  // 로그인 여부를 확인하는 동안에는 로그인 화면을 먼저 보여 주지 않는다(깜빡임 방지).
  test("shows a loading message while checking the session", () => {
    fetchMeMock.mockReturnValue(new Promise(() => {})); // 끝나지 않는 확인
    renderApp();
    expect(screen.getByText("불러오는 중…")).toBeInTheDocument();
    expect(screen.queryByLabelText("아이디")).not.toBeInTheDocument();
  });

  // 로그인하지 않았으면 로그인 화면이 보이고, 화면 위쪽 고정 안내(자문 아님·개인정보)도 이미 보인다.
  test("shows the login form and the top notices when logged out", async () => {
    fetchMeMock.mockResolvedValue(null);
    renderApp();
    expect(await screen.findByLabelText("아이디")).toBeInTheDocument();
    expect(screen.getByText("법률 자문이 아닙니다. 출처의 원문을 확인하세요")).toBeInTheDocument();
    expect(screen.getByText("개인정보를 입력하지 마세요")).toBeInTheDocument();
  });

  // 이미 로그인된 상태(쿠키가 살아 있음)면 곧바로 채팅 화면과 사용자 이름을 보여 준다.
  test("goes straight to the chat when already logged in", async () => {
    fetchMeMock.mockResolvedValue(TEST_USER);
    renderApp();
    expect(await screen.findByLabelText("질문 입력")).toBeInTheDocument();
    expect(screen.getByText("test")).toBeInTheDocument();
  });

  // 서버에 닿지 못해 로그인 여부를 모를 때 "로그인 안 함"으로 보여 주면 거짓이다. 안내와 다시 시도 단추를 보인다.
  test("an unreachable server shows an error with a retry button, not the login form", async () => {
    fetchMeMock.mockRejectedValueOnce(new ApiError(0, "서버에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요."));
    fetchMeMock.mockResolvedValueOnce(null);
    const { user } = renderApp();
    expect(await screen.findByRole("alert")).toHaveTextContent("서버에 연결하지 못했습니다");
    expect(screen.queryByLabelText("아이디")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "다시 시도" }));
    expect(await screen.findByLabelText("아이디")).toBeInTheDocument();
  });
});

describe("App — 로그인과 로그아웃", () => {
  // 로그인에 성공하면 채팅 화면으로 바뀐다. 입력한 아이디·비밀번호가 그대로 서버로 간다.
  test("logging in switches to the chat", async () => {
    fetchMeMock.mockResolvedValue(null);
    loginMock.mockResolvedValue(TEST_USER);
    const { user } = renderApp();
    await screen.findByLabelText("아이디");
    await submitLogin(user, "test", "pw");
    expect(await screen.findByLabelText("질문 입력")).toBeInTheDocument();
    expect(loginMock).toHaveBeenCalledWith("test", "pw");
  });

  // 로그인 실패(401)·시도 과다(429)의 서버 문장을 그대로 보이고 로그인 화면에 머문다.
  test.each([
    [401, "아이디 또는 비밀번호가 올바르지 않습니다."],
    [429, "로그인 시도가 너무 많습니다. 잠시 후 다시 시도해 주세요."],
  ])("a failed login (HTTP %i) shows the server sentence and stays on the form", async (status, message) => {
    fetchMeMock.mockResolvedValue(null);
    loginMock.mockRejectedValue(new ApiError(status, message));
    const { user } = renderApp();
    await screen.findByLabelText("아이디");
    await submitLogin(user, "admin", "wrong");
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.getByLabelText("아이디")).toBeInTheDocument();
  });

  // 아이디나 비밀번호가 비었으면 로그인 단추가 잠겨 있다.
  test("the login button is disabled until both fields are filled", async () => {
    fetchMeMock.mockResolvedValue(null);
    const { user } = renderApp();
    await screen.findByLabelText("아이디");
    expect(screen.getByRole("button", { name: "로그인" })).toBeDisabled();
    await user.type(screen.getByLabelText("아이디"), "test");
    expect(screen.getByRole("button", { name: "로그인" })).toBeDisabled();
    await user.type(screen.getByLabelText("비밀번호"), "pw");
    expect(screen.getByRole("button", { name: "로그인" })).toBeEnabled();
  });

  // 로그인 요청이 오가는 동안 단추가 잠겨서 두 번 눌리지 않는다.
  test("the login button is locked while the request is in flight", async () => {
    fetchMeMock.mockResolvedValue(null);
    let finish: (u: User) => void = () => {};
    loginMock.mockReturnValue(new Promise<User>((resolve) => (finish = resolve)));
    const { user } = renderApp();
    await screen.findByLabelText("아이디");
    await submitLogin(user);
    await waitFor(() => expect(screen.getByRole("button", { name: /로그인/ })).toBeDisabled());
    finish(TEST_USER);
    expect(await screen.findByLabelText("질문 입력")).toBeInTheDocument();
  });

  // 로그아웃하면 서버에 알리고 로그인 화면으로 돌아가며, 이전 대화는 화면에서 사라진다.
  test("logging out returns to the login form", async () => {
    fetchMeMock.mockResolvedValue(TEST_USER);
    logoutMock.mockResolvedValue(undefined);
    const { user } = renderApp();
    await user.click(await screen.findByRole("button", { name: "로그아웃" }));
    expect(await screen.findByLabelText("아이디")).toBeInTheDocument();
    expect(logoutMock).toHaveBeenCalledTimes(1);
    expect(screen.queryByLabelText("질문 입력")).not.toBeInTheDocument();
  });

  // 서버에서 로그아웃에 실패하면 로그인 화면으로 바꾸지 않고 채팅을 유지하며 이유를 보인다(쿠키가 남아 있는데 로그아웃된 것처럼 보이면 거짓이다).
  test("a failed logout keeps the chat and shows the reason", async () => {
    fetchMeMock.mockResolvedValue(TEST_USER);
    logoutMock.mockRejectedValue(new ApiError(0, "서버에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요."));
    const { user } = renderApp();
    await user.click(await screen.findByRole("button", { name: "로그아웃" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("서버에 연결하지 못했습니다");
    expect(screen.getByLabelText("질문 입력")).toBeInTheDocument();
    expect(screen.queryByLabelText("아이디")).not.toBeInTheDocument();
  });

  // 채팅 중 로그인이 풀리면(서버가 401) 로그인 화면으로 돌아간다.
  test("an expired session during a chat returns to the login form", async () => {
    fetchMeMock.mockResolvedValue(TEST_USER);
    streamChatMock.mockRejectedValueOnce(new ApiError(401, "로그인이 필요합니다."));
    const { user } = renderApp();
    await user.type(await screen.findByLabelText("질문 입력"), "q");
    await user.click(screen.getByRole("button", { name: "보내기" }));
    expect(await screen.findByLabelText("아이디")).toBeInTheDocument();
  });
});
