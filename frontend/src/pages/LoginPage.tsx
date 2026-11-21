import { useId, useLayoutEffect, useRef, useState, type FormEvent } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";

import { useLogin, useSession } from "../api/hooks";
import { Brand } from "../app/MainNav";
import { Button } from "../components/Button";
import { useDocumentTitle } from "../app/useDocumentTitle";
import { LoginComb } from "./LoginComb";
import { loginErrorMessage, type LoginState } from "./loginMessages";

export function LoginPage() {
  useDocumentTitle("Sign in");
  const session = useSession();
  const login = useLogin();
  const navigate = useNavigate();
  const location = useLocation();
  const state = (location.state ?? {}) as LoginState;
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const nameRef = useRef<HTMLInputElement>(null);
  const errorId = useId();

  // Layout effect: the name field has focus in the same commit that shows the form
  // (a passive effect can run a frame later, after the page is already visible).
  useLayoutEffect(() => {
    nameRef.current?.focus();
  }, []);

  if (session.data) {
    return <Navigate to={state.from ?? "/"} replace />;
  }

  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (login.isPending) return;
    login.mutate(
      { name: name.trim(), password },
      {
        onSuccess: () => {
          setPassword("");
          void navigate(state.from ?? "/", { replace: true });
        },
        onError: () => {
          setPassword("");
        },
      },
    );
  };

  const notice =
    state.reason === "expired"
      ? "Your session ended. Sign in again."
      : state.reason === "logged_out"
        ? "You are signed out."
        : null;

  return (
    <div className="mf-app mf-login relative flex min-h-screen overflow-hidden bg-bg text-fg">
      <LoginComb />
      <main className="mf-login-in relative z-10 m-auto flex w-full max-w-[25rem] flex-col items-center px-4 py-16">
        <div className="mb-8">
          <Brand />
        </div>
        <form
          onSubmit={onSubmit}
          aria-labelledby="login-title"
          className="ov-card ov-frost nd-glass mf-login-card flex w-full flex-col px-9 pt-9 pb-8"
          noValidate
        >
          <h1 id="login-title" className="text-[1.375rem] leading-none font-semibold text-fg">
            Sign in
          </h1>
          {notice !== null && (
            <p role="status" className="mt-5 flex items-center gap-2.5 text-sm text-fg">
              <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-[#ffc93c]" />
              {notice}
            </p>
          )}
          <div className="mt-8 flex flex-col gap-7">
            <div className="mf-login-field flex flex-col gap-2">
              <label htmlFor="login-name" className="nd-label">
                User name
              </label>
              <input
                ref={nameRef}
                id="login-name"
                name="username"
                autoComplete="username"
                autoCapitalize="none"
                spellCheck={false}
                required
                maxLength={64}
                value={name}
                onChange={(e) => {
                  setName(e.target.value);
                }}
                aria-describedby={login.isError ? errorId : undefined}
                className="mf-login-input"
              />
            </div>
            <div className="mf-login-field flex flex-col gap-2">
              <label htmlFor="login-password" className="nd-label">
                Password
              </label>
              <input
                id="login-password"
                name="password"
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(e) => {
                  setPassword(e.target.value);
                }}
                aria-describedby={login.isError ? errorId : undefined}
                className="mf-login-input"
              />
            </div>
          </div>
          {login.isError && (
            <p id={errorId} role="alert" className="mt-6 text-sm text-status-failed">
              {loginErrorMessage(login.error)}
            </p>
          )}
          <Button
            type="submit"
            variant="primary"
            loading={login.isPending}
            disabled={name.trim() === "" || password === ""}
            className="mf-login-btn mt-9"
          >
            Sign in
          </Button>
        </form>
      </main>
    </div>
  );
}
