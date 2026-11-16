"""AI models chosen at setup (GLiNER by default, Arabic by flag).

  REDACTOR_NER=gliner   GLiNER PII model replaces spaCy's name finder.
                         Better at unusual and non-English names, fewer
                         false names. Installed by default; skip with
                         bash setup.sh --no-gliner
  REDACTOR_ARABIC=1     OpenMed Arabic PII model for Arabic-script text
                         (names, addresses, IDs). Installed with:
                         bash setup.sh --arabic

Both load from the local model cache only (no downloads at run time). If a
model is missing or fails to load, the redactor keeps working without it and
the panel shows which engines are active.
"""

import os
import re
import sys

from presidio_analyzer import EntityRecognizer, RecognizerResult

GLINER_MODEL = os.environ.get("REDACTOR_GLINER_MODEL", "knowledgator/gliner-pii-base-v1.0")
ARABIC_MODEL = os.environ.get("REDACTOR_ARABIC_MODEL", "OpenMed/OpenMed-PII-Arabic-BigMed-Large-560M-v1")

# Run-time loading never touches the network.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

STATUS = {"gliner": "off", "arabic": "off"}


def _log(msg):
    sys.stderr.write(f"[redactor models] {msg}\n")


# ---------- GLiNER ----------

GLINER_LABELS = {
    "person": "PERSON",
    "first name": "PERSON",
    "last name": "PERSON",
    "email address": "EMAIL_ADDRESS",
    "phone number": "PHONE_NUMBER",
    "address": "LOCATION",
    "iban": "IBAN_CODE",
    "bank account number": "BANK_ACCOUNT",
    "passport number": "LONG_NUMBER",
    "national id number": "LONG_NUMBER",
}


def add_gliner(analyzer):
    """Swap spaCy's name finder for GLiNER. Returns True when active."""
    if os.environ.get("REDACTOR_NER", "").lower() != "gliner":
        return False
    try:
        from presidio_analyzer.predefined_recognizers import GLiNERRecognizer

        rec = GLiNERRecognizer(
            model_name=GLINER_MODEL,
            entity_mapping=GLINER_LABELS,
            flat_ner=False,
            multi_label=True,
            map_location="cpu",
        )
    except Exception as e:  # missing package or model: keep spaCy
        STATUS["gliner"] = "unavailable"
        _log(f"GLiNER not loaded, using spaCy instead: {e}")
        return False
    analyzer.registry.add_recognizer(rec)
    analyzer.registry.remove_recognizer("SpacyRecognizer")
    STATUS["gliner"] = "on"
    _log(f"GLiNER active ({GLINER_MODEL})")
    return True


# ---------- Arabic ----------

_ARABIC_RUN = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿ][؀-ۿݐ-ݿࢠ-ࣿ\s\d.,:/\-]*")


def map_arabic_label(label):
    """OpenMed labels (FIRSTNAME, IBAN, PHONE...) to this redactor's types."""
    lab = label.upper().replace("B-", "").replace("I-", "")
    if "NAME" in lab and "USER" not in lab and "COMPANY" not in lab:
        return "PERSON"
    if "IBAN" in lab:
        return "IBAN_CODE"
    if "ACCOUNT" in lab:
        return "BANK_ACCOUNT"
    if "PHONE" in lab or "TEL" in lab:
        return "PHONE_NUMBER"
    if "EMAIL" in lab:
        return "EMAIL_ADDRESS"
    if "CREDIT" in lab or "CARD" in lab:
        return "CREDIT_CARD"
    if any(k in lab for k in ("STREET", "ADDRESS", "BUILDING", "ZIP", "CITY", "STATE", "COUNTY")):
        return "LOCATION"
    if any(k in lab for k in ("SSN", "PASSPORT", "ID", "LICENSE", "IDCARD")):
        return "LONG_NUMBER"
    return None  # amounts, currencies, dates, jobs... are left alone


class ArabicPiiRecognizer(EntityRecognizer):
    """Runs the Arabic model on Arabic-script stretches of the text only."""

    def __init__(self, pipe):
        super().__init__(
            supported_entities=["PERSON", "IBAN_CODE", "BANK_ACCOUNT", "PHONE_NUMBER", "EMAIL_ADDRESS",
                                "CREDIT_CARD", "LOCATION", "LONG_NUMBER"],
            name="ArabicPiiRecognizer",
            supported_language="en",
        )
        self.pipe = pipe

    def load(self):
        pass

    def analyze(self, text, entities, nlp_artifacts=None):
        out = []
        for run in _ARABIC_RUN.finditer(text):
            chunk = run.group(0)
            if len(chunk.strip()) < 2:
                continue
            try:
                preds = self.pipe(chunk)
            except Exception as e:
                _log(f"Arabic model failed on a passage: {e}")
                continue
            for p in preds:
                etype = map_arabic_label(p.get("entity_group") or p.get("entity") or "")
                if not etype or etype not in entities or float(p.get("score", 0)) < 0.5:
                    continue
                s, e = run.start() + int(p["start"]), run.start() + int(p["end"])
                while s < e and text[s].isspace():
                    s += 1
                while e > s and text[e - 1].isspace():
                    e -= 1
                if e > s:
                    out.append(RecognizerResult(etype, s, e, float(p["score"])))
        return out


def add_arabic(analyzer, pipeline_factory=None):
    """Register the Arabic model. Returns True when active."""
    if os.environ.get("REDACTOR_ARABIC", "") not in ("1", "true", "yes"):
        return False
    try:
        if pipeline_factory is None:
            from transformers import pipeline

            def pipeline_factory():
                return pipeline("token-classification", model=ARABIC_MODEL, aggregation_strategy="simple", device=-1)
        pipe = pipeline_factory()
    except Exception as e:
        STATUS["arabic"] = "unavailable"
        _log(f"Arabic model not loaded: {e}")
        return False
    analyzer.registry.add_recognizer(ArabicPiiRecognizer(pipe))
    STATUS["arabic"] = "on"
    _log(f"Arabic model active ({ARABIC_MODEL})")
    return True
