import {
  App,
  applyDocumentTheme,
  applyHostFonts,
  applyHostStyleVariables,
} from "@modelcontextprotocol/ext-apps";

const LABELS = {
  PERSON: "People's names",
  EMAIL_ADDRESS: "Emails",
  PHONE_NUMBER: "Phone numbers",
  LB_PHONE: "Lebanese numbers",
  CREDIT_CARD: "Card numbers",
  IBAN_CODE: "IBANs",
  IP_ADDRESS: "IP addresses",
  BANK_ACCOUNT: "Bank accounts",
  LONG_NUMBER: "Long numbers (IDs)",
  LOCATION: "Places",
  DATE_TIME: "Dates and times",
  NRP: "Nationalities, religions",
  URL: "Links",
  CUSTOM: "Your words",
};
const DEFAULTS = ["PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "LB_PHONE", "CREDIT_CARD", "IBAN_CODE", "BANK_ACCOUNT", "LONG_NUMBER", "IP_ADDRESS"];
const OPTIONAL = ["LOCATION", "DATE_TIME", "NRP", "URL"];
const MAX_BYTES = 10 * 1024 * 1024;
const CHUNK = 1_500_000; // base64 characters per upload_chunk call
const pdfMessage = (d) =>
  `I've redacted a PDF on my computer: "${d.name}", ${d.pages} page${d.pages === 1 ? "" : "s"}, document id ${d.doc_id}. ` +
  "Please read it with the read_redacted_document tool. Placeholders like <PERSON_1> stand for hidden details: " +
  "keep them exactly as written and don't guess what they are.";
const SEND_HEADER =
  "Here is the content, redacted on my computer. Placeholders like <PERSON_1> stand for hidden " +
  "details: keep them exactly as written and don't guess what they are.\n\n";

const $ = (id) => document.getElementById(id);
let file = null; // { name, b64 }
let lastInput = null; // what was redacted last, so the review can re-run it
const review = { keep: new Set(), add: new Set() };
let pdf = null; // { doc_id, name, pages, verified, page }
let ready = false;

// ---------- host connection ----------

const app = new App({ name: "Presidio Redactor", version: "1.0.0" }, {}, { autoResize: true });

function applyContext(ctx) {
  if (!ctx) return;
  if (ctx.theme) applyDocumentTheme(ctx.theme);
  if (ctx.styles?.variables) applyHostStyleVariables(ctx.styles.variables);
  if (ctx.styles?.css?.fonts) applyHostFonts(ctx.styles.css.fonts);
}
app.onhostcontextchanged = (ctx) => applyContext({ ...app.getHostContext(), ...ctx });
app.ontoolresult = () => {};
app.onteardown = async () => ({});

async function call(name, args = {}) {
  const res = await app.callServerTool({ name, arguments: args });
  const data = res.structuredContent ?? {};
  if (res.isError) {
    const text = data.error || res.content?.find((c) => c.type === "text")?.text || "Something went wrong.";
    throw new Error(text.replace(/^Error:\s*/, ""));
  }
  return data;
}

// ---------- UI helpers ----------

function setMsg(id, text, kind = "") {
  const el = $(id);
  el.textContent = text;
  el.className = "msg" + (kind ? " " + kind : "");
}

function setStatus(text, kind = "") {
  const el = $("status");
  el.textContent = text;
  el.className = "pill" + (kind ? " " + kind : "");
}

function setHeld(n) {
  $("held").textContent = `Codebook: ${n} hidden value${n === 1 ? "" : "s"} in memory`;
  // Restore and the codebook only matter once something has been hidden.
  if (n > 0) {
    $("restoreStep").classList.remove("hidden");
    $("foot").classList.remove("hidden");
  }
}

function updateRedactButton() {
  $("redact").disabled = !ready || (!file && !$("input").value.trim());
}

async function copy(text, msgId) {
  try {
    await navigator.clipboard.writeText(text);
    setMsg(msgId, "Copied.", "ok");
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    setMsg(msgId, ok ? "Copied." : "Copy is blocked here. Select the text and copy it manually.", ok ? "ok" : "err");
  }
}

function buildOptions() {
  const box = $("opts");
  for (const key of [...DEFAULTS, ...OPTIONAL]) {
    const label = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.value = key;
    cb.checked = DEFAULTS.includes(key);
    label.append(cb, " " + LABELS[key]);
    box.appendChild(label);
  }
}

function selectedEntities() {
  return [...$("opts").querySelectorAll("input:checked")].map((c) => c.value);
}

function customTerms() {
  return $("terms").value.split(",").map((t) => t.trim()).filter(Boolean);
}

function toBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  const step = 0x8000;
  for (let i = 0; i < bytes.length; i += step) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + step));
  }
  return btoa(binary);
}

