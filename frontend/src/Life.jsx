import { useEffect, useState } from "react";

const API = import.meta.env.VITE_API_URL;
const POLL_MS = 30_000;

// Слова те же, что у агента (drives.KIND_WORDS, agenda.ACTION_PAST): экран
// показывает его жизнь его же словами, а не кодами таблиц.
const KIND = {
  want: "хочет",
  fear: "боится",
  belief: "верит",
  question: "не даёт покоя",
};
const ACTION = {
  read: "читал",
  write: "писал своё",
  news: "смотрел новости",
  explore: "копался",
  recall: "вспоминал",
  daydream: "задумался",
  reach: "хотел написать",
  tend: "разбирался в твоём деле",
  rest: "ничего не делал",
};
const SOURCE = {
  genesis: "с рождения",
  told: "рассказал",
  dream: "снилось",
  inferred: "вспомнил",
  lived: "прожил",
};

function age(born) {
  if (!born) return null;
  const b = new Date(born);
  const n = new Date();
  let a = n.getFullYear() - b.getFullYear();
  if (n.getMonth() < b.getMonth() || (n.getMonth() === b.getMonth() && n.getDate() < b.getDate())) a -= 1;
  return a;
}

function when(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  return d.toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

function year(ts) {
  return ts ? new Date(ts).getFullYear() : "";
}

export default function Life() {
  const [life, setLife] = useState(null);

  useEffect(() => {
    let stop = false;
    async function pull() {
      const res = await fetch(`${API}/life`);
      if (!res.ok || stop) return;
      setLife(await res.json());
    }
    pull();
    const timer = setInterval(pull, POLL_MS);
    return () => {
      stop = true;
      clearInterval(timer);
    };
  }, []);

  if (!life) return <div className="empty"><p>смотрю…</p></div>;
  const a = life.agent;
  if (!a?.born_at) {
    return (
      <div className="empty">
        <h2>Ещё не родился</h2>
        <p>Первая реплика в разговоре — и у него появятся имя, место и прошлое.</p>
      </div>
    );
  }
  const open = life.drives.filter((d) => !d.closed_at);
  const closed = life.drives.filter((d) => d.closed_at);

  return (
    <div className="life">
      <section>
        <h2>
          {a.name}, {age(a.born_at)}
        </h2>
        <p className="life-sub">
          родом из {a.birthplace || "—"} · живёт: {a.place_label || "—"} · настроение: {a.mood}
          {a.mood_reason ? ` (${a.mood_reason})` : ""}
        </p>
        <p>{(a.traits || []).join(", ") || "черт пока нет"}</p>
      </section>

      <section>
        <h3>Ты для него</h3>
        {!life.him?.view && !(life.him?.facts || []).length ? (
          <p className="life-muted">
            пока никак: взгляд сложится после первого закрытого разговора
          </p>
        ) : (
          <>
            {life.him.view ? <p>{life.him.view}</p> : null}
            {(life.him.facts || []).length > 0 && (
              <ul>
                {life.him.facts.map((f) => (
                  <li key={f.id}>
                    {f.text}
                    <span className="life-muted"> · с {when(f.noted_at)}{f.hits > 1 ? ` · ×${f.hits}` : ""}</span>
                  </li>
                ))}
              </ul>
            )}
            {(life.him.threads || []).length > 0 && (
              <>
                <p className="life-muted">что у тебя сейчас происходит, как он это понял:</p>
                <ul>
                  {life.him.threads.map((t) => (
                    <li key={t.id}>{t.text}</li>
                  ))}
                </ul>
              </>
            )}
          </>
        )}
        {life.him?.talk != null && (
          <p className="life-muted">
            тяга говорить первым: {Number(life.him.talk).toFixed(2)}
            {life.him.talk_why ? ` — ${life.him.talk_why}` : ""}
          </p>
        )}
      </section>

      <section>
        <h3>Что в нём живёт</h3>
        {open.length === 0 ? (
          <p className="life-muted">пока ничего не выросло</p>
        ) : (
          <ul>
            {open.map((d) => (
              <li key={d.id}>
                <b>{KIND[d.kind]}:</b> {d.text}
                <span className="life-muted">
                  {" "}— {d.basis} · из #{(d.sources || []).join(", #")} · сила {d.score}
                </span>
              </li>
            ))}
          </ul>
        )}
        {closed.length > 0 && (
          <details>
            <summary>прошедшее ({closed.length})</summary>
            <ul>
              {closed.map((d) => (
                <li key={d.id} className="life-muted">
                  {KIND[d.kind]}: {d.text} — {d.closed_why}, {when(d.closed_at)}
                </li>
              ))}
            </ul>
          </details>
        )}
      </section>

      <section>
        <h3>Чем занимался сам</h3>
        {life.pursuits.length === 0 ? (
          <p className="life-muted">по своей воле ещё ничего не делал</p>
        ) : (
          <ul>
            {life.pursuits.map((p) => (
              <li key={p.id}>
                <span className="life-muted">{when(p.at)}</span> {ACTION[p.action] || p.action}
                {p.about ? ` (${p.about})` : ""}
                <span className="life-muted"> — {p.why}</span>
                {p.outcome ? <div className="life-outcome">{p.outcome}</div> : null}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section>
        <h3>
          Биография <span className="life-muted">({life.memoriesTotal}, свежие записи сверху)</span>
        </h3>
        <ul>
          {life.memories.map((m) => (
            <li key={m.id}>
              <span className="life-muted">
                #{m.id} · {year(m.happened_at)} · {SOURCE[m.source] || m.source}
              </span>{" "}
              {m.text}
            </li>
          ))}
        </ul>
      </section>

      <section>
        <h3>Как менялся характер</h3>
        {life.traits.length === 0 ? (
          <p className="life-muted">история черт начнётся со следующего пересмотра</p>
        ) : (
          <ul>
            {life.traits.map((t, i) => (
              <li key={i} className={t.dropped_at ? "life-muted" : ""}>
                <b>{t.name}</b> — {t.reason}
                <span className="life-muted">
                  {" "}· с {when(t.set_at)}
                  {t.dropped_at ? `, ушла ${when(t.dropped_at)}` : ""}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
