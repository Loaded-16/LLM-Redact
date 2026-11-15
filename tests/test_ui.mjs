// Drives the real panel in headless Chromium, inside a test host built on the
// SDK's AppBridge, wired to the real MCP server. Clicks through the whole flow
// and checks that only redacted text ever reaches the "chat".
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { build } from "esbuild";
import { chromium } from "playwright-core";
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readdirSync, writeFileSync } from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const SHOTS = process.env.SHOT_DIR || path.join(os.tmpdir(), "redactor-shots");
mkdirSync(SHOTS, { recursive: true });

let pass = 0;
let fail = 0;
const check = (name, cond, detail = "") => {
  cond ? pass++ : fail++;
  console.log(`${cond ? "PASS" : "FAIL"} ${name}${cond ? "" : `  -> ${typeof detail === "string" ? detail : JSON.stringify(detail).slice(0, 500)}`}`);
};

function findChrome() {
  if (process.env.CHROME_PATH) return process.env.CHROME_PATH;
  const base = process.env.PLAYWRIGHT_BROWSERS_PATH || "/opt/pw-browsers";
  for (const d of readdirSync(base).filter((d) => /^chromium-\d+$/.test(d))) {
    for (const sub of ["chrome-linux64/chrome", "chrome-linux/chrome"]) {
      const p = path.join(base, d, sub);
      if (existsSync(p)) return p;
    }
  }
  return "/usr/bin/google-chrome";
}

function makeDocx(file) {
  execFileSync(path.join(ROOT, ".venv", "bin", "python"), ["-c", `
import docx, io, os, zipfile, sys
d = docx.Document()
d.add_paragraph("Casting call notes. Lead: Maya Fares, reach her agent at maya@talents.me or +961 3 456 789.")
d.add_paragraph("Backup: Omar Nassar. Fee 15 000 000 LBP.")
d.save(sys.argv[1])
z = zipfile.ZipFile(sys.argv[1], "a", zipfile.ZIP_STORED); z.writestr("word/media/pad.bin", os.urandom(6 * 1024 * 1024)); z.close()
`, file]);
}

const SECRETS = [
  "Rami Haddad", "rami.haddad@acme.com", "03 123 456", "Sara Khoury", "Najjar",
  "Maya Fares", "maya@talents.me", "456 789", "Omar Nassar",
  "Maya Joseph", "Karim Aoun", "96171234567", "Lea Antoine", "0012345678901", "Elie Saab", "Nadine Abou Saab", "Beirut",
];

