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
