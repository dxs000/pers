import express from "express";
import cors from "cors";
import dotenv from "dotenv";
import { pushInbox, readState, notify, readOpenSessionMessages, listenReplies, CHANNEL_INBOX } from "./store_pg.js";

dotenv.config();

const PORT = process.env.PORT || 8800

const app = express();

const subscribers = new Set();

listenReplies(() => broadcast()).catch((err) => {
  console.error("LISTEN reply_ready:", err);
});

app.use(express.json());
app.use(cors({ origin: "http://localhost:5173" }));

app.post("/inbox", async (req, res) => {
  const text = (req.body?.text || "").trim();
  if (!text) {
    res.status(400).json({ error: "text required" });
    return;
  }
  const id = await pushInbox(text);
  await notify(CHANNEL_INBOX, String(id));
  res.status(201).json({ id });
});

app.get("/inbox/:id", async (req, res) => {
  const id = Number(req.params.id);
  if (!Number.isInteger(id) || id < 1) {
    res.status(400).json({ error: "bad id" });
    return;
  }
  res.json(await readState(id));
});

app.get("/session", async (_req, res) => {
  const messages = await readOpenSessionMessages();
  res.json({ messages });
});

app.get("/events", (req, res) => {
  res.setHeader("Content-Type", "text/event-stream");
  res.setHeader("Cache-Control", "no-cache");
  res.setHeader("Connection", "keep-alive");
  res.flushHeaders();
  subscribers.add(res);
  req.on("close", () => subscribers.delete(res));
});

function broadcast() {
  for (const res of subscribers) {
    res.write("event: reply\ndata: {}\n\n");
  }
}

app.listen(PORT, () => {
    console.log(`Server running at port ${PORT}`);
})