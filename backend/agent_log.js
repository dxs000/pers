import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

const here = path.dirname(fileURLToPath(import.meta.url));

export function logPath() {
  const fromEnv = (process.env.AGENT_LOG || "").trim();
  if (fromEnv) return fromEnv;
  const agentDir = (process.env.AGENT_DIR || "").trim()
    || path.resolve(here, "..", "agent");
  return path.join(agentDir, "var", "agent.log");
}

export function tailLog(maxLines = 200, maxBytes = 256 * 1024) {
  const file = logPath();
  if (!fs.existsSync(file)) {
    return { path: file, missing: true, lines: [] };
  }
  const stat = fs.statSync(file);
  const size = stat.size;
  const fd = fs.openSync(file, "r");
  try {
    const start = Math.max(0, size - maxBytes);
    const buf = Buffer.alloc(size - start);
    fs.readSync(fd, buf, 0, buf.length, start);
    let text = buf.toString("utf8");
    if (start > 0) {
      const cut = text.indexOf("\n");
      if (cut >= 0) text = text.slice(cut + 1);
    }
    const lines = text.split(/\r?\n/).filter((line) => line.length > 0);
    return { path: file, missing: false, lines: lines.slice(-maxLines) };
  } finally {
    fs.closeSync(fd);
  }
}
