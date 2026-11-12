// Connects a real MCP client to dist/server.js over stdio (like Claude Desktop does)
// and checks tools, visibility, the panel resource, and that no real value ever
// appears in the model-readable `content` of any result.
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const SERVER = path.join(ROOT, "dist", "server.js");
let pass = 0;
let fail = 0;
const check = (name, cond, detail = "") => {
  cond ? pass++ : fail++;
  console.log(`${cond ? "PASS" : "FAIL"} ${name}${cond ? "" : `  -> ${typeof detail === "string" ? detail : JSON.stringify(detail).slice(0, 400)}`}`);
};

async function connect(env = {}) {
  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [SERVER],
    env: { ...process.env, ...env },
    stderr: "pipe",
  });
  const client = new Client(
    { name: "test-host", version: "1.0.0" },
    { capabilities: { extensions: { "io.modelcontextprotocol/ui": { mimeTypes: ["text/html;profile=mcp-app"] } } } },
  );
  await client.connect(transport);
  return client;
}

function makeBigDocx() {
  // A real .docx with a sensitive name, padded with an 8 MB random blob inside the zip.
  const py = path.join(ROOT, ".venv", "bin", "python");
  const out = execFileSync(py, ["-c", `
import docx, io, os, sys, zipfile
d = docx.Document(); d.add_paragraph("Talent: Nadine Saab, agent nadine@agency.com")
buf = io.BytesIO(); d.save(buf)
z = zipfile.ZipFile(buf, "a", zipfile.ZIP_STORED); z.writestr("word/media/pad.bin", os.urandom(8 * 1024 * 1024)); z.close()
sys.stdout.buffer.write(buf.getvalue())`], { maxBuffer: 64 * 1024 * 1024 });
  return out;
}

const SECRETS = ["Rami Haddad", "rami.haddad@acme.com", "03 123 456", "Sara Khoury", "Najjar", "Nadine Saab", "nadine@agency.com",
  "Maya Joseph", "Karim Aoun", "96171234567", "Lea Antoine", "0012345678901", "Elie Saab", "Nadine Abou Saab"];
const modelVisible = []; // every `content` block Claude could ever read

