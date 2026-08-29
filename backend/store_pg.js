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