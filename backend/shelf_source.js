import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));

export const KNOWN = [
  ".fb2",
  ".fb2.zip",
  ".zip",
  ".epub",
  ".pdf",
  ".djvu",
  ".djv",
  ".txt",
  ".md",
];

const jobs = new Map();
let nextId = 1;

export function libraryRoot() {
  const fromEnv = (process.env.LIBRARY_DIR || "").trim();
  if (fromEnv) return path.resolve(fromEnv);
  return path.resolve(HERE, "../agent/library");
}

export function agentRoot() {
  const fromEnv = (process.env.AGENT_DIR || "").trim();
  if (fromEnv) return path.resolve(fromEnv);
  return path.resolve(HERE, "../agent");
}

export function sourceDir() {
  return path.join(libraryRoot(), "source");
}

export function kindOf(name) {
  const lower = name.toLowerCase();
  return KNOWN.find((s) => lower.endsWith(s)) || null;
}

export function safeName(original) {
  const base = path.basename(original || "book");
  const suffix = kindOf(base);
  const stem = suffix ? base.slice(0, base.length - suffix.length) : base;
  const clean = stem.replace(/[^\p{L}\p{N}._ -]+/gu, "_").replace(/\s+/g, " ").trim() || "book";
  return suffix ? clean + suffix : clean;
}

export function ensureSourceDir() {
  fs.mkdirSync(sourceDir(), { recursive: true });
  return sourceDir();
}

export function getJob(id) {
  return jobs.get(Number(id)) || null;
}

export function startConvert(savedPath, { ocr = false } = {}) {
  const id = nextId++;
  const job = {
    id,
    state: "running",
    file: path.basename(savedPath),
    log: "",
    ok: null,
    code: null,
  };
  jobs.set(id, job);

  const py = (process.env.PYTHON || "python3").trim() || "python3";
  const script = path.join(agentRoot(), "convert.py");
  const args = ["convert.py", "--library", libraryRoot(), "--file", savedPath];
  if (ocr) args.push("--ocr");

  const child = spawn(py, args, {
    cwd: agentRoot(),
    env: process.env,
  });

  const append = (buf) => {
    job.log += buf.toString();
    if (job.log.length > 20_000) job.log = job.log.slice(-16_000);
  };
  child.stdout.on("data", append);
  child.stderr.on("data", append);
  child.on("error", (err) => {
    job.state = "failed";
    job.ok = false;
    job.log += `\n${err.message}`;
    if (!fs.existsSync(script)) {
      job.log += `\nнет ${script} — проверь AGENT_DIR`;
    }
  });
  child.on("close", (code) => {
    job.code = code;
    job.ok = /\[\s*ok\s*\]/i.test(job.log) && !/ОТКАЗ/.test(job.log);
    if (code !== 0 && job.ok !== true) job.ok = false;
    job.state = job.ok ? "ok" : "failed";
  });

  return job;
}
