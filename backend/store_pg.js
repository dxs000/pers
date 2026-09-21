import pg from "pg";
import dotenv from "dotenv"

export const WAITING = "waiting";
export const ANSWERED = "answered";
export const DROPPED = "dropped";
export const CHANNEL_INBOX = "inbox_new";
export const CHANNEL_REPLY = "reply_ready";

dotenv.config();

const { Pool } = pg;

export const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
});

export async function pushInbox(text, ts = new Date()) {
  const result = await pool.query(
    "INSERT INTO inbox (ts, text) VALUES ($1, $2) RETURNING id",
    [ts, (text || "").trim()]
  );
  return result.rows[0].id;
}

export function inboxState(row) {
  if (!row || row.handled_at == null) return WAITING;
  if (row.reply_id != null) return ANSWERED;
  return DROPPED;
}

export async function readInbox(id) {
  const result = await pool.query(
    "SELECT handled_at, reply_id FROM inbox WHERE id = $1",
    [id]
  );
  return result.rows[0] ?? null;
}

export async function readReply(replyId) {
  const result = await pool.query(
    "SELECT text FROM messages WHERE id = $1",
    [replyId]
  );
  return result.rows[0]?.text ?? null;
}

export async function readState(inboxId) {
  const row = await readInbox(inboxId);
  const state = inboxState(row);
  if (state !== ANSWERED) return { state, text: null };
  const text = await readReply(row.reply_id);
  return { state, text };
}

export async function notify(channel, payload = "") {
  await pool.query("SELECT pg_notify($1, $2)", [channel, payload]);
}

export async function readOpenSessionMessages() {
  const session = await pool.query(
    `SELECT id
       FROM sessions
      WHERE closed_at IS NULL
      ORDER BY id DESC
      LIMIT 1`
  );
  if (!session.rows[0]) return [];

  const result = await pool.query(
    `SELECT id, role, text, ts
       FROM messages
      WHERE session_id = $1
      ORDER BY id ASC`,
    [session.rows[0].id]
  );
  return result.rows;
}

export async function readShelf() {
  const waiting = await pool.query(
    "SELECT count(*)::int AS n FROM books WHERE picked_at IS NULL"
  );
  const book = await pool.query(
    `SELECT id, title, author, length, position, picked_at
       FROM books
      WHERE picked_at IS NOT NULL AND closed_at IS NULL`
  );
  const row = book.rows[0] ?? null;
  if (!row) {
    return { holding: null, waiting: waiting.rows[0]?.n ?? 0, essay: await readEssay() };
  }
  const notes = await pool.query(
    `SELECT text
       FROM notes
      WHERE book_id = $1
      ORDER BY at DESC, id DESC
      LIMIT 2`,
    [row.id]
  );
  const length = row.length || 1;
  return {
    holding: {
      title: row.title,
      author: row.author,
      progress: Math.round((row.position / length) * 10000) / 10000,
      started: row.picked_at,
      notes: notes.rows.map((n) => n.text),
    },
    waiting: waiting.rows[0]?.n ?? 0,
    essay: await readEssay(),
  };
}

export async function readEssay() {
  try {
    const found = await pool.query(
      `SELECT e.id, e.title, e.why, e.file_path,
              e.opened_at, e.closed_at, e.closed_why,
              coalesce((
                SELECT count(*)::int FROM essay_passages p WHERE p.essay_id = e.id
              ), 0) AS passages,
              coalesce((
                SELECT sum(p.chars)::int FROM essay_passages p WHERE p.essay_id = e.id
              ), 0) AS chars
         FROM essays e
        ORDER BY (e.closed_at IS NULL) DESC,
                 coalesce(e.closed_at, e.opened_at) DESC
        LIMIT 1`
    );
    const row = found.rows[0];
    if (!row) return { state: "idle" };
    const closed = row.closed_why && String(row.closed_why).trim();
    let state = "writing";
    if (row.closed_at) {
      state = closed === "написал" ? "done" : "dropped";
    }
    return {
      state,
      title: row.title,
      why: row.why,
      passages: row.passages,
      chars: row.chars,
      opened_at: row.opened_at,
      closed_at: row.closed_at,
      closed_why: closed || null,
    };
  } catch (err) {
    console.error("readEssay:", err.message);
    return { state: "idle" };
  }
}

