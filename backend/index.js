import express from "express";
import dotenv from "dotenv";
import { pushInbox, readState } from "./store_pg.js";

dotenv.config();

const PORT = process.env.PORT || 8800

const app = express();

app.use(express.json());

app.post("/inbox", async (req, res) => {
  const text = (req.body?.text || "").trim();
  if (!text) {
    res.status(400).json({ error: "text required" });
    return;
  }
  const id = await pushInbox(text);
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

app.listen(PORT, () => {
    console.log(`Server running at port ${PORT}`);
})