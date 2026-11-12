// Presidio Redactor: an MCP App for Claude Desktop.
//
// One tool is visible to Claude (open_redactor). It opens a panel inside the
// chat. Everything the panel does (redact, restore, reset) goes through tools
// marked visibility: ["app"], which Claude cannot see or call. Raw text and
// files go from the panel to this local process and on to the Presidio worker
// over stdin/stdout. Only text the user chooses to send reaches the chat.

import { spawn } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  registerAppResource,
  registerAppTool,
  RESOURCE_MIME_TYPE,
} from "@modelcontextprotocol/ext-apps/server";
import { z } from "zod";

const VERSION = "1.0.0";
const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..");
const PANEL_URI = "ui://presidio-redactor/panel.html";
const MAX_FILE_BYTES = 10 * 1024 * 1024;

const log = (...a) => process.stderr.write(`[presidio-redactor] ${a.join(" ")}\n`);

// ---------- Presidio worker (child process, no network port) ----------

function findPython() {
  if (process.env.REDACTOR_PYTHON) return process.env.REDACTOR_PYTHON;
  const candidates = [
    path.join(ROOT, ".venv", "bin", "python"),
    path.join(ROOT, ".venv", "Scripts", "python.exe"),
  ];
  return candidates.find((p) => existsSync(p)) ?? "python3";
}

class Worker {
  constructor() {
    this.pending = new Map();
    this.nextId = 1;
    this.readyPromise = null;
    this.start();
  }

  start() {
    const python = findPython();
    const script = path.join(ROOT, "worker", "worker.py");
    log(`starting worker: ${python} ${script}`);
    this.proc = spawn(python, [script], { stdio: ["pipe", "pipe", "pipe"], windowsHide: true });
    let buffer = "";
    let resolveReady;
    let rejectReady;
    this.readyPromise = new Promise((res, rej) => {
      resolveReady = res;
      rejectReady = rej;
    });
    this.readyPromise.catch(() => {}); // surfaced through request()

    this.proc.stdout.setEncoding("utf8");
    this.proc.stdout.on("data", (chunk) => {
      buffer += chunk;
      let nl;
      while ((nl = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, nl).trim();
        buffer = buffer.slice(nl + 1);
        if (!line) continue;
        let msg;
        try {
          msg = JSON.parse(line);
        } catch {
          log("worker sent non-JSON output (ignored)");
          continue;
        }
        if (msg.event === "ready") {
          log("worker ready");
          resolveReady();
          continue;
        }
        const p = this.pending.get(msg.id);
        if (!p) continue;
        this.pending.delete(msg.id);
        clearTimeout(p.timer);
        msg.ok ? p.resolve(msg.result) : p.reject(new Error(msg.error));
      }
    });
    // Worker logs go to our stderr (Claude Desktop's MCP log), never to stdout.
    this.proc.stderr.on("data", (d) => process.stderr.write(d));
    this.proc.on("error", (err) => {
      log(`could not start worker: ${err.message}`);
      rejectReady(new Error(`Could not start Python (${python}). Run the setup script first.`));
    });
    this.proc.on("exit", (code) => {
      log(`worker exited (${code})`);
      rejectReady(new Error("The Presidio worker stopped while starting. Check the setup."));
      for (const p of this.pending.values()) {
        clearTimeout(p.timer);
        p.reject(new Error("The Presidio worker stopped. It will restart on the next request."));
      }
      this.pending.clear();
      this.proc = null;
    });
  }

  async request(op, payload = {}, timeoutMs = 120_000) {
    if (!this.proc) this.start();
    await this.readyPromise;
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error("Presidio took too long to respond."));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      this.proc.stdin.write(JSON.stringify({ id, op, ...payload }) + "\n");
    });
  }

  stop() {
    this.proc?.kill();
  }
}

// ---------- MCP server ----------

function loadPanelHtml() {
  const file = path.join(ROOT, "dist", "panel.html");
  if (!existsSync(file)) throw new Error(`Panel not built: ${file}`);
  return readFileSync(file, "utf8");
}

// Files arrive in chunks (the MCP stdio transport caps one message at 10 MB).
// Chunks live in memory only and are dropped after use or after 10 minutes.
const uploads = new Map(); // upload_id -> { parts: [], total, bytes, at }
const UPLOAD_TTL_MS = 10 * 60 * 1000;

function sweepUploads() {
  const now = Date.now();
  for (const [id, u] of uploads) if (now - u.at > UPLOAD_TTL_MS) uploads.delete(id);
}

const errorResult = (err) => ({
  isError: true,
  content: [{ type: "text", text: `Error: ${err.message}` }],
  structuredContent: { error: err.message },
});

