import { useState, type FormEvent } from "react";
import { api, setToken, type User } from "../api";

export default function Login({ onLogin }: { onLogin: (u: User) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const r = await api<{ access_token: string; user: User }>("/auth/login", { method: "POST", json: { email, password } });
      setToken(r.access_token);
      onLogin(r.user);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="panel login" onSubmit={submit}>
      <h1>BIS — вход</h1>
      {error && <div className="alert error">{error}</div>}
      <div className="field">
        <label>Email</label>
        <input value={email} onChange={(e) => setEmail(e.target.value)} autoFocus autoComplete="username" />
      </div>
      <div className="field">
        <label>Пароль</label>
        <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />
      </div>
      <button className="btn primary" disabled={busy}>
        {busy ? "Вход…" : "Войти"}
      </button>
      <p className="muted small">Демо: admin@demo.bis / demo12345</p>
    </form>
  );
}
