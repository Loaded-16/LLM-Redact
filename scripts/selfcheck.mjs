// Self-check with no dependencies: starts dist/server.js the way Claude Desktop
// does, speaks raw MCP JSON-RPC over stdio, and redacts a sample sentence.
import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const proc = spawn(process.execPath, [path.join(ROOT, "dist", "server.js")], { stdio: ["pipe", "pipe", "pipe"] });
let buf = "";
const waiters = new Map();
let stderr = "";
proc.stderr.on("data", (d) => (stderr += d));
proc.stdout.on("data", (d) => {
  buf += d;
  let i;
  while ((i = buf.indexOf("\n")) >= 0) {
    const line = buf.slice(0, i);
    buf = buf.slice(i + 1);
    try {
      const msg = JSON.parse(line);
      waiters.get(msg.id)?.(msg);
    } catch {
      console.error("Server wrote something that is not JSON-RPC to stdout:", line.slice(0, 200));
      process.exit(1);
    }
  }
});
let id = 0;
const rpc = (method, params = {}) =>
  new Promise((resolve, reject) => {
    const myId = ++id;
    const t = setTimeout(() => reject(new Error(`${method} timed out`)), 120000);
    waiters.set(myId, (m) => (clearTimeout(t), m.error ? reject(new Error(m.error.message)) : resolve(m.result)));
    proc.stdin.write(JSON.stringify({ jsonrpc: "2.0", id: myId, method, params }) + "\n");
  });

try {
  await rpc("initialize", { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "selfcheck", version: "1" } });
  proc.stdin.write(JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }) + "\n");
  const { tools } = await rpc("tools/list");
  if (!tools.some((t) => t.name === "open_redactor")) throw new Error("open_redactor tool missing");
  const status = await rpc("tools/call", { name: "redactor_status", arguments: {} });
  if (status.isError) throw new Error(status.content[0].text);
  const f = status.structuredContent?.features || {};
  if (process.env.REDACTOR_NER === "gliner" && f.gliner !== "on") throw new Error("GLiNER add-on was requested but did not load.");
  if (process.env.REDACTOR_ARABIC === "1" && f.arabic !== "on") throw new Error("Arabic add-on was requested but did not load.");
  const r = await rpc("tools/call", {
    name: "redact_content",
    arguments: { text: "Email Rami Haddad at rami@example.com or call 03 123 456." },
  });
  if (r.isError) throw new Error(r.content[0].text);
  const masked = r.structuredContent.masked;
  if (masked.includes("Rami") || masked.includes("rami@") || masked.includes("123 456")) throw new Error(`Leak: ${masked}`);
  console.log(`Self-check passed (add-ons: ${["gliner", "arabic"].filter((k) => f[k] === "on").concat(f.ocr ? ["ocr"] : []).join(", ") || "none"}):`);
  console.log(`  in:  Email Rami Haddad at rami@example.com or call 03 123 456.`);
  console.log(`  out: ${masked}`);
  proc.kill();
  process.exit(0);
} catch (e) {
  console.error(`Self-check FAILED: ${e.message}`);
  console.error(stderr.split("\n").slice(-15).join("\n"));
  proc.kill();
  process.exit(1);
}
