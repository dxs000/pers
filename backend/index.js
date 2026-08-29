import express from "express";
import dotenv from "dotenv";
import { pushInbox } from "./store_pg";

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

app.listen(PORT, () => {
    console.log(`Server running at port ${PORT}`);
})