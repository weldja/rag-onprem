import { useState, type FormEvent } from "react";
import { api, type Me } from "../api";

export default function Account({ me }: { me: Me }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [repeat, setRepeat] = useState("");
  const [status, setStatus] = useState<{ ok: boolean; text: string } | null>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (next !== repeat) {
      setStatus({ ok: false, text: "The new passwords don't match" });
      return;
    }
    try {
      await api.changePassword(current, next);
      setStatus({ ok: true, text: "Password changed. Other sessions have been signed out." });
      setCurrent("");
      setNext("");
      setRepeat("");
    } catch (err) {
      setStatus({ ok: false, text: err instanceof Error ? err.message : "Failed" });
    }
  };

  return (
    <section className="page">
      <h2>Account</h2>
      <p>
        Signed in as <strong>{me.username}</strong>
        {me.is_admin && " (administrator)"}. Groups:{" "}
        {me.groups.length ? me.groups.map((g) => g.name).join(", ") : "none"}.
      </p>
      <form className="card form-grid" onSubmit={submit}>
        <h3>Change password</h3>
        <label>
          Current password
          <input
            type="password"
            autoComplete="current-password"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
            required
          />
        </label>
        <label>
          New password (at least 12 characters)
          <input
            type="password"
            autoComplete="new-password"
            minLength={12}
            value={next}
            onChange={(e) => setNext(e.target.value)}
            required
          />
        </label>
        <label>
          Repeat new password
          <input
            type="password"
            autoComplete="new-password"
            value={repeat}
            onChange={(e) => setRepeat(e.target.value)}
            required
          />
        </label>
        {status && (
          <p className={status.ok ? "note" : "error"} role="status">
            {status.text}
          </p>
        )}
        <button className="primary">Change password</button>
      </form>
    </section>
  );
}
