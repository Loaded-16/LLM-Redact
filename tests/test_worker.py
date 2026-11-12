"""End-to-end tests for the Presidio worker (run with the project's venv python).

Starts the worker exactly like the MCP server does, with every outbound
network connection blocked and recorded, then exercises each operation.
"""

import base64
import io
import json
import re
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["REDACTOR_STORE"] = tempfile.mkdtemp(prefix="redactor-store-")
WORKER = os.path.join(ROOT, "worker", "worker.py")

# Wrapper that blocks and logs any network connection attempt, then runs the worker.
NET_GUARD = r"""
import socket, sys, runpy
_attempts = []
_orig = socket.socket.connect
def _blocked(self, addr):
    sys.stderr.write("NETWORK_ATTEMPT %r\n" % (addr,))
    raise OSError("network blocked by test")
socket.socket.connect = _blocked
socket.create_connection = lambda *a, **k: (_ for _ in ()).throw(OSError("network blocked by test"))
sys.argv = [sys.argv[1]]
runpy.run_path(sys.argv[0], run_name="__main__")
"""

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))


class Worker:
    def __init__(self):
        self.err = tempfile.TemporaryFile(mode="w+")
        self.p = subprocess.Popen(
            [sys.executable, "-c", NET_GUARD, WORKER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.err,
            text=True, encoding="utf-8",
        )
        first = json.loads(self.p.stdout.readline())
        assert first == {"event": "ready"}, first
        self.n = 0

    def call(self, op, **kw):
        self.n += 1
        self.p.stdin.write(json.dumps({"id": self.n, "op": op, **kw}) + "\n")
        self.p.stdin.flush()
        resp = json.loads(self.p.stdout.readline())
        assert resp["id"] == self.n
        return resp

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=10)
        self.err.seek(0)
        return self.err.read()


def b64(data):
    return base64.b64encode(data).decode()


def make_docx():
    import docx
    d = docx.Document()
    d.add_paragraph("Client contact: Nadine Saab, nadine.saab@brandco.com")
    t = d.add_table(rows=1, cols=2)
    t.rows[0].cells[0].text = "Producer"
    t.rows[0].cells[1].text = "Karim Aoun +961 3 987 654"
    buf = io.BytesIO(); d.save(buf); return buf.getvalue()


def make_pdf():
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 750, "Contract signed by Georges Haddad on behalf of the client.")
    c.drawString(72, 730, "Payment to IBAN DE89370400440532013000.")
    c.showPage(); c.save()
    return buf.getvalue()


def make_blank_pdf():
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf); c.showPage(); c.save()
    return buf.getvalue()


def make_pptx():
    from pptx import Presentation
    from pptx.util import Inches
    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[5])
    s.shapes.title.text = "Talent shortlist"
    tb = s.shapes.add_textbox(Inches(1), Inches(2), Inches(6), Inches(1))
    tb.text_frame.text = "Lead: Maya Fares, agent email maya@talents.me"
    s.notes_slide.notes_text_frame.text = "Call Maya's agent on 70 111 222 before Friday"
    buf = io.BytesIO(); prs.save(buf); return buf.getvalue()


def make_xlsx():
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active; ws.title = "Crew"
    ws.append(["Name", "Role", "Email"])
    ws.append(["Elie Mansour", "DOP", "elie.mansour@crew.co"])
    ws.append(["Budget", "Fee", "45 000 000 LBP"])
    buf = io.BytesIO(); wb.save(buf); return buf.getvalue()