async function main() {
  // Real MCP server
  const transport = new StdioClientTransport({ command: process.execPath, args: [path.join(ROOT, "dist", "server.js")], stderr: "ignore" });
  const client = new Client({ name: "ui-test-host", version: "1.0.0" });
  await client.connect(transport);
  const { tools } = await client.listTools();
  const appCallable = new Set(tools.filter((t) => (t._meta?.ui?.visibility ?? ["model", "app"]).includes("app")).map((t) => t.name));
  const panelHtml = (await client.readResource({ uri: "ui://presidio-redactor/panel.html" })).contents[0].text;
  const modelReadable = []; // tool `content` blocks + chat messages

  // Host page served from localhost (a secure context, like the desktop app)
  const hostJs = (await build({
    entryPoints: [path.join(ROOT, "tests", "ui", "host.js")], bundle: true, format: "esm", write: false, platform: "browser",
  })).outputFiles[0].text;
  const srv = http.createServer((_q, r) => {
    r.setHeader("content-type", "text/html");
    r.end(`<!doctype html><html><body style="margin:0"><script type="module">${hostJs}</script></body></html>`);
  });
  await new Promise((r) => srv.listen(0, "127.0.0.1", r));
  const url = `http://127.0.0.1:${srv.address().port}/`;

  const browser = await chromium.launch({ executablePath: findChrome() });
  const errors = [];

  async function openPanel(theme) {
    const page = await browser.newPage({ viewport: { width: 660, height: 1300 } });
    page.on("pageerror", (e) => errors.push(`${theme}: ${e.message}`));
    page.on("console", (m) => m.type() === "error" && errors.push(`${theme}: ${m.text()}`));
    await page.exposeFunction("hostCallTool", async (json) => {
      const params = JSON.parse(json);
      if (!appCallable.has(params.name)) return JSON.stringify({ isError: true, content: [{ type: "text", text: "not app-callable" }] });
      const res = await client.callTool(params, undefined, { timeout: 180000 });
      modelReadable.push(...(res.content || []));
      return JSON.stringify(res);
    });
    await page.goto(url);
    await page.waitForFunction(() => typeof window.startHost === "function");
    await page.evaluate(([html, t]) => window.startHost(html, t), [panelHtml, theme]);
    const f = page.frameLocator("#panel");
    await f.locator("#status.ok").waitFor({ timeout: 90000 });
    return { page, f };
  }

  // ---------- light theme: full flow with pasted text ----------
  const { page, f } = await openPanel("light");
  check("panel connects and shows Presidio ready", (await f.locator("#status").textContent()).includes("ready"));
  check("redact disabled while empty", await f.locator("#redact").isDisabled());

  await f.locator("#input").fill(
    "Rami Haddad (rami.haddad@acme.com, 03 123 456) wants Sara Khoury on the Najjar shoot in Beirut. Budget 70 000 000 LBP.",
  );
  await f.locator("summary", { hasText: "What to hide" }).click();
  await f.locator("#terms").fill("Najjar");
  await f.locator("#redact").click();
  await f.locator("#reviewStep:not(.hidden)").waitFor();
  const masked = await f.locator("#masked").inputValue();
  check("redacts pasted text", masked.startsWith("<PERSON_1> (<EMAIL_ADDRESS_1>, <LB_PHONE_1>) wants <PERSON_2> on the <CUSTOM_1> shoot in Beirut."), masked);
  check("keeps the LBP amount", masked.includes("70 000 000 LBP"), masked);
  const chips = await f.locator(".chip").allTextContents();
  check("shows what was hidden", chips.some((c) => c.startsWith("People's names: 2")) && chips.some((c) => c.startsWith("Your words: 1")), chips);
  await page.screenshot({ path: path.join(SHOTS, "panel-light.png"), fullPage: true });

  // Review: untick a false hit, add a miss, apply
  const rowTexts = await f.locator("#reviewList .val").allTextContents();
  check("review list shows each hidden value", rowTexts.includes("Rami Haddad") && rowTexts.includes("Najjar"), rowTexts);
  await f.locator("#reviewSummary").click(); // the review list starts collapsed
  await f.locator("#reviewList label", { hasText: "Najjar" }).locator("input").uncheck();
  await f.locator("#addTerm").fill("Beirut");
  await f.locator("#addBtn").click();
  check("Apply is enabled after changes", !(await f.locator("#apply").isDisabled()));
  await page.screenshot({ path: path.join(SHOTS, "panel-review.png"), fullPage: true });
  await f.locator("#apply").click();
  await f.locator("#redactMsg.ok").waitFor();
  const reviewed = await f.locator("#masked").inputValue();
  check("review applied: unticked word visible, added word hidden",
    reviewed.includes("Najjar") && !reviewed.includes("Beirut") && !reviewed.includes("Rami"), reviewed);
  // put it back the way the rest of the test expects
  await f.locator("#reviewList label", { hasText: "Najjar" }).locator("input").check();
  await f.locator("#apply").click();
  await f.locator("#redactMsg.ok").waitFor();
  const masked2 = await f.locator("#masked").inputValue();
  check("re-ticking hides it again", !masked2.includes("Najjar"), masked2);

  // Send to chat
  await f.locator("#send").click();
  await f.locator("#sendMsg.ok").waitFor();
  const sent = await page.evaluate(() => window.hostLog.messages);
  check("Send posts one user message to the chat", sent.length === 1 && sent[0].role === "user", sent);
  const sentText = sent[0]?.content?.[0]?.text ?? "";
  modelReadable.push(...(sent[0]?.content ?? []));
  const maskedNow = await f.locator("#masked").inputValue();
  check("message has the placeholder instruction + masked text", sentText.includes("keep them exactly as written") && sentText.endsWith(maskedNow.trim()), sentText);

  // Restore Claude's reply locally
  const najjarTag = (await f.locator("#reviewList label", { hasText: "Najjar" }).locator(".meta").textContent()).match(/<CUSTOM_\d+>/)[0];
  await f.locator("#reply").fill(`Great, I'll draft a note from <PERSON_1> to <PERSON_2> about ${najjarTag}. <PERSON_7>?`);
  await f.locator("#restore").click();
  await f.locator("#restoreWrap:not(.hidden)").waitFor();
  const restored = await f.locator("#restored").inputValue();
  check("restores real names in the panel", restored.startsWith("Great, I'll draft a note from Rami Haddad to Sara Khoury about Najjar."), restored);
  check("flags placeholders it doesn't know", (await f.locator("#restoreMsg").textContent()).includes("<PERSON_7>"));
  const afterRestore = await page.evaluate(() => window.hostLog.messages.length);
  check("restoring sends nothing to the chat", afterRestore === 1, afterRestore);

  // ---------- file upload (chunked) ----------
  const docxPath = path.join(SHOTS, "casting.docx");
  makeDocx(docxPath);
  await f.locator("#picker").setInputFiles(docxPath);
  await f.locator("#fileRow.show").waitFor();
  await f.locator("#redact").click();
  await f.locator("#redactMsg.ok").waitFor({ timeout: 120000 });
  const maskedFile = await f.locator("#masked").inputValue();
  check("redacts a 6 MB Word file via chunks",
    maskedFile.includes("Lead: <PERSON_3>") && !maskedFile.includes("Maya") && !maskedFile.includes("maya@talents.me") && !maskedFile.includes("456 789"),
    maskedFile);
  check("file amount kept", maskedFile.includes("15 000 000 LBP"), maskedFile);
  const calls = await page.evaluate(() => window.hostLog.toolCalls);
  check("file went through several upload_chunk calls", calls.filter((c) => c === "upload_chunk").length >= 5, calls);

  // ---------- PDF: redacted in place, previewed, sent by reference ----------
  const pdfPath = path.join(SHOTS, "brief.pdf");
  execFileSync(path.join(ROOT, ".venv", "bin", "python"), [path.join(ROOT, "tests", "make_test_pdf.py"), pdfPath]);
  await f.locator("#clearFile").click();
  await f.locator("#picker").setInputFiles(pdfPath);
  await f.locator("#redact").click();
  await f.locator("#pdfBox:not(.hidden)").waitFor({ timeout: 120000 });
  await f.locator("#pageImg").evaluate((img) => img.complete && img.naturalWidth > 0 ? true : new Promise((r) => (img.onload = () => r(true))));
  const imgW = await f.locator("#pageImg").evaluate((img) => img.naturalWidth);
  check("PDF: redacted page preview shown", imgW > 300, imgW);
  check("PDF: text box swapped for preview", await f.locator("#textBox").isHidden());
  check("PDF: send button sends the PDF", (await f.locator("#send").textContent()) === "Send redacted PDF to chat" && !(await f.locator("#send").isDisabled()));
  check("PDF: text fallback offered", await f.locator("#sendText").isVisible());
  await page.screenshot({ path: path.join(SHOTS, "panel-pdf.png"), fullPage: true });
  const before = await page.evaluate(() => window.hostLog.messages.length);
  await f.locator("#send").click();
  await page.waitForFunction((n) => window.hostLog.messages.length > n, before);
  const pdfMsg = (await page.evaluate(() => window.hostLog.messages.at(-1)))?.content?.[0]?.text ?? "";
  modelReadable.push({ type: "text", text: pdfMsg });
  const docId = (pdfMsg.match(/doc_[0-9a-f]{12}/) || [])[0];
  check("PDF: chat message names the document and the tool", !!docId && pdfMsg.includes("read_redacted_document") && pdfMsg.includes("brief (redacted).pdf"), pdfMsg);
  // What Claude gets when it follows the message:
  const read = await client.callTool({ name: "read_redacted_document", arguments: { doc_id: docId } });
  modelReadable.push(...read.content.filter((c) => c.type === "text"));
  check("PDF: Claude can read it", read.content.some((c) => c.type === "image") && !read.isError);

  // ---------- forget all (two-step, no popups) ----------
  await f.locator("#reset").click();
  check("first click only arms the reset", (await f.locator("#reset").textContent()).includes("Click again"));
  await f.locator("#reset").click();
  await f.locator("#held", { hasText: "0 hidden values" }).waitFor();
  check("second click clears the codebook", true);
  await page.close();

  // ---------- dark theme renders ----------
  const dark = await openPanel("dark");
  const theme = await dark.f.locator("html").getAttribute("data-theme");
  const bg = await dark.f.locator("body").evaluate((el) => getComputedStyle(el).backgroundColor);
  check("dark theme applied from host", theme === "dark" && bg !== "rgb(255, 255, 255)", { theme, bg });
  await dark.f.locator("#input").fill("Ping Lina Azar at lina@x.com");
  await dark.f.locator("#redact").click();
  await dark.f.locator("#reviewStep:not(.hidden)").waitFor();
  await dark.page.screenshot({ path: path.join(SHOTS, "panel-dark.png"), fullPage: true });
  await dark.page.close();

  // ---------- privacy + errors ----------
  const visible = JSON.stringify(modelReadable);
  const leaked = SECRETS.filter((s) => visible.includes(s));
  check("privacy: nothing Claude can read contains a real value", leaked.length === 0, leaked);
  check("no JavaScript errors in the panel", errors.length === 0, errors);

  await browser.close();
  srv.close();
  await client.close();
  console.log(`\nscreenshots: ${SHOTS}`);
  console.log(`${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