async function takeFile(f) {
  if (!f) return;
  if (f.size > MAX_BYTES) {
    setMsg("redactMsg", "That file is larger than 10 MB.", "err");
    return;
  }
  file = { name: f.name, b64: toBase64(await f.arrayBuffer()) };
  $("fileName").textContent = `${f.name} · ${(f.size / 1024).toFixed(0)} KB`;
  $("fileRow").classList.add("show");
  setMsg("redactMsg", "");
  updateRedactButton();
}

function clearFile() {
  file = null;
  $("picker").value = "";
  $("fileRow").classList.remove("show");
  updateRedactButton();
}

// ---------- actions ----------

async function redact() {
  $("redact").disabled = true;
  setMsg("redactMsg", "Redacting on your computer…");
  try {
    const args = {
      entities: selectedEntities(),
      // Unticking in the review wins over the "always hide" box.
      custom_terms: [...new Set([...customTerms(), ...review.add])]
        .filter((t) => ![...review.keep].some((k) => k.toLowerCase() === t.toLowerCase())),
      keep: [...review.keep],
      remove_images: $("removeImages").checked,
      flatten: $("flatten").checked,
    };
    if (file) {
      // Send the file in chunks: one stdio message is capped at 10 MB.
      const uploadId = newId();
      const total = Math.max(1, Math.ceil(file.b64.length / CHUNK));
      for (let i = 0; i < total; i++) {
        if (total > 1) setMsg("redactMsg", `Reading file… ${Math.round((i / total) * 100)}%`);
        await call("upload_chunk", {
          upload_id: uploadId, index: i, total, data: file.b64.slice(i * CHUNK, (i + 1) * CHUNK),
        });
      }
      setMsg("redactMsg", "Redacting on your computer… long documents can take up to a minute.");
      Object.assign(args, { file_name: file.name, upload_id: uploadId });
    } else {
      args.text = $("input").value;
    }
    const r = await call("redact_content", args);
    lastInput = file ? { file } : { text: args.text };

    $("masked").value = r.masked;
    renderReview(r.detections || []);
    const chips = $("chips");
    chips.innerHTML = "";
    const entries = Object.entries(r.counts || {});
    if (!entries.length) {
      const c = document.createElement("span");
      c.className = "chip";
      c.textContent = "Nothing detected. Check carefully before sending.";
      chips.appendChild(c);
    }
    for (const [type, n] of entries) {
      const c = document.createElement("span");
      c.className = "chip";
      c.textContent = `${LABELS[type] || type}: ${n}`;
      chips.appendChild(c);
    }
    showPdf(r.pdf || null);
    $("reviewStep").classList.remove("hidden");
    setMsg("redactMsg", `Done. ${r.total} item${r.total === 1 ? "" : "s"} hidden from ${r.source}.`, "ok");
    setMsg("sendMsg", "");
    refreshHeld();
    $("masked").focus();
  } catch (e) {
    setMsg("redactMsg", e.message, "err");
  } finally {
    updateRedactButton();
  }
}

// ---------- review: untick false hits, add misses, re-run ----------

