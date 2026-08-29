import { useEffect, useState } from "react";

const API = import.meta.env.VITE_API_URL;

export default function App() {
  const [text, setText] = useState("");
  const [inboxId, setInboxId] = useState(null);
  const [status, setStatus] = useState(null);

  useEffect(() => {
    if (inboxId == null) return;
    setStatus({ state: "waiting", text: null });
    const started = Date.now();
    const timer = setInterval(async () => {
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
      }
    }, 2000);
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
    setInboxId(data.id);
    setText("");
  }

  

  return (
    <form onSubmit={send}>
      <input
        value={text}
        onChange={(e) => setText(e.target.value)}
        autoFocus
      />
      <button type="submit">Отправить</button>
      {inboxId != null && <p>inbox #{inboxId}</p>}
    </form>

    {status?.state === "answered" && <p>{status.text}</p>}
      {status?.state === "dropped" && (
        <p>эти слова дошли, но уже к прошлому разговору</p>
      )}
      {status?.timeout && (
        <p>нет ответа за 180 с — похоже, агент не запущен</p>
      )}
  );
 
}