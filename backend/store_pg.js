import pg from "pg";
import dotenv from "dotenv"

export const WAITING = "waiting";
export const ANSWERED = "answered";
export const DROPPED = "dropped";

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