function renderReview(rows) {
  const list = $("reviewList");
  list.innerHTML = "";
  const all = [...rows];
  for (const v of review.add) {
    if (!rows.some((r) => r.value.toLowerCase() === v.toLowerCase())) all.push({ value: v, type: "CUSTOM", tag: "", count: 0, pending: true });
  }
  for (const v of review.keep) {
    if (!rows.some((r) => r.value.toLowerCase() === v.toLowerCase())) all.push({ value: v, type: "", tag: "", count: 0, kept: true });
  }
  $("reviewSummary").textContent = `Review what's hidden (${rows.length})`;
  for (const row of all) {
    const label = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = !row.kept;
    const val = document.createElement("span");
    val.className = "val" + (row.kept ? " off" : "") + (row.pending ? " added" : "");
    val.textContent = row.value;
    val.title = row.value;
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = row.kept ? "kept visible" : row.pending ? "will be added" :
      `${LABELS[row.type] || row.type} · ${row.tag}${row.count > 1 ? ` · ×${row.count}` : ""}`;
    cb.addEventListener("change", () => {
      const key = row.value;
      if (row.pending) {
        if (!cb.checked) review.add.delete(key);
      } else if (cb.checked) {
        review.keep.delete(key);
      } else {
        review.keep.add(key);
      }
      val.classList.toggle("off", !cb.checked);
      $("apply").disabled = false;
    });
    label.append(cb, val, meta);
    list.appendChild(label);
  }
  if (!all.length) {
    const empty = document.createElement("p");
    empty.className = "hint";
    empty.style.padding = "8px";
    empty.textContent = "Nothing detected. Add anything that should be hidden below.";
    list.appendChild(empty);
  }
  $("apply").disabled = true;
}

function addMissed() {
  const v = $("addTerm").value.trim();
  if (!v) return;
  review.add.add(v);
  review.keep.delete(v);
  $("addTerm").value = "";
  $("apply").disabled = false;
  const label = document.createElement("label");
  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.checked = true;
  cb.addEventListener("change", () => { if (!cb.checked) review.add.delete(v); else review.add.add(v); });
  const val = document.createElement("span");
  val.className = "val added";
  val.textContent = v;
  const meta = document.createElement("span");
  meta.className = "meta";
  meta.textContent = "will be added";
  label.append(cb, val, meta);
  $("reviewList").prepend(label);
}

async function applyReview() {
  if (!lastInput) return;
  if (!lastInput.file) $("input").value = lastInput.text;
  else file = lastInput.file;
  await redact();
}

// ---------- PDF preview ----------

function showPdf(info) {
  pdf = info ? { ...info, page: 1 } : null;
  $("pdfBox").classList.toggle("hidden", !pdf);
  $("textBox").classList.toggle("hidden", !!pdf);
  $("sendText").classList.toggle("hidden", !pdf);
  if (!pdf) {
    $("send").textContent = "Send to chat";
    $("send").disabled = false;
    return;
  }
  const warn = $("pdfWarn");
  if (!pdf.verified) {
    warn.textContent = `${pdf.leak_count || "Some"} detected value(s) could not be removed from the PDF layout. ` +
      "Sending the PDF is blocked. Send as text instead.";
    warn.classList.remove("hidden");
  } else {
    warn.classList.add("hidden");
  }
  $("send").disabled = !pdf.verified;
  $("send").textContent = "Send redacted PDF to chat";
  loadPage(1);
}

async function loadPage(n) {
  if (!pdf) return;
  pdf.page = Math.min(Math.max(1, n), pdf.pages);
  $("pageLabel").textContent = `Page ${pdf.page} of ${pdf.pages}`;
  $("prevPage").disabled = pdf.page <= 1;
  $("nextPage").disabled = pdf.page >= pdf.pages;
  try {
    const r = await call("preview_page", { doc_id: pdf.doc_id, page: pdf.page });
    $("pageImg").src = `data:image/png;base64,${r.png_b64}`;
  } catch (e) {
    $("pdfWarn").textContent = e.message;
    $("pdfWarn").classList.remove("hidden");
  }
}

async function send(asText = false) {
  const text = pdf && !asText ? pdfMessage(pdf) : $("masked").value.trim();
  if (!text) return;
  const caps = app.getHostCapabilities?.() || {};
  if (!caps.message) {
    setMsg("sendMsg", "This version of Claude can't receive messages from the panel. Use Copy and paste it into the chat.", "err");
    return;
  }
  $("send").disabled = true;
  try {
    const body = pdf && !asText ? text : SEND_HEADER + text;
    const res = await app.sendMessage({ role: "user", content: [{ type: "text", text: body }] });
    if (res?.isError) throw new Error("Claude declined the message.");
    setMsg("sendMsg", "Sent to the chat.", "ok");
  } catch (e) {
    setMsg("sendMsg", `Couldn't send: ${e.message}. Use Copy instead.`, "err");
  } finally {
    $("send").disabled = !!pdf && !pdf.verified;
  }
}

