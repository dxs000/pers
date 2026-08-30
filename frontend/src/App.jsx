import { useEffect, useState } from "react";

const API = import.meta.env.VITE_API_URL;

export default function App() {
  const [text, setText] = useState("");
  const [inboxId, setInboxId] = useState(null);
  const [status, setStatus] = useState(null);
  const [log, setLog] = useState([]);

  const busy = status?.state === "waiting" && !status?.timeout;

  async function loadSession() {
    const res = await fetch(`${API}/session`);
    const data = await res.json();
    setLog(data.messages ?? []);
  }

  useEffect(() => {
    loadSession();
  }, []);

  useEffect(() => {
  const src = new EventSource(`${API}/events`);
  src.addEventListener("reply", async () => {
    await loadSession();
    if (inboxId == null) return;
    const res = await fetch(`${API}/inbox/${inboxId}`);
    const data = await res.json();
    if (data.state !== "waiting") setStatus(data);
  });
  return () => src.close();
}, [inboxId]);

  async function send(e) {
    e.preventDefault();
    const value = text.trim();
    if (!value) return;
    const res = await fetch(`${API}/inbox`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: value }),
    });
    const data = await res.json();
    if (!res.ok || data.id == null) return;
    setLog((prev) => [...prev, { role: "user", text: value }]);
    setInboxId(data.id);
    setText("");
  }

  return (
    <div className="app">
      <header className="top">
        <div>
          <p className="eyebrow">очередь inbox</p>
          <h1>Реплика</h1>
          <p className="sub">клиент пишет в inbox и читает ответ</p>
        </div>
        <div className="top-right">
          <label className="toggle">
            демон
            <span className="switch" />
          </label>
          <span>экран</span>
        </div>
      </header>

      <main className="stage">
        {log.length === 0 ? (
          <div className="empty">
            <h2>Напишите реплику</h2>
            <p>
              Она ляжет в очередь. Ответит агент, если он запущен
              и смотрит inbox.
            </p>
          </div>
        ) : (
          <div className="log">
            {log.map((item, i) => (
              <p key={item.id ?? i} className={item.role}>
                {item.text}
              </p>
            ))}
          </div>
        )}
      </main>

      <footer className="composer">
        <form onSubmit={send}>
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                e.currentTarget.form?.requestSubmit();
              }
            }}
            disabled={busy}
            autoFocus
            rows={1}
            placeholder="Напишите реплику"
          />
          <button type="submit" disabled={busy}>↑</button>
        </form>
        <p className="hint">
          Enter — отправить, Shift+Enter — новая строка.
        </p>
        {status?.state === "dropped" && (
          <p>эти слова дошли, но уже к прошлому разговору</p>
        )}
        {status?.timeout && (
          <p>нет ответа за 180 с — похоже, агент не запущен</p>
        )}
        {busy && <p>персонаж думает…</p>}
      </footer>
    </div>
  );
}