export async function listenReplies(onReply) {
  const client = new pg.Client({ connectionString: process.env.DATABASE_URL });
  await client.connect();
  client.on("notification", (msg) => {
    if (msg.channel === CHANNEL_REPLY) onReply(msg.payload || "");
  });
  await client.query(`LISTEN ${CHANNEL_REPLY}`);
  return client;
}

// Вкладка «жизнь» (Шаг 57): то, что персонаж делает и чем становится, пока
// никто не смотрит. Только чтение. Каждый срез обёрнут отдельно: база, на
// которую ещё не накатили 0015/0016, отдаёт то, что есть, а не падает целиком.
async function safeRows(sql, params = []) {
  try {
    return (await pool.query(sql, params)).rows;
  } catch (err) {
    console.error("readLife:", err.message);
    return [];
  }
}

// Шаг 61: мысли вслух. Внутреннее — то, что он делал и видел один: дела по
// своей воле (кроме «ничего»), сны, прожитое днём, вспомнившееся. Отдаётся
// рядом с разговором, чтобы экран не молчал, когда молчит только чат.
// Схема та же, что у агента: ничего нового в базе ради этого не заводится.
export async function readInner(hours = 36) {
  const pursuits = await safeRows(
    `SELECT 'p' || id AS id, 'pursuit' AS kind, action, about, outcome AS text, at AS ts
       FROM pursuits
      WHERE action <> 'rest' AND at > now() - make_interval(hours => $1)
      ORDER BY at`,
    [hours]
  );
  const memories = await safeRows(
    `SELECT 'm' || id AS id, source AS kind, NULL AS action, NULL AS about, text,
            created_at AS ts
       FROM memories
      WHERE source IN ('dream', 'lived', 'inferred')
        AND created_at > now() - make_interval(hours => $1)
      ORDER BY created_at`,
    [hours]
  );
  return [...pursuits, ...memories].sort((a, b) => new Date(a.ts) - new Date(b.ts));
}

export async function readLife() {
  const [agent] = await safeRows(
    `SELECT name, born_at, birthplace, place_label, traits, mood, mood_reason, mood_since
       FROM agent WHERE id = 1`
  );
  const drives = await safeRows(
    `SELECT d.id, d.kind, d.text, d.basis, d.opened_at, d.closed_at, d.closed_why,
            round(drive_score(d.strength, d.touched_at, now())::numeric, 2)::float AS score,
            coalesce(array_agg(s.memory_id ORDER BY s.memory_id)
                     FILTER (WHERE s.memory_id IS NOT NULL), '{}') AS sources
       FROM drives d LEFT JOIN drive_sources s ON s.drive_id = d.id
      GROUP BY d.id
      ORDER BY (d.closed_at IS NULL) DESC, coalesce(d.closed_at, d.opened_at) DESC
      LIMIT 20`
  );
  const traits = await safeRows(
    `SELECT name, reason, set_at, dropped_at FROM trait_history
      ORDER BY coalesce(dropped_at, set_at) DESC, id DESC LIMIT 30`
  );
  const pursuits = await safeRows(
    `SELECT id, at, action, why, about, outcome FROM pursuits
      ORDER BY at DESC, id DESC LIMIT 30`
  );
  const memories = await safeRows(
    `SELECT id, happened_at, precision, text, source, created_at FROM memories
      ORDER BY created_at DESC, id DESC LIMIT 20`
  );
  const total = await safeRows("SELECT count(*)::int AS n FROM memories");
  // Шаг 59–60: он и голос. Отдельные запросы через safeRows: на базе до
  // миграции 0018 колонок нет, и панель должна показать остальное.
  const [him] = await safeRows(
    `SELECT him_view AS view, him_at AS at, talk, talk_why FROM agent WHERE id = 1`
  );
  const himFacts = await safeRows(
    `SELECT id, text, noted_at, hits FROM him_facts
      WHERE dropped_at IS NULL ORDER BY touched_at DESC, id DESC LIMIT 20`
  );
  const himThreads = await safeRows(
    `SELECT id, text, opened_at FROM threads
      WHERE side = 'user' AND closed_at IS NULL ORDER BY touched_at DESC`
  );
  return {
    agent: agent ?? null,
    drives,
    traits,
    pursuits,
    memories,
    memoriesTotal: total[0]?.n ?? 0,
    him: him ? { ...him, facts: himFacts, threads: himThreads } : null,
  };
}