async function restore() {
  const text = $("reply").value;
  if (!text.trim()) return;
  try {
    const r = await call("restore_content", { text });
    $("restored").value = r.restored;
    $("restoreWrap").classList.remove("hidden");
    if (r.unresolved?.length) {
      setMsg("restoreMsg", `Not found in memory: ${r.unresolved.join(", ")}. They may come from before a reset or a restart.`, "err");
    } else {
      setMsg("restoreMsg", "Restored on your computer. Nothing was sent.", "ok");
    }
  } catch (e) {
    setMsg("restoreMsg", e.message, "err");
  }
}

async function refreshHeld() {
  try {
    const s = await call("redactor_status");
    setHeld(s.values_held ?? 0);
  } catch {
    /* status is best effort */
  }
}

// Two-step confirm: popups like confirm() are usually blocked inside chat panels.
let resetArmed = null;
async function resetCodebook() {
  const btn = $("reset");
  if (!resetArmed) {
    btn.textContent = "Click again to forget everything";
    resetArmed = setTimeout(() => {
      resetArmed = null;
      btn.textContent = "Forget all";
    }, 4000);
    return;
  }
  clearTimeout(resetArmed);
  resetArmed = null;
  btn.textContent = "Forget all";
  try {
    await call("reset_codebook");
    review.keep.clear();
    review.add.clear();
    setHeld(0);
    setMsg("restoreMsg", "Forgotten. Placeholders already in the chat can no longer be restored.", "ok");
  } catch (e) {
    setMsg("restoreMsg", e.message, "err");
  }
}

function newId() {
  if (globalThis.crypto?.randomUUID) return crypto.randomUUID();
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// ---------- wiring ----------

function wire() {
  buildOptions();
  $("input").addEventListener("input", updateRedactButton);
  $("redact").addEventListener("click", redact);
  $("send").addEventListener("click", () => send(false));
  $("sendText").addEventListener("click", () => send(true));
  $("apply").addEventListener("click", applyReview);
  $("addBtn").addEventListener("click", addMissed);
  $("addTerm").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); addMissed(); } });
  $("prevPage").addEventListener("click", () => loadPage(pdf.page - 1));
  $("nextPage").addEventListener("click", () => loadPage(pdf.page + 1));
  $("copyMasked").addEventListener("click", () => copy($("masked").value, "sendMsg"));
  $("restore").addEventListener("click", restore);
  $("copyRestored").addEventListener("click", () => copy($("restored").value, "restoreMsg"));
  $("reset").addEventListener("click", resetCodebook);
  $("clearFile").addEventListener("click", clearFile);

  const drop = $("drop");
  const picker = $("picker");
  drop.addEventListener("click", () => picker.click());
  drop.addEventListener("keydown", (e) => (e.key === "Enter" || e.key === " ") && picker.click());
  picker.addEventListener("change", () => takeFile(picker.files[0]));
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    drop.classList.remove("over");
    takeFile(e.dataTransfer.files[0]);
  });
}

async function init() {
  wire();
  try {
    await app.connect();
    applyContext(app.getHostContext());
  } catch (e) {
    setStatus("Not connected", "err");
    setMsg("redactMsg", `Couldn't connect to Claude: ${e.message}`, "err");
    return;
  }
  setStatus("Loading language model…");
  try {
    const s = await call("redactor_status");
    ready = true;
    const f = s.features || {};
    const extras = [f.gliner === "on" && "GLiNER", f.arabic === "on" && "Arabic", f.ocr && "OCR"].filter(Boolean);
    setStatus(`Presidio ready · local${extras.length ? " · " + extras.join(" · ") : ""}`, "ok");
    setHeld(s.values_held ?? 0);
  } catch (e) {
    setStatus("Presidio not running", "err");
    setMsg("redactMsg", e.message, "err");
  }
  updateRedactButton();
}

init();