async function main() {
  const client = await connect();

  // tools
  const { tools } = await client.listTools();
  const byName = Object.fromEntries(tools.map((t) => [t.name, t]));
  check("lists 8 tools", tools.length === 8, tools.map((t) => t.name));
  const visibleToModel = tools.filter((t) => (t._meta?.ui?.visibility ?? ["model", "app"]).includes("model"));
  check("Claude sees only open_redactor and read_redacted_document",
    JSON.stringify(visibleToModel.map((t) => t.name).sort()) === '["open_redactor","read_redacted_document"]', visibleToModel.map((t) => t.name));
  for (const n of ["upload_chunk", "redact_content", "preview_page", "restore_content", "redactor_status", "reset_codebook"]) {
    check(`${n} is app-only`, JSON.stringify(byName[n]?._meta?.ui?.visibility) === '["app"]', byName[n]?._meta);
  }
  check("open_redactor links the panel", byName.open_redactor?._meta?.ui?.resourceUri === "ui://presidio-redactor/panel.html");

  // resource
  const res = await client.readResource({ uri: "ui://presidio-redactor/panel.html" });
  const c0 = res.contents[0];
  check("panel resource has MCP App mime type", c0.mimeType === "text/html;profile=mcp-app", c0.mimeType);
  check("panel is self-contained HTML", c0.text.startsWith("<!doctype html>") && !c0.text.includes('src="./panel.js"'));
  check("panel loads no external scripts", !/<script[^>]+src=/i.test(c0.text));

  // open
  const open = await client.callTool({ name: "open_redactor", arguments: {} });
  modelVisible.push(...open.content);
  check("open_redactor tells Claude to keep placeholders", open.content[0].text.includes("Keep every placeholder"));

  // status (waits for spaCy)
  const t0 = Date.now();
  const st = await client.callTool({ name: "redactor_status", arguments: {} });
  modelVisible.push(...st.content);
  check("status ready", st.structuredContent?.ready === true, st);
  console.log(`     (model load took ${((Date.now() - t0) / 1000).toFixed(1)}s)`);

  // redact
  const red = await client.callTool({
    name: "redact_content",
    arguments: {
      text: "Rami Haddad (rami.haddad@acme.com, 03 123 456) will brief Sara Khoury on the Najjar launch. Budget 70 000 000 LBP.",
      custom_terms: ["Najjar"],
    },
  });
  modelVisible.push(...red.content);
  const masked = red.structuredContent?.masked ?? "";
  check("redact: returns masked text", masked.startsWith("<PERSON_1> (<EMAIL_ADDRESS_1>, <LB_PHONE_1>) will brief <PERSON_2> on the <CUSTOM_1> launch."), masked);
  check("redact: amount preserved", masked.includes("70 000 000 LBP"), masked);
  check("redact: content is only a short summary", red.content.length === 1 && red.content[0].text.startsWith("Redacted 5 item(s)"), red.content);

  // file
  const fileRes = await client.callTool({
    name: "redact_content",
    arguments: { file_name: "note.txt", file_b64: Buffer.from("Ask Sara Khoury to call Rami Haddad.").toString("base64") },
  });
  modelVisible.push(...fileRes.content);
  check("redact file: consistent tags", fileRes.structuredContent?.masked === "Ask <PERSON_2> to call <PERSON_1>.", fileRes.structuredContent);

  // restore
  const rest = await client.callTool({ name: "restore_content", arguments: { text: "Thanks <PERSON_1>, cc <PERSON_2> re <CUSTOM_1>." } });
  modelVisible.push(...rest.content);
  check("restore: real values come back", rest.structuredContent?.restored === "Thanks Rami Haddad, cc Sara Khoury re Najjar.", rest.structuredContent);
  check("restore: content holds no restored text", rest.content[0].text === "Restore complete.", rest.content);

  // errors
  const bad = await client.callTool({ name: "redact_content", arguments: { file_name: "x.heic", file_b64: "eHg=" } });
  modelVisible.push(...bad.content);
  check("bad file: isError with a clear message", bad.isError === true && /Unsupported/.test(bad.content[0].text), bad);
  // chunked upload of a real ~8 MB Word file (text plus a large embedded blob)
  const docx = makeBigDocx();
  const b64 = docx.toString("base64");
  const CHUNK = 1_500_000;
  const total = Math.ceil(b64.length / CHUNK);
  const uploadId = "test-upload-0001";
  for (let i = 0; i < total; i++) {
    const up = await client.callTool({
      name: "upload_chunk",
      arguments: { upload_id: uploadId, index: i, total, data: b64.slice(i * CHUNK, (i + 1) * CHUNK) },
    });
    modelVisible.push(...up.content);
    if (up.isError) { check("chunk upload accepted", false, up.content); break; }
  }
  const bigRes = await client.callTool({ name: "redact_content", arguments: { upload_id: uploadId, file_name: "big.docx" } });
  modelVisible.push(...bigRes.content);
  check(`chunked ${(docx.length / 1048576).toFixed(1)} MB docx (${total} chunks): redacted`,
    !bigRes.isError && bigRes.structuredContent?.masked.includes("<PERSON_") && !bigRes.structuredContent.masked.includes("Nadine Saab"),
    bigRes.content);
  const reuse = await client.callTool({ name: "redact_content", arguments: { upload_id: uploadId, file_name: "big.docx" } });
  check("chunks are dropped after use", reuse.isError === true && /not found/.test(reuse.content[0].text), reuse.content);

  // over 10 MB through chunks
  let rejected = null;
  for (let i = 0; i < 10; i++) {
    const up = await client.callTool({ name: "upload_chunk", arguments: { upload_id: "too-big-0001", index: i, total: 10, data: "A".repeat(1_500_000) } });
    if (up.isError) { rejected = up.content[0].text; break; }
  }
  check("over 10 MB via chunks: rejected", /10 MB/.test(rejected ?? ""), rejected);

  // PDF redacted in place, then read by Claude
  const pdfPath = path.join(os.tmpdir(), `brief-${process.pid}.pdf`);
  execFileSync(path.join(ROOT, ".venv", "bin", "python"), [path.join(ROOT, "tests", "make_test_pdf.py"), pdfPath]);
  const pdfB64 = readFileSync(pdfPath).toString("base64");
  await client.callTool({ name: "upload_chunk", arguments: { upload_id: "pdf-upload-01", index: 0, total: 1, data: pdfB64 } });
  const pr = await client.callTool({ name: "redact_content", arguments: { upload_id: "pdf-upload-01", file_name: "brief.pdf" } });
  modelVisible.push(...pr.content);
  const doc = pr.structuredContent?.pdf;
  check("pdf: redacted in place and verified", doc?.verified === true && /^doc_[0-9a-f]{12}$/.test(doc.doc_id), pr.structuredContent?.pdf ?? pr.content);
  const prev = await client.callTool({ name: "preview_page", arguments: { doc_id: doc.doc_id, page: 1 } });
  check("pdf: panel preview is a PNG", Buffer.from(prev.structuredContent?.png_b64 ?? "", "base64").subarray(1, 4).toString() === "PNG");
  const read = await client.callTool({ name: "read_redacted_document", arguments: { doc_id: doc.doc_id } });
  modelVisible.push(...read.content.filter((c) => c.type === "text"));
  const imgs = read.content.filter((c) => c.type === "image");
  const txt = read.content.filter((c) => c.type === "text").map((c) => c.text).join("\n");
  check("Claude reads the PDF: page images + redacted text", imgs.length === doc.pages && imgs[0].mimeType === "image/png" && txt.includes("<PERSON_1>"), { imgs: imgs.length, txt: txt.slice(0, 200) });
  check("Claude reads the PDF: layout words kept", txt.includes("ceramic studio") && txt.includes("70 000 000 LBP"), txt.slice(0, 300));
  const bogus = await client.callTool({ name: "read_redacted_document", arguments: { doc_id: "doc_000000000000" } });
  check("unknown document id: clear error", bogus.isError === true && /no longer available/.test(bogus.content[0].text), bogus.content);

  // reset
  const rs = await client.callTool({ name: "reset_codebook", arguments: {} });
  modelVisible.push(...rs.content);
  const after = await client.callTool({ name: "restore_content", arguments: { text: "<PERSON_1>" } });
  check("reset: codebook cleared", JSON.stringify(after.structuredContent?.unresolved) === '["<PERSON_1>"]', after.structuredContent);

  // leak check
  const visibleText = JSON.stringify(modelVisible);
  const leaked = SECRETS.filter((s) => visibleText.includes(s));
  check("privacy: no real value in any model-readable content", leaked.length === 0, leaked);

  await client.close();

  // missing Python: server must stay up and explain
  const broken = await connect({ REDACTOR_PYTHON: "/nonexistent/python" });
  const r = await broken.callTool({ name: "redactor_status", arguments: {} });
  check("missing Python: clear error, server stays up", r.isError === true && /setup/i.test(r.content[0].text), r.content);
  const again = await broken.listTools();
  check("missing Python: still answers", again.tools.length === 8);
  await broken.close();

  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
