import pg from "pg";
import dotenv from "dotenv"

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