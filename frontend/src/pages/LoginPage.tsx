import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";

import { useLogin, useSession } from "../api/hooks";
import { LogoMark } from "../app/icons";
import { Button } from "../components/Button";
import { PRODUCT_NAME } from "../config";
import { useDocumentTitle } from "../app/useDocumentTitle";
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

  useEffect(() => {
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
    <div className="flex min-h-screen items-center justify-center bg-bg px-4">
      <main className="w-full max-w-sm">
        <div className="mb-6 flex items-center gap-2">
          <LogoMark />
          <span className="text-lg font-semibold text-fg">{PRODUCT_NAME}</span>
        </div>
        <form
          onSubmit={onSubmit}
          aria-labelledby="login-title"
          className="flex flex-col gap-4 rounded-lg border border-border bg-surface p-6"
          noValidate
        >
          <h1 id="login-title" className="text-xl font-semibold text-fg">
            Sign in
          </h1>
          {notice !== null && (
            <p role="status" className="text-sm text-fg-muted">
              {notice}
            </p>
          )}
          <div className="flex flex-col gap-1.5">
            <label htmlFor="login-name" className="text-sm font-medium text-fg">
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
              className="h-9 rounded-md border border-border-strong bg-surface-sunken px-3 text-base text-fg"
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <label htmlFor="login-password" className="text-sm font-medium text-fg">
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
              className="h-9 rounded-md border border-border-strong bg-surface-sunken px-3 text-base text-fg"
            />
          </div>
          {login.isError && (
            <p id={errorId} role="alert" className="text-sm text-status-failed">
              {loginErrorMessage(login.error)}
            </p>
          )}
          <Button type="submit" variant="primary" loading={login.isPending} disabled={name.trim() === "" || password === ""}>
            Sign in
          </Button>
        </form>
        <p className="mt-4 text-xs text-fg-subtle">
          Accounts are created on the controller host with <code className="font-mono">meshforge-controller user create</code>.
        </p>
      </main>
    </div>
  );
}
