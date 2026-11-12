"""Redact a PDF in place with PyMuPDF.

The real text under each detected value is deleted (not just covered), a
placeholder such as <PERSON_1> is drawn in its place, and everything else
(layout, fonts, images, page count) is kept. Hidden places where names often
live are cleared too: metadata, comments and other annotations, form fields,
links, bookmarks and attachments. The result is re-read and rejected if any
detected value can still be found in it.
"""

import re

import pymupdf

FILL = (0.92, 0.92, 0.92)
TEXT_COLOR = (0.25, 0.25, 0.25)


def page_texts(data):
    """Text of each page, as PyMuPDF sees it (the same text we search later)."""
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        if doc.needs_pass:
            raise ValueError("This PDF is password protected. Remove the password and try again.")
        return [clean(page.get_text("text")) for page in doc]


def _fontsize(rect, tag):
    by_height = rect.height * 0.75
    by_width = rect.width / (0.52 * max(len(tag), 1))
    return max(min(by_height, by_width, 11), 0)


def _norm(s):
    return re.sub(r"\s+", " ", s).strip().lower()


_ALNUM_BEFORE = r"(?<![^\W_])"
_ALNUM_AFTER = r"(?![^\W_])"


def clean(text):
    """Ordinary spaces, no invisible characters (same rules as detection)."""
    from finance import normalize_text

    return normalize_text(text)


def value_pattern(value, bounded=True):
    """Regex for a value that tolerates any spacing or line breaks inside it.

    bounded: must not sit inside a longer word or number ("Rami" in "Ceramic").
    """
    tokens = value.split()
    if not tokens:
        return None
    # Inside a word: at most one same-line gap between letters (wide letter
    # spacing), none between digits. Between words: any spacing, including a line break. Without
    # this, "5" in one table cell and "0.25" in the next could read as "50.25".
    def word(tok):
        out = re.escape(tok[0])
        for a, b in zip(tok, tok[1:]):
            # Only letters may be letter-spaced; digits must touch.
            out += (r"[ \t]?" if a.isalpha() and b.isalpha() else "") + re.escape(b)
        return out

    body = r"\s*".join(word(tok) for tok in tokens)
    return re.compile((_ALNUM_BEFORE + body + _ALNUM_AFTER) if bounded else body, re.IGNORECASE)


def still_present(value, text):
    """Is value still readable in text? Used to verify the result."""
    pat = value_pattern(value)
    if pat and pat.search(text):
        return True
    compact = re.sub(r"[\W_]", "", value)
    if len(compact) >= 8:  # long values: also catch them glued to other text
        loose = value_pattern(value, bounded=False)
        return bool(loose and loose.search(text))
    return False


def page_chars(page):
    """The page as one string plus, for each character, its box and line.

    Built from single glyphs so a value is found whatever the PDF's word
    spacing: glued to a label, letter-spaced, or broken across lines.
    """
    text, boxes = [], []
    raw = page.get_text("rawdict", flags=pymupdf.TEXTFLAGS_TEXT)
    line_no = 0
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            line_no += 1
            prev = None
            for span in line.get("spans", []):
                size = span.get("size", 10)
                for ch in span.get("chars", []):
                    c = clean(ch["c"])
                    r = pymupdf.Rect(ch["bbox"])
                    if prev is not None and c.strip() and r.x0 - prev.x1 > size * 0.25:
                        text.append(" ")
                        boxes.append(None)  # a visual gap with no space glyph
                    if not c:
                        continue
                    for piece in c:
                        text.append(piece)
                        boxes.append((r, line_no) if piece.strip() else None)
                    if c.strip():
                        prev = r
            text.append("\n")
            boxes.append(None)
    return "".join(text), boxes


def find_rects(chars, value, bounded=True):
    """One rectangle per line for every match of value on the page."""
    text, boxes = chars
    pat = value_pattern(value, bounded)
    rects = []
    if not pat:
        return rects
    for m in pat.finditer(text):
        lines = {}
        for i in range(m.start(), m.end()):
            if boxes[i] is None:
                continue
            r, line = boxes[i]
            lines[line] = lines[line] | r if line in lines else pymupdf.Rect(r)
        rects.extend(lines.values())
    return rects


