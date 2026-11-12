"""Builds a realistic test PDF full of (made-up) personal details and hiding spots."""
import io
import sys

import pymupdf
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def build(path):
    buf = io.BytesIO()
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(buf, pagesize=A4, title="Brief for Maya Joseph Haddad", author="Rami Haddad")
    story = [
        Paragraph("Production Brief: Spring Campaign", styles["Title"]),
        Paragraph(
            "Client lead is <b>Rami Haddad</b> (rami.haddad@acme.com, +961 3 123 456). "
            "Our talent shortlist is led by Maya Joseph Haddad, with Elie Saab Junior as backup. "
            "The ceramic studio set and the budget of 70 000 000 LBP were approved on Monday. "
            "Please send the invoice to account number 0012345678901 before Friday.",
            styles["BodyText"],
        ),
        Spacer(1, 10),
        Paragraph(
            "Long paragraph to force wrapping: the director of photography will coordinate closely with "
            "our producer Nadine Abou Saab during the scouting week so that every location is confirmed.",
            styles["BodyText"],
        ),
        Spacer(1, 10),
    ]
    t = Table([["Role", "Name", "Phone"], ["Producer", "Karim Aoun", "96171234567"], ["Stylist", "Lea Antoine", "03 987 654"]])
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey), ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey)]))
    story += [t, Spacer(1, 12)]
    # a small coloured image (stands in for a photo or logo)
    pm = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 40), 0)
    pm.set_rect(pm.irect, (200, 120, 40))
    story.append(Image(io.BytesIO(pm.tobytes("png")), width=90, height=60))
    story.append(Paragraph("Page two note: Rami asked for the final cut by the 20th.", styles["BodyText"]))
    doc.build(story)

    # Add hiding spots reportlab can't: a comment annotation, a mailto link, a bookmark, an attachment.
    pdf = pymupdf.open(stream=buf.getvalue(), filetype="pdf")
    page = pdf[0]
    page.add_text_annot((400, 60), "Check fees with Rami Haddad")
    page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(50, 50, 200, 70), "uri": "mailto:rami.haddad@acme.com"})
    pdf.set_toc([[1, "Notes for Rami Haddad", 1]])
    pdf.embfile_add("contacts.txt", b"Rami Haddad +961 3 123 456")
    pdf.save(path)


def build_statement(path):
    """A made-up bank statement: IBAN glued to its label, wrapped IBAN, amounts table."""
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(path, pagesize=A4)
    story = [
        Paragraph("Bank Statement", styles["Title"]),
        Paragraph("Account holder: Maya Joseph Haddad", styles["BodyText"]),
        Paragraph("iban:lb62099900000001001901229114", styles["BodyText"]),
        Paragraph("Account No: 1234567890 USD", styles["BodyText"]),
        Paragraph(
            "Please note that for all incoming international transfers the beneficiary account is IBAN "
            "SA03 8000 0000 6080 1016 7519 held at the main branch.", styles["BodyText"]),
        Spacer(1, 10),
    ]
    rows = [["Date", "Description", "Debit", "Credit", "Balance"],
            ["01/11/2026", "Transfer to Karim Aoun", "1,250.00", "", "8,750.00"],
            ["05/11/2026", "ATM withdrawal", "500000", "", "11750000"],
            ["07/11/2026", "Card payment ref 123456", "45 000 000", "", "150000000"],
            ["10/11/2026", "Deposit account interest", "", "1250000", "151 249 997"],
            ["11/11/2026", "Wire transfer", "70 123 456", "", "81 126 541"]]
    t = Table(rows)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey)]))
    story.append(t)
    doc.build(story)


if __name__ == "__main__":
    build(sys.argv[1])
