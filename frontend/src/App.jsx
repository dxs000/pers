import { useEffect, useRef, useState } from "react";
import EssayBar from "./EssayBar";

const API = import.meta.env.VITE_API_URL;
const RECONNECT_MS = 3_000;

export default function App() {
  const [text, setText] = useState("");
  const [inboxId, setInboxId] = useState(null);
  const [status, setStatus] = useState(null);
  const [log, setLog] = useState([]);
  const [live, setLive] = useState(false);
  const [shelf, setShelf] = useState(null);
  const [convert, setConvert] = useState(null);
  const [tab, setTab] = useState("talk");
  const [agentLog, setAgentLog] = useState({ lines: [], missing: false });
  const logEndRef = useRef(null);
  const inboxIdRef = useRef(null);
  const fileRef = useRef(null);

  const busy = status?.state === "waiting" && !status?.timeout;

  async function loadSession() {
    const res = await fetch(`${API}/session`);
    const data = await res.json();
    setLog(data.messages ?? []);
  }

  async function loadShelf() {
    const res = await fetch(`${API}/shelf`);
    if (!res.ok) return;
    setShelf(await res.json());
  }

  useEffect(() => {
    inboxIdRef.current = inboxId;
  }, [inboxId]);

  useEffect(() => {
    loadSession();
    loadShelf();
    const timer = setInterval(loadShelf, 20_000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    let src;
    let timer;
    let stopped = false;

    function connect() {
      src = new EventSource(`${API}/events`);
      src.addEventListener("ping", () => setLive(true));
      src.addEventListener("reply", async () => {
        setLive(true);
        await loadSession();
        await loadShelf();
        const id = inboxIdRef.current;
        if (id == null) return;
        const res = await fetch(`${API}/inbox/${id}`);
        const data = await res.json();
        if (data.state !== "waiting") setStatus(data);
      });
      src.onerror = () => {
        setLive(false);
        src.close();
        if (!stopped) timer = setTimeout(connect, RECONNECT_MS);
      };
    }

    connect();
    return () => {
      stopped = true;
      clearTimeout(timer);
      src?.close();
    };
  }, []);

  useEffect(() => {
    if (status?.state !== "waiting" || status?.timeout) return;
    const timer = setTimeout(() => {
      setStatus({ state: "waiting", text: null, timeout: true });
    }, 180_000);
    return () => clearTimeout(timer);
  }, [status]);

  useEffect(() => {
    if (tab !== "log") return;
    let stop = false;
    async function pull() {
      const res = await fetch(`${API}/log`);
      if (!res.ok || stop) return;
      setAgentLog(await res.json());
    }
    pull();
    const timer = setInterval(pull, 2000);
    return () => {
      stop = true;
      clearInterval(timer);
    };
  }, [tab]);

  useEffect(() => {
    if (tab !== "log") return;
    logEndRef.current?.scrollIntoView({ block: "end" });
  }, [tab, agentLog.lines]);

  useEffect(() => {
    if (!convert?.id || convert.state !== "running") return;
    const timer = setInterval(async () => {
      const res = await fetch(`${API}/shelf/convert/${convert.id}`);
      if (!res.ok) return;
      const data = await res.json();
      setConvert(data);
      if (data.state !== "running") loadShelf();
    }, 1500);
    return () => clearInterval(timer);
  }, [convert?.id, convert?.state]);

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
    setStatus({ state: "waiting", text: null });
    setInboxId(data.id);
    setText("");
  }

  async function uploadBook(e) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    const body = new FormData();
    body.append("file", file);
    setConvert({ state: "running", file: file.name, log: "" });
    const res = await fetch(`${API}/shelf/source`, { method: "POST", body });
    const data = await res.json();
    if (!res.ok) {
      setConvert({ state: "failed", file: file.name, log: data.error || "upload failed" });
      return;
    }
    setConvert({ id: data.id, state: "running", file: data.saved, log: "" });
  }

  const holding = shelf?.holding;
  const who = holding
    ? [holding.author, holding.title].filter(Boolean).join(". ")
    : null;
  const pct = holding ? Math.round((holding.progress ?? 0) * 100) : null;

  return (
    <div className="app">
      <header className="top">
        <div>
          <p className="eyebrow">очередь inbox</p>
          <h1>Реплика</h1>
          <p className="sub">
            он может написать первым — экран держит канал reply_ready
          </p>
        </div>
        <div className="top-right">
          <nav className="tabs">
            <button
              type="button"
              className={tab === "talk" ? "on" : ""}
              onClick={() => setTab("talk")}
            >
              разговор
            </button>
            <button
              type="button"
              className={tab === "log" ? "on" : ""}
              onClick={() => setTab("log")}
            >
              лог
            </button>
          </nav>
          <span className={live ? "live on" : "live"}>
            {live ? "канал жив" : "канал тих"}
          </span>
        </div>
      </header>

      <div className="shelf">
        <div className="shelf-row">
          {holding ? (
            <p>
              на руках «{who}»
              {pct != null ? ` — ${pct}%` : ""}
            </p>
          ) : (
            <p>
              на руках пусто
              {shelf?.waiting
                ? ` · на полке ${shelf.waiting}`
                : ""}
            </p>
          )}
          <label className="shelf-upload">
            положить книгу
            <input
              ref={fileRef}
              type="file"
              accept=".fb2,.zip,.epub,.pdf,.djvu,.djv,.txt,.md"
              onChange={uploadBook}
            />
          </label>
        </div>
        {holding?.notes?.[0] ? (
          <p className="shelf-note">{holding.notes[0]}</p>
        ) : null}
        <EssayBar essay={shelf?.essay} />
        {convert ? (
          <p className="shelf-job">
            {convert.state === "running" && `конвертирую ${convert.file || "книгу"}…`}
            {convert.state === "ok" && `${convert.file}: на полке. Каталог подхватит демон.`}
            {convert.state === "failed" && `${convert.file}: не прошло ворота или convert упал.`}
          </p>
        ) : null}
      </div>

      {tab === "talk" ? (
      <main className="stage">
        {log.length === 0 ? (
          <div className="empty">
            <h2>Можно молчать</h2>
            <p>
              Он пишет первым, если захочется. Реплика придёт
              сюда сама. Можно и написать — ляжет в inbox.
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
      ) : (
      <main className="stage log-stage">
        {agentLog.missing ? (
          <div className="empty">
            <h2>Файла лога ещё нет</h2>
            <p>
              Демон пишет в AGENT_LOG (по умолчанию agent/var/agent.log).
              После перезапуска agent.py строки появятся здесь.
            </p>
          </div>
        ) : (
          <pre className="agent-log">
            {(agentLog.lines || []).join("\n")}
            <span ref={logEndRef} />
          </pre>
        )}
      </main>
      )}

      {tab === "talk" && (
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
      )}
    </div>
  );
}
