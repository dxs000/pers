import express from "express";
import cors from "cors";
import dotenv from "dotenv";
import multer from "multer";
import path from "node:path";
import { pushInbox, readState, notify, readOpenSessionMessages, readShelf, listenReplies, CHANNEL_INBOX } from "./store_pg.js";
import { ensureSourceDir, kindOf, safeName, startConvert, getJob } from "./shelf_source.js";

dotenv.config();

const PORT = process.env.PORT || 8800;
const HEARTBEAT_MS = 25_000;

const app = express();
const subscribers = new Set();

listenReplies(() => broadcast("reply")).catch((err) => {
  console.error("LISTEN reply_ready:", err);
});

const upload = multer({
  storage: multer.diskStorage({
    destination: (_req, _file, cb) => {
      try {
        cb(null, ensureSourceDir());
      } catch (err) {
        cb(err);
      }
    },
    filename: (_req, file, cb) => cb(null, safeName(file.originalname)),
  }),
  limits: { fileSize: 80 * 1024 * 1024 },
  fileFilter: (_req, file, cb) => {
    if (!kindOf(file.originalname)) {
      cb(new Error("unknown format"));
      return;
    }
    cb(null, true);
  },
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

app.get("/shelf", async (_req, res) => {
  res.json(await readShelf());
});

app.post("/shelf/source", (req, res) => {
  upload.single("file")(req, res, (err) => {
    if (err) {
      res.status(400).json({ error: err.message || "upload failed" });
      return;
    }
    if (!req.file) {
      res.status(400).json({ error: "file required" });
      return;
    }
    const job = startConvert(req.file.path, { ocr: req.body?.ocr === "1" });
    res.status(202).json({
      id: job.id,
      saved: req.file.filename,
      state: job.state,
    });
  });
});

app.get("/shelf/convert/:id", (req, res) => {
  const job = getJob(req.params.id);
  if (!job) {
    res.status(404).json({ error: "no such job" });
    return;
  }
  res.json({
    id: job.id,
    state: job.state,
    file: job.file,
    ok: job.ok,
    log: job.log,
  });
});

app.get("/events", (req, res) => {
  res.setHeader("Content-Type", "text/event-stream");
  res.setHeader("Cache-Control", "no-cache");
  res.setHeader("Connection", "keep-alive");
  res.setHeader("X-Accel-Buffering", "no");
  res.flushHeaders();
  subscribers.add(res);
  writeEvent(res, "ping", {});
  req.on("close", () => subscribers.delete(res));
});

function writeEvent(res, event, data) {
  res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
}

function broadcast(event) {
  for (const res of subscribers) {
    writeEvent(res, event, {});
  }
}

setInterval(() => broadcast("ping"), HEARTBEAT_MS);

app.listen(PORT, () => {
  console.log(`Server running at port ${PORT}`);
});