export function createServer(worker) {
  const server = new McpServer({ name: "presidio-redactor", version: VERSION });

  // The panel itself.
  registerAppResource(
    server,
    "Redaction panel",
    PANEL_URI,
    { description: "Local redaction panel powered by Microsoft Presidio" },
    async () => ({
      contents: [{ uri: PANEL_URI, mimeType: RESOURCE_MIME_TYPE, text: loadPanelHtml() }],
    }),
  );

  // Visible to Claude: the only tool the model can call.
  registerAppTool(
    server,
    "open_redactor",
    {
      title: "Open private redaction panel",
      description:
        "Opens a panel inside the chat where the user can paste text or drop a file " +
        "(PDF, Word, PowerPoint, Excel, text). It is redacted on the user's own computer " +
        "with Microsoft Presidio before anything reaches the conversation. Use this when the " +
        "user wants to share sensitive or confidential material, or asks to redact, anonymize " +
        "or mask something. Never ask the user to paste sensitive content directly into the chat.",
      inputSchema: {},
      _meta: { ui: { resourceUri: PANEL_URI } },
    },
    async () => ({
      content: [
        {
          type: "text",
          text:
            "The private redaction panel is open. Wait for the user to send the redacted " +
            "content into the chat. In that content, placeholders such as <PERSON_1> or " +
            "<EMAIL_ADDRESS_2> stand for hidden details. Keep every placeholder exactly as " +
            "written in your replies, never guess what it stands for, and do not ask the user " +
            "to reveal it.",
        },
      ],
    }),
  );

  // App-only: hidden from Claude, callable only by the panel.
  const appOnly = { ui: { resourceUri: PANEL_URI, visibility: ["app"] } };

  registerAppTool(
    server,
    "upload_chunk",
    {
      title: "Upload part of a file",
      description: "Panel-only. Receives one base64 chunk of a file to redact.",
      inputSchema: {
        upload_id: z.string().min(8).max(64),
        index: z.number().int().min(0),
        total: z.number().int().min(1).max(64),
        data: z.string(),
      },
      _meta: appOnly,
    },
    async ({ upload_id, index, total, data }) => {
      try {
        sweepUploads();
        let u = uploads.get(upload_id);
        if (!u) {
          u = { parts: new Array(total), total, bytes: 0, at: Date.now() };
          uploads.set(upload_id, u);
        }
        if (u.total !== total || index >= total) throw new Error("Upload chunks don't match.");
        u.bytes += (data.length * 3) / 4 - (u.parts[index] ? (u.parts[index].length * 3) / 4 : 0);
        if (u.bytes > MAX_FILE_BYTES) {
          uploads.delete(upload_id);
          throw new Error("File is larger than 10 MB.");
        }
        u.parts[index] = data;
        const received = u.parts.filter(Boolean).length;
        return {
          content: [{ type: "text", text: `Received part ${index + 1} of ${total}.` }],
          structuredContent: { received, total },
        };
      } catch (err) {
        return errorResult(err);
      }
    },
  );

  registerAppTool(
    server,
    "redact_content",
    {
      title: "Redact text or a file",
      description: "Panel-only. Redacts pasted text or an uploaded file locally.",
      inputSchema: {
        text: z.string().optional(),
        file_name: z.string().optional(),
        file_b64: z.string().optional(),
        upload_id: z.string().optional(),
        entities: z.array(z.string()).optional(),
        custom_terms: z.array(z.string()).optional(),
        remove_images: z.boolean().optional(),
      },
      _meta: appOnly,
    },
    async ({ text, file_name, file_b64, upload_id, entities, custom_terms, remove_images }) => {
      try {
        if (upload_id) {
          const u = uploads.get(upload_id);
          uploads.delete(upload_id);
          if (!u) throw new Error("Upload not found or expired. Please add the file again.");
          if (u.parts.filter(Boolean).length !== u.total) throw new Error("Upload is incomplete. Please add the file again.");
          file_b64 = u.parts.join("");
        }
        if (file_b64 && (file_b64.length * 3) / 4 > MAX_FILE_BYTES) {
          throw new Error("File is larger than 10 MB.");
        }
        const result = await worker.request("redact", {
          text,
          file_name,
          file_b64,
          entities,
          custom_terms,
          remove_images,
        });
        return {
          // Short summary only. The masked text travels in structuredContent.
          content: [{ type: "text", text: `Redacted ${result.total} item(s) from ${result.source}.` }],
          structuredContent: result,
        };
      } catch (err) {
        return errorResult(err);
      }
    },
  );

  registerAppTool(
    server,
    "preview_page",
    {
      title: "Preview a redacted page",
      description: "Panel-only. Returns one page of a redacted PDF as an image.",
      inputSchema: { doc_id: z.string(), page: z.number().int().min(1) },
      _meta: appOnly,
    },
    async ({ doc_id, page }) => {
      try {
        const r = await worker.request("doc_page", { doc_id, page, dpi: 90 });
        return {
          content: [{ type: "text", text: `Page ${page} of ${r.pages}.` }],
          structuredContent: { png_b64: r.png_b64, pages: r.pages, page },
        };
      } catch (err) {
        return errorResult(err);
      }
    },
  );

  // Visible to Claude: reads a document the user already redacted in the panel.
  // Only redacted copies exist in memory, so this can never return an original.
  server.registerTool(
    "read_redacted_document",
    {
      title: "Read a redacted document",
      description:
        "Reads a PDF that the user redacted in the private redaction panel, given the document id " +
        "they sent (it looks like doc_xxxxxxxxxxxx). Returns each page as an image, so you can see " +
        "the layout, plus the page's redacted text. Hidden details appear as placeholders such as " +
        "<PERSON_1>: keep them exactly as written and never guess what they stand for. Read up to " +
        "8 pages per call; use start_page for longer documents.",
      inputSchema: {
        doc_id: z.string().describe("The document id from the user's message"),
        start_page: z.number().int().min(1).optional().describe("First page to read (default 1)"),
        max_pages: z.number().int().min(1).max(8).optional().describe("Pages to read (default 8)"),
      },
    },
    async ({ doc_id, start_page = 1, max_pages = 8 }) => {
      try {
        const info = await worker.request("doc_info", { doc_id, require_verified: true });
        const last = Math.min(info.pages, start_page + max_pages - 1);
        if (start_page > info.pages) throw new Error(`The document has ${info.pages} page(s).`);
        const content = [
          {
            type: "text",
            text:
              `"${info.name}", pages ${start_page}-${last} of ${info.pages}. Redacted on the user's computer; ` +
              "grey boxes and <TYPE_n> placeholders mark hidden details." +
              (last < info.pages ? ` Call again with start_page=${last + 1} for the rest.` : ""),
          },
        ];
        for (let p = start_page; p <= last; p++) {
          const r = await worker.request("doc_page", { doc_id, page: p, dpi: 100, require_verified: true });
          content.push({ type: "image", data: r.png_b64, mimeType: "image/png" });
          content.push({ type: "text", text: `Page ${p} text (redacted):\n${r.text}` });
        }
        return { content };
      } catch (err) {
        return errorResult(err);
      }
    },
  );

  registerAppTool(
    server,
    "restore_content",
    {
      title: "Restore placeholders",
      description: "Panel-only. Swaps placeholders back to real values, locally.",
      inputSchema: { text: z.string() },
      _meta: appOnly,
    },
    async ({ text }) => {
      try {
        const result = await worker.request("restore", { text });
        return {
          // Never put restored (real) values in content.
          content: [{ type: "text", text: "Restore complete." }],
          structuredContent: result,
        };
      } catch (err) {
        return errorResult(err);
      }
    },
  );

  registerAppTool(
    server,
    "redactor_status",
    {
      title: "Redactor status",
      description: "Panel-only. Reports whether Presidio is ready and how many values are held.",
      inputSchema: {},
      _meta: appOnly,
    },
    async () => {
      try {
        const info = await worker.request("ping");
        const stats = await worker.request("stats");
        return {
          content: [{ type: "text", text: "Presidio is ready." }],
          structuredContent: { ...info, ...stats },
        };
      } catch (err) {
        return errorResult(err);
      }
    },
  );

  registerAppTool(
    server,
    "reset_codebook",
    {
      title: "Forget all placeholders",
      description: "Panel-only. Clears the in-memory codebook.",
      inputSchema: {},
      _meta: appOnly,
    },
    async () => {
      try {
        const result = await worker.request("reset");
        return { content: [{ type: "text", text: "Codebook cleared." }], structuredContent: result };
      } catch (err) {
        return errorResult(err);
      }
    },
  );

  return server;
}

async function main() {
  const worker = new Worker(); // starts loading spaCy right away
  const server = createServer(worker);
  // Headroom above the 10 MB default in case a host batches large messages.
  await server.connect(new StdioServerTransport(undefined, undefined, { maxBufferSize: 32 * 1024 * 1024 }));
  log(`server v${VERSION} running on stdio`);
  const shutdown = () => {
    worker.stop();
    process.exit(0);
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);
  process.stdin.on("close", shutdown);
}

main().catch((err) => {
  log(`fatal: ${err.stack || err.message}`);
  process.exit(1);
});
