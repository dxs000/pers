import { useState } from "react";

const API = import.meta.env.VITE_API_URL;

export default function App() {
  const [text, setText] = useState("");
  const [inboxId, setInboxId] = useState(null);

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
  );
}