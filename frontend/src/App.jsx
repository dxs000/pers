import { useEffect, useState } from "react";

const API = import.meta.env.VITE_API_URL;

export default function App() {
  const [text, setText] = useState("");
  const [inboxId, setInboxId] = useState(null);
  const [status, setStatus] = useState(null);
  const [log, setLog] = useState([]);

  const busy = status?.state === "waiting" && !status?.timeout;

  useEffect(() => {
    if (inboxId == null) return;
    setStatus({ state: "waiting", text: null });
    const started = Date.now();
    let timer;

    async function tick() {
      if (Date.now() - started > 180_000) {
        clearInterval(timer);
        setStatus({ state: "waiting", text: null, timeout: true });
        return;
      }
      const res = await fetch(`${API}/inbox/${inboxId}`);
      const data = await res.json();
      if (data.state !== "waiting") {
        clearInterval(timer);
        setStatus(data);
        if (data.state === "answered" && data.text) {
          setLog((prev) => [...prev, { role: "assistant", text: data.text }]);
        }
      }
    }

    tick();
    timer = setInterval(tick, 2000);
    return () => clearInterval(timer);
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
    setInboxId(data.id);
    setLog((prev) => [...prev, { role: "user", text: value }]);
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
          <span>демон</span>
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
              <p key={i}>
                {item.role === "user" ? "вы" : "агент"}: {item.text}
              </p>
            ))}
          </div>
        )}
      </main>

      <footer className="composer">
        <form onSubmit={send}>
          <input
            value={text}
            onChange={(e) => setText(e.target.value)}
            disabled={busy}
            autoFocus
            placeholder="Напишите реплику"
          />
          <button type="submit" disabled={busy}>↑</button>
        </form>
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