def redact_pdf(data, replacements, remove_images=False):
    """Return (pdf_bytes, report).

    replacements: list of (original_value, placeholder), longest values first.
    """
    doc = pymupdf.open(stream=data, filetype="pdf")
    hits = 0
    images_removed = 0
    located = set()
    all_chars = [page_chars(page) for page in doc]
    # A value detected in this document but not found as a whole word anywhere
    # (Arabic prefixes, text glued together) is matched without word edges.
    loose = {v for v, _ in replacements if not any(find_rects(c, v) for c in all_chars)}
    for page, chars in zip(doc, all_chars):
        # Comments, highlights, sticky notes and form fields can hold names.
        for annot in list(page.annots() or []):
            page.delete_annot(annot)
        for widget in list(page.widgets() or []):
            page.delete_widget(widget)
        for link in page.get_links():
            page.delete_link(link)

        covered = []
        for value, tag in replacements:
            for rect in find_rects(chars, value, bounded=value not in loose):
                located.add(value)
                # Text lines overlap by a point or two: a full-height box would
                # also delete letters on the lines above and below.
                h = rect.height
                rect = pymupdf.Rect(rect.x0, rect.y0 + h * 0.18, rect.x1, rect.y1 - h * 0.18)
                if any(rect in c for c in covered):
                    continue  # already inside a longer value's box ("Rami" in "Rami Haddad")
                covered.append(rect)
                fs = _fontsize(rect, tag)
                page.add_redact_annot(
                    rect,
                    text=tag if fs >= 3.5 else "",
                    fontname="helv",
                    fontsize=fs if fs >= 3.5 else 11,
                    align=pymupdf.TEXT_ALIGN_CENTER,
                    fill=FILL,
                    text_color=TEXT_COLOR,
                    cross_out=False,
                )
                hits += 1

        if remove_images:
            for info in page.get_image_info():
                r = pymupdf.Rect(info["bbox"]) & page.rect
                if r.is_empty:
                    continue
                page.add_redact_annot(r, text="image removed", fontname="helv", fontsize=8,
                                      fill=(0.85, 0.85, 0.85), text_color=TEXT_COLOR, cross_out=False)
                images_removed += 1

        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_PIXELS if remove_images else pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
            text=pymupdf.PDF_REDACT_TEXT_REMOVE,
        )

    # Hidden places names live.
    doc.set_metadata({})
    doc.del_xml_metadata()
    doc.set_toc([])
    for name in list(doc.embfile_names()):
        doc.embfile_del(name)

    out = doc.tobytes(garbage=4, deflate=True, clean=True)
    doc.close()

    # Verify: nothing we meant to hide may still be readable in the result,
    # and everything detected must have been found on a page.
    with pymupdf.open(stream=out, filetype="pdf") as check:
        remaining_text = clean("\n".join(p.get_text("text") for p in check))
        meta = {k: v for k, v in (check.metadata or {}).items() if v and k not in ("format", "encryption")}
        pages = check.page_count
    leaks = sorted({v for v, _ in replacements if still_present(v, remaining_text)})
    unlocated = sorted({v for v, _ in replacements} - located)
    return out, {
        "pages": pages,
        "boxes": hits,
        "images_removed": images_removed,
        "leaks": leaks,
        "unlocated": unlocated,
        "metadata_left": sorted(meta),
    }


def render_page(pdf_bytes, page_number, dpi=110):
    """PNG of one page of an already redacted PDF (1-based page number)."""
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        if not 1 <= page_number <= doc.page_count:
            raise ValueError(f"Page {page_number} does not exist (the document has {doc.page_count}).")
        page = doc[page_number - 1]
        return page.get_pixmap(dpi=dpi).tobytes("png"), page.get_text("text")
