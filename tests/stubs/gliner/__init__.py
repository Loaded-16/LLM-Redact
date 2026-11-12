"""Stand-in for the gliner package, used only by tests (the real model is a
600 MB download). Mimics the API Presidio's GLiNERRecognizer calls."""


class GLiNERConfig:  # noqa: D101
    pass


class _Tokenizer:
    def __call__(self, text, **kw):
        return {"input_ids": list(range(len(text.split())))}

    def tokenize(self, text):
        return text.split()

    def encode(self, text, **kw):
        return list(range(len(text.split())))


class _Processor:
    transformer_tokenizer = _Tokenizer()


class GLiNER:
    """Finds the made-up name 'Xavier Qubain' only, labelled 'person'."""

    loads = 0
    data_processor = _Processor()

    @classmethod
    def from_pretrained(cls, name, **kw):
        cls.loads += 1
        return cls()

    def predict_entities(self, text, labels, flat_ner=False, threshold=0.3, multi_label=False):
        out = []
        name = "Xavier Qubain"
        start = text.find(name)
        while start >= 0:
            out.append({"start": start, "end": start + len(name), "label": "person", "score": 0.95, "text": name})
            start = text.find(name, start + 1)
        return out