def main():
    w = Worker()

    r = w.call("ping")
    check("ping", r["ok"] and r["result"]["ready"])

    # --- pasted text ---
    text = ("Hi team, Rami Haddad (rami.haddad@acme.com) approved it. Call him on 03 123 456 "
            "or +961 71 234 567. Rami Haddad will loop in Sara Khoury. The budget is "
            "70 000 000 LBP plus $12 500. Card 4111 1111 1111 1111.")
    r = w.call("redact", text=text)
    m = r["result"]["masked"]
    check("text: names masked", "Rami Haddad" not in m and "Sara Khoury" not in m, m)
    check("text: email masked", "rami.haddad@acme.com" not in m, m)
    check("text: LB phones masked", "03 123 456" not in m and "71 234 567" not in m, m)
    check("text: card masked", "4111" not in m, m)
    check("text: LBP amount kept", "70 000 000 LBP" in m, m)
    check("text: USD amount kept", "$12 500" in m, m)
    check("text: same person same tag", m.count("<PERSON_1>") == 2, m)
    check("text: reading order numbering", m.index("<PERSON_1>") < m.index("<PERSON_2>"), m)

    # --- consistency across calls ---
    r2 = w.call("redact", text="Sara Khoury and Rami Haddad will present.")
    m2 = r2["result"]["masked"]
    check("codebook: tags reused across messages",
          "<PERSON_2> and <PERSON_1> will present." == m2, m2)
    check("codebook: no new values for known people", r2["result"]["new_values"] == 0, r2)

    # --- custom terms ---
    r = w.call("redact", text="Project Falcon for najjar launches in Riyadh.",
               custom_terms=["Najjar", "Project Falcon"])
    m = r["result"]["masked"]
    check("custom terms: masked case-insensitively", "Falcon" not in m and "najjar" not in m.lower(), m)
    check("custom terms: location kept by default", "Riyadh" in m, m)

    # --- optional entities ---
    r = w.call("redact", text="The shoot is in Beirut with Lina Azar.",
               entities=["PERSON", "LOCATION"])
    m = r["result"]["masked"]
    check("optional LOCATION masks when enabled", "Beirut" not in m and "Lina Azar" not in m, m)

    # --- restore ---
    r = w.call("restore", text="Dear <PERSON_1>, please brief <PERSON_2>. cc <PERSON_99>")
    res = r["result"]
    check("restore: values swapped back",
          res["restored"].startswith("Dear Rami Haddad, please brief Sara Khoury."), res)
    check("restore: unknown tag reported", res["unresolved"] == ["<PERSON_99>"], res)

    # --- files ---
    for label, name, data, secrets in [
        ("docx", "brief.docx", make_docx(), ["Nadine Saab", "nadine.saab@brandco.com", "987 654"]),
        ("pdf", "contract.pdf", make_pdf(), ["Georges Haddad", "DE89370400440532013000"]),
        ("pptx", "deck.pptx", make_pptx(), ["Maya Fares", "maya@talents.me", "70 111 222"]),
        ("xlsx", "crew.xlsx", make_xlsx(), ["Elie Mansour", "elie.mansour@crew.co"]),
        ("txt", "notes.txt", "Ping Joe Bloggs at joe@x.com".encode(), ["Joe Bloggs", "joe@x.com"]),
    ]:
        r = w.call("redact", file_name=name, file_b64=b64(data))
        if not r["ok"]:
            check(f"file {label}: redacted", False, r)
            continue
        m = r["result"]["masked"]
        leaked = [s for s in secrets if s in m]
        check(f"file {label}: redacted", not leaked, f"leaked {leaked} in: {m}")

    r = w.call("redact", file_name="crew.xlsx", file_b64=b64(make_xlsx()))
    check("file xlsx: amount kept", "45 000 000 LBP" in r["result"]["masked"], r["result"]["masked"])

    # --- errors ---
    r = w.call("redact", file_name="scan.pdf", file_b64=b64(make_blank_pdf()))
    check("scanned PDF: clear error", not r["ok"] and "scan" in r["error"].lower(), r)
    r = w.call("redact", file_name="photo.heic", file_b64=b64(b"xx"))
    check("unsupported type: clear error", not r["ok"] and "Unsupported" in r["error"], r)
    r = w.call("redact", text="   ")
    check("empty text: clear error", not r["ok"], r)
    r = w.call("nope")
    check("unknown op: error, worker survives", not r["ok"] and w.call("ping")["ok"], r)

    # --- Lebanese / Arab names ---
    w.call("reset")
    for t, hidden in [
        ("Jad will present, then Elie and Nour join.", ["Jad", "Elie", "Nour"]),
        ("Hi Ghinwa, can Toni and Joelle call Chadi?", ["Ghinwa", "Toni", "Joelle", "Chadi"]),
        ("Talent: Hiba Tawaji / Wael Kfoury", ["Hiba", "Tawaji", "Wael", "Kfoury"]),
        ("Meeting with Ziad Abou Jaoude and Rita Bou Khalil.", ["Ziad", "Jaoude", "Rita", "Khalil"]),
        ("ATTENDEES: GEORGES KHOURY, NADINE NASSIF", ["GEORGES", "KHOURY", "NADINE", "NASSIF"]),
        ("اتصل بجاد ورامي غداً بخصوص الحملة", ["جاد", "رامي"]),
    ]:
        m = w.call("redact", text=t)["result"]["masked"]
        check(f"names: {t[:32]}", not [h for h in hidden if h in m], m)
    w.call("reset")
    m = w.call("redact", text="Rami Haddad leads. Rami will send it, cc Haddad.")["result"]["masked"]
    check("names: first/last name alone share the full name's tag",
          m == "<PERSON_1> leads. <PERSON_1> will send it, cc <PERSON_1>.", m)
    m = w.call("redact", text="Ask Rami for the files.")["result"]["masked"]
    check("names: remembered across messages", m == "Ask <PERSON_1> for the files.", m)
    r = w.call("restore", text="Thanks <PERSON_1>")["result"]
    check("names: restore uses the full name", r["restored"] == "Thanks Rami Haddad", r)
    for t in ["Lead the team in May with joy and grace. Said the client: we hope to launch.",
              "The Najjar Project Brief is due Monday."]:
        m = w.call("redact", text=t)["result"]["masked"]
        check(f"names: no false hits in '{t[:28]}'", m == t, m)
    m = w.call("redact", text="Sami Project lead. Email sami@x.com")["result"]["masked"]
    check("names: heading words and labels are not names", m.startswith("<PERSON_") and " Project lead. Email <EMAIL_ADDRESS_" in m, m)

    # --- lowercase names, numbers without +, accounts (all made-up values) ---
    w.call("reset")
    m = w.call("redact", text="contact maya joseph haddad for the shoot")["result"]["masked"]
    check("lowercase three-part name", m == "contact <PERSON_1> for the shoot", m)
    m = w.call("redact", text="Later, haddad confirmed it.")["result"]["masked"]
    check("lowercase surname alone after full name", m == "Later, <PERSON_1> confirmed it.", m)
    for t, kind in [
        ("whatsapp 96171234567", "LB_PHONE"),
        ("or 9613123456 tonight", "LB_PHONE"),
        ("call 966501234567", "PHONE_NUMBER"),
        ("account number 0012345678901", "BANK_ACCOUNT"),
        ("Acct: 001-234567-89 at the bank", "BANK_ACCOUNT"),
        ("Please transfer to 12345678 (bank)", "BANK_ACCOUNT"),
        ("PO 450012345, thanks", "LONG_NUMBER"),
    ]:
        m = w.call("redact", text=t)["result"]["masked"]
        check(f"numbers: '{t}' -> {kind}", f"<{kind}_" in m and not any(ch.isdigit() for ch in m.split("<")[0][-3:]), m)
    m = w.call("redact", text="Please transfer to 12345678 (bank)")["result"]["masked"]
    check("numbers: surrounding words are kept", m.startswith("Please transfer to <BANK_ACCOUNT_"), m)
    for t in ["Budget 150000000 LBP, fee 2500 USD",
              "Invoice dated 12/11/2026, order 123456, year 2026",
              "The shoot is on 2026-11-20 at 10:30"]:
        m = w.call("redact", text=t)["result"]["masked"]
        check(f"numbers: left alone '{t[:30]}'", m == t, m)

    # --- IBANs, accounts and amounts (bank documents) ---
    w.call("reset")
    for t in ["IBAN: LB62 0999 0000 0001 0019 0122 9114 thanks",
              "iban:lb62099900000001001901229114",
              "IBAN/LB62\u00a00999\u00a00000\u00a00001\u00a00019\u00a00122\u00a09114",
              "pay into LB62-0999-0000-0001-0019-0122-9114 today",
              "account is IBAN LB62 0999 0000 0001\n0019 0122 9114 held at the branch",
              "IBAN: LB12 3456 7890 1234 5678 9012 3456 (bad checksum)",
              "IBAN GB82 WEST 1234 5698 7654 32.", "use SA03 8000 0000 6080 1016 7519", "AE07 0331 2345 6789 0123 456"]:
        m = w.call("redact", text=t)["result"]["masked"]
        check(f"iban: {t[:34]!r}", "<IBAN_CODE_" in m and not re.search(r"\d{4}", m), m)
    m = w.call("redact", text="LB62 0999 0000 0001 0019 0122 9114 and lb62099900000001001901229114")["result"]["masked"]
    check("iban: two spellings share one placeholder", len(set(re.findall(r"<IBAN_CODE_\d+>", m))) == 1, m)
    for t, kind in [("Account No: 1234567890 USD", "BANK_ACCOUNT"), ("A/C 001-234567-89 - LBP", "BANK_ACCOUNT"),
                    ("Account number\n0012345678901", "BANK_ACCOUNT"), ("رقم الحساب: 0012345678901", "BANK_ACCOUNT"),
                    ("Customer ID\n450012345", "LONG_NUMBER"), ("Mobile\n70 123 456", "PHONE_NUMBER")]:
        m = w.call("redact", text=t)["result"]["masked"]
        check(f"labelled number hidden: {t[:26]!r}", f"<{kind}_" in m and not re.search(r"\d{5}", m), m)
    statement = ("Date\nDescription\nDebit\nBalance\n01/11/2026\nSalary deposit\n3,500.00\n12,250.00\n"
                 "05/11/2026\nATM withdrawal\n500000\n11750000\n07/11/2026\nCard payment ref 123456\n45 000 000\n150000000\n"
                 "09/11/2026\nBank transfer fee\n2.50\n149999997.50\n10/11/2026\nDeposit account interest\n1250000\n151 249 997\n"
                 "11/11/2026\nWire transfer\n70 123 456\n81 126 541\nSavings account\n1250000")
    m = w.call("redact", text=statement)["result"]["masked"]
    check("statement: no transaction amount is hidden", m == statement, m)

    # --- PDF redacted in place ---
    import pymupdf
    sys.path.insert(0, os.path.join(ROOT, "tests"))
    from make_test_pdf import build as build_pdf
    src = os.path.join(tempfile.mkdtemp(), "brief.pdf")
    build_pdf(src)
    pdf_secrets = ["Rami", "Haddad", "rami.haddad@acme.com", "123 456", "0012345678901", "Maya", "Nadine",
                   "Karim Aoun", "96171234567", "Lea Antoine", "987 654", "Elie Saab"]
    for remove_images in (False, True):
        w.call("reset")
        r = w.call("redact", file_name="brief.pdf", file_b64=b64(open(src, "rb").read()), remove_images=remove_images)
        info = r["result"].get("pdf") if r["ok"] else None
        check(f"pdf (images {'removed' if remove_images else 'kept'}): redacted and verified",
              bool(info) and info["verified"] and info["boxes"] >= 10, r)
        if not info:
            continue
        pages = []
        n = 1
        while True:
            p = w.call("doc_page", doc_id=info["doc_id"], page=n)
            if not p["ok"]:
                break
            pages.append(p["result"])
            n += 1
        check("pdf: every page renders", len(pages) == info["pages"], len(pages))
        # What Claude will see: page text + rendered image.
        page_text = " ".join(p["text"] for p in pages)
        leaked = [s for s in pdf_secrets if s in page_text]
        check("pdf: page text has placeholders only", not leaked and "<PERSON_1>" in page_text, leaked or page_text[:200])
        check("pdf: innocent words and budget kept", "ceramic studio" in page_text and "70 000 000 LBP" in page_text, page_text[:300])
        img = pymupdf.Pixmap(base64.b64decode(pages[0]["png_b64"]))
        # The test image is orange (200,120,40) and sits under the table.
        orange = sum(1 for y in range(0, img.height, 4) for x in range(0, img.width, 4)
                     if (lambda c: abs(c[0] - 200) < 20 and abs(c[1] - 120) < 20 and abs(c[2] - 40) < 20)(img.pixel(x, y)))
        if remove_images:
            check("pdf: 'remove images' blanks the image", orange == 0, orange)
        else:
            check("pdf: images kept by default", orange > 50, orange)

    # The PDF file itself: open the worker's output directly to check hidden spots.
    stdout_backup = sys.stdout
    sys.stdout = io.StringIO()
    try:
        sys.path.insert(0, os.path.join(ROOT, "worker"))
        import importlib
        wk = importlib.import_module("worker")
    finally:
        sys.stdout = stdout_backup
    res = wk.op_redact({"file_name": "brief.pdf", "file_b64": b64(open(src, "rb").read())})
    out = wk.DOCS.get(res["pdf"]["doc_id"])["pdf"]
    d = pymupdf.open(stream=out, filetype="pdf")
    raw = out.decode("latin-1")
    all_text = " ".join(p.get_text() for p in d)
    check("pdf file: no detected value in its text layer", not [s for s in pdf_secrets if s in all_text], all_text[:200])
    check("pdf file: no value left in raw bytes", not [s for s in ["rami.haddad@acme.com", "Rami Haddad", "0012345678901"] if s in raw])
    check("pdf file: metadata cleared", not {k: v for k, v in d.metadata.items() if v and k not in ("format", "encryption")}, d.metadata)
    check("pdf file: comments, links, bookmarks, attachments removed",
          not any(list(p.annots()) for p in d) and not any(p.get_links() for p in d) and not d.get_toc() and not d.embfile_names())
    check("pdf file: original is never stored", all(open(src, "rb").read() != wk.DOCS.get(i)["pdf"] for i in wk.DOCS.ids())
          and not any(n.startswith("brief") for n in os.listdir(wk.DOCS.dir)))

    # --- bank statement PDF ---
    from make_test_pdf import build_statement
    stmt = os.path.join(tempfile.mkdtemp(), "statement.pdf")
    build_statement(stmt)
    wk.op_reset({})
    res = wk.op_redact({"file_name": "statement.pdf", "file_b64": b64(open(stmt, "rb").read())})
    sdoc = wk.DOCS.get(res["pdf"]["doc_id"])
    layer = " ".join(p.get_text() for p in pymupdf.open(stream=sdoc["pdf"], filetype="pdf"))
    compact = re.sub(r"\s", "", layer).lower()
    check("statement pdf: verified", res["pdf"]["verified"], res["pdf"])
    check("statement pdf: glued, wrapped IBANs and account gone from the file",
          "lb620999" not in compact and "sa038000" not in compact and "01229114" not in compact
          and "10167519" not in compact and "1234567890" not in compact, layer[:300])
    check("statement pdf: every amount still there",
          all(a in layer for a in ["1,250.00", "8,750.00", "500000", "11750000", "45 000 000", "150000000",
                                   "1250000", "151 249 997", "70 123 456", "81 126 541"]), layer)
    claude_text = " ".join(sdoc["masked_pages"])
    check("statement pdf: text for Claude has placeholders, no IBAN",
          claude_text.count("<IBAN_CODE_") == 2 and "0999" not in claude_text and "8000 0000" not in claude_text, claude_text[:300])
    import stat
    modes = {stat.S_IMODE(os.stat(os.path.join(wk.DOCS.dir, n)).st_mode) for n in os.listdir(wk.DOCS.dir)}
    check("redacted copies are private files (owner only)", modes == {0o600} and stat.S_IMODE(os.stat(wk.DOCS.dir).st_mode) == 0o700, modes)
    cache_blob = b"".join(open(os.path.join(wk.DOCS.dir, n), "rb").read() for n in os.listdir(wk.DOCS.dir)).lower()
    check("nothing sensitive in the cache folder", not [v for v in [b"lb620999", b"haddad", b"1234567890", b"sa03 8000", b"karim"] if v in cache_blob])
    wk2 = wk.DocStore()
    check("a second engine can read the same document (restart-safe)", wk2.get(res["pdf"]["doc_id"])["pages"] == 1)
    for k in range(10):
        wk.DOCS.put("doc_%012x" % k, dict(sdoc))
    check("only the newest 8 copies are kept", len(wk.DOCS.ids()) == 8, len(wk.DOCS.ids()))
    wk.DOCS.put(res["pdf"]["doc_id"], dict(sdoc, verified=False))
    try:
        wk.op_doc_page({"doc_id": res["pdf"]["doc_id"], "page": 1, "require_verified": True})
        blocked = False
    except ValueError:
        blocked = True
    check("a document that fails the check can't be read by Claude", blocked)
    check("...but the panel can still preview it", bool(wk.op_doc_page({"doc_id": res["pdf"]["doc_id"], "page": 1})["png_b64"]))

    wk.DOCS.clear()
    check("'Forget all' empties the cache folder", os.listdir(wk.DOCS.dir) == [])

    # --- amounts are never names or phones; no matches across table cells ---
    w.call("reset")
    cells = "Online Purchase SHOP BEIRUT LB\n4821\n70.25\n-\n812.40\nTransfer via Mobile - shukran habibi\n25.00\n-\n5\n0.25\n1,250.00\n(300.00)"
    m = w.call("redact", text=cells)["result"]["masked"]
    check("amounts next to words are not names or phones",
          all(a in m for a in ["70.25", "812.40", "25.00", "0.25", "1,250.00", "(300.00)", "4821"]), m)
    from pdf_redact import value_pattern
    check("pdf matching: no match across table cells", not value_pattern("50.25").search("5\n0.25") and not value_pattern("50.25").search("5 0.25\n"))
    check("pdf matching: still follows a value over a line break between words",
          bool(value_pattern("LB62 0999 0001").search("iban:lb62 0999\n0001 x")))

    # --- review step: keep a false hit visible, add a miss ---
    w.call("reset")
    r = w.call("redact", text="Order from Sami Market for Rami Haddad, code ZX-77.")["result"]
    rows = {d["value"]: d for d in r["detections"]}
    check("review: detections listed with type, tag and count",
          rows.get("Rami Haddad", {}).get("tag") == "<PERSON_2>" and ("Sami Market" in rows or "Sami" in rows), r["detections"])
    shop = "Sami Market" if "Sami Market" in rows else "Sami"
    r = w.call("redact", text="Order from Sami Market for Rami Haddad, code ZX-77.", keep=[shop], custom_terms=["ZX-77"])["result"]
    check("review: unticked value stays visible, added value is hidden",
          "Sami" in r["masked"] and "Rami" not in r["masked"] and "ZX-77" not in r["masked"] and "<CUSTOM_1>" in r["masked"], r["masked"])
    tags = re.findall(r"<CUSTOM_\d+>", r["masked"])
    r2 = w.call("redact", text="New code QW-12.", custom_terms=["QW-12"])["result"]
    check("review: placeholder numbers are never reused", re.findall(r"<CUSTOM_\d+>", r2["masked"]) == ["<CUSTOM_2>"] and tags == ["<CUSTOM_1>"], (tags, r2["masked"]))
    r = w.call("redact", text="Later, Sami Market called again.")["result"]
    check("review: a kept value stays visible in later messages", "Sami Market" in r["masked"] or "Sami" in r["masked"], r["masked"])
    r = w.call("redact", text="Ask Sami Market.", custom_terms=[shop])["result"]
    check("review: adding a kept value again hides it", "Sami" not in r["masked"], r["masked"])
    w.call("reset")
    r = w.call("redact", text="Order from Sami Market for Rami Haddad.")["result"]
    check("review: Forget all clears the kept list", "Rami" not in r["masked"])

    # --- flatten: the file keeps no text layer ---
    wk.op_reset({})
    res = wk.op_redact({"file_name": "brief.pdf", "file_b64": b64(open(src, "rb").read()), "flatten": True})
    fdoc = wk.DOCS.get(res["pdf"]["doc_id"])
    flat = pymupdf.open(stream=fdoc["pdf"], filetype="pdf")
    check("flatten: verified, no text layer, same page count",
          res["pdf"]["verified"] and res["pdf"]["flattened"] and not "".join(p.get_text() for p in flat).strip()
          and flat.page_count == pymupdf.open(src).page_count, res["pdf"])
    check("flatten: Claude still gets redacted text", "<PERSON_1>" in " ".join(fdoc["masked_pages"]) and "Rami" not in " ".join(fdoc["masked_pages"]))

    # --- OCR: a scanned statement ---
    from pdf_redact import ocr_available
    if ocr_available():
        scan_path = os.path.join(tempfile.mkdtemp(), "scan.pdf")
        srcdoc, scan = pymupdf.open(stmt), pymupdf.open()
        for p in srcdoc:
            pg = scan.new_page(width=p.rect.width, height=p.rect.height)
            pg.insert_image(pg.rect, stream=p.get_pixmap(dpi=150).tobytes("png"))
        scan.save(scan_path)
        wk.op_reset({})
        res = wk.op_redact({"file_name": "scan.pdf", "file_b64": b64(open(scan_path, "rb").read())})
        sdoc = wk.DOCS.get(res["pdf"]["doc_id"])
        check("ocr: scanned PDF redacted and verified", res["pdf"]["verified"] and res["pdf"]["ocr_pages"] == 1, res["pdf"])
        from pdf_redact import _ocr_page
        reread = " ".join(t for t, _ in _ocr_page(pymupdf.open(stream=sdoc["pdf"], filetype="pdf")[0]))
        compact = re.sub(r"\s", "", reread).lower()
        check("ocr: IBANs, account and name gone from the picture",
              "0999" not in reread and "10167519" not in compact and "1234567890" not in compact and "maya" not in compact, reread[:300])
        check("ocr: amounts still readable in the picture",
              all(a in compact for a in ["1,250.00", "11750000", "150000000", "81126541"]), reread[:400])
    else:
        check("ocr: add-on installed for tests", False, "pip install -r requirements-ocr.txt")

    # --- optional models: fall back cleanly when missing ---
    import models
    check("models: off unless switched on", models.STATUS == {"gliner": "off", "arabic": "off"}, models.STATUS)
    os.environ["REDACTOR_ARABIC"] = "1"
    def broken():
        raise OSError("model not downloaded")
    from presidio_analyzer import AnalyzerEngine
    check("models: a missing Arabic model is skipped, not fatal",
          models.add_arabic(AnalyzerEngine(), pipeline_factory=broken) is False and models.STATUS["arabic"] == "unavailable")
    fake = lambda t: [{"entity_group": "FIRSTNAME", "start": t.find("وسيم"), "end": t.find("وسيم") + 4, "score": 0.97},
                      {"entity_group": "AMOUNT", "start": 0, "end": 3, "score": 0.99}] if "وسيم" in t else []
    rec = models.ArabicPiiRecognizer(fake)
    txt = "Meeting notes: اتصل بوسيم غداً, budget 500 USD"
    hits = rec.analyze(txt, ["PERSON"])
    check("models: Arabic hits mapped to the whole text, amounts ignored",
          len(hits) == 1 and txt[hits[0].start:hits[0].end] == "وسيم" and hits[0].entity_type == "PERSON", [(h.entity_type, txt[h.start:h.end]) for h in hits])
    check("models: label mapping",
          [models.map_arabic_label(x) for x in ["B-FIRSTNAME", "LASTNAME", "IBAN", "PHONE", "AMOUNT", "DATE", "CITY", "USERNAME"]]
          == ["PERSON", "PERSON", "IBAN_CODE", "PHONE_NUMBER", None, None, "LOCATION", None])
    os.environ.pop("REDACTOR_ARABIC")

    # GLiNER with a stand-in model, in a fresh engine
    env = dict(os.environ, REDACTOR_NER="gliner", PYTHONPATH=os.path.join(ROOT, "tests", "stubs"))
    probe = subprocess.run([sys.executable, "-c", (
        "import sys, io, contextlib, json; sys.path.insert(0, 'worker')\n"
        "with contextlib.redirect_stdout(io.StringIO()):\n    import worker\n"
        "names = [r.name for r in worker.ANALYZER.registry.recognizers]\n"
        "m = worker.op_redact({'text': 'Meet Xavier Qubain and Rami Haddad. IBAN LB62 0999 0000 0001 0019 0122 9114, 70 000 000 LBP'})['masked']\n"
        "print(json.dumps([worker.features()['gliner'], 'SpacyRecognizer' in names, m]))")],
        cwd=ROOT, env=env, capture_output=True, text=True)
    try:
        status, spacy_left, gm = json.loads(probe.stdout.strip().splitlines()[-1])
    except Exception:
        status, spacy_left, gm = None, None, probe.stderr[-400:]
    check("gliner: replaces spaCy and finds names", status == "on" and spacy_left is False
          and gm.startswith("Meet <PERSON_1> and <PERSON_2>.") and "<IBAN_CODE_1>" in gm and "70 000 000 LBP" in gm, gm)

    # --- stats / reset ---
    held = w.call("stats")["result"]["values_held"]
    check("stats: codebook populated", held > 0, held)
    w.call("reset")
    check("reset: codebook cleared", w.call("stats")["result"]["values_held"] == 0)
    r = w.call("restore", text="<PERSON_1>")
    check("reset: old tags no longer restore", r["result"]["unresolved"] == ["<PERSON_1>"], r)

    stderr = w.close()
    check("privacy: no network connection attempted", "NETWORK_ATTEMPT" not in stderr,
          [l for l in stderr.splitlines() if "NETWORK_ATTEMPT" in l][:3])

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
