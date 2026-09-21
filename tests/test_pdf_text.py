import io
import subprocess
from contextlib import closing
from types import SimpleNamespace

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from pi import attachments, forgetting, pdf_text, session_settings
from pi.loop import Loop
from pi.providers import Completion
from pi.routing import Router
from pi.store import Store


def document(pages=("First page", "Second page"), *, password=None, script=False):
    writer = PdfWriter()
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    for text in pages:
        page = writer.add_blank_page(width=600, height=800)
        if text is not None:
            stream = DecodedStreamObject()
            escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            stream.set_data(f"BT /F1 12 Tf 50 750 Td ({escaped}) Tj ET".encode("ascii"))
            page[NameObject("/Contents")] = writer._add_object(stream)
            page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({
                NameObject("/F1"): writer._add_object(font)})})
    if password:
        writer.encrypt(password)
    if script:
        writer.add_js("app.launchURL('https://example.invalid/private');")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_real_worker_extracts_text_and_keeps_page_boundaries_without_scripts():
    assert pdf_text.extract(document(script=True), 200_000) == "[PDF page 1]\nFirst page\n\n[PDF page 2]\nSecond page"


def test_mixed_text_and_blank_pages_are_not_silently_omitted():
    text = pdf_text.extract(document(("Real text", None)), 200_000)
    assert "[PDF page 2]\n[No extractable text" in text


@pytest.mark.parametrize("raw,reason", [
    (b"not a pdf", "damaged"),
    (b"%PDF-1.7\ntruncated", "damaged"),
    (document(password="private-pass"), "Password-protected"),
    (document((None,)), "OCR"),
    (document(tuple(None for _ in range(201))), "limit"),
    (document(("x" * 200001,)), "limit"),
], ids=["wrong-type", "truncated", "encrypted", "no-text", "page-limit", "text-limit"])
def test_invalid_encrypted_empty_and_excessive_pdfs_are_explicit(raw, reason):
    text, status, detail = attachments._extract(raw, "application/pdf")
    assert text is None and status == "unsupported" and reason in detail
    assert "private-pass" not in detail


def test_worker_timeout_returns_fixed_error_and_releases_slot(monkeypatch):
    original = pdf_text.subprocess.run

    def timeout(*args, **kwargs):
        assert "PRIVATE_SECRET" not in kwargs["env"]
        assert kwargs["stderr"] == subprocess.DEVNULL
        raise subprocess.TimeoutExpired("worker", 10, stderr=b"private secret")

    monkeypatch.setenv("PRIVATE_SECRET", "private secret")
    monkeypatch.setattr(pdf_text.subprocess, "run", timeout)
    with pytest.raises(pdf_text.PDFError, match="timed out") as error:
        pdf_text.extract(document(), 200_000)
    assert "private secret" not in str(error.value)
    monkeypatch.setattr(pdf_text.subprocess, "run", original)
    assert "First page" in pdf_text.extract(document(), 200_000)


@pytest.mark.parametrize("payload", [b'{}', b'[]', b'{"text":"bad\\u0000text"}', b'{"error":"private"}'])
def test_invalid_worker_output_never_enters_context(monkeypatch, payload):
    monkeypatch.setattr(pdf_text.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout=payload))
    with pytest.raises(pdf_text.PDFError):
        pdf_text.extract(document(), 200_000)


def test_pdf_turn_citations_restart_forgetting_and_session_isolation(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        other = store.create_session()
        resolve = session_settings.source_privacy
        raw = document()
        item = attachments.upload(store, sid, attachments.Metadata(name="report.pdf", type="application/pdf"),
                                  raw, resolve=resolve)
        assert item["processing"]["status"] == "extracted"

        class Provider:
            name = "local"

            def complete(self, messages, *, model):
                assert any("[PDF page 2]" in message.content and "Second page" in message.content
                           for message in messages)
                return Completion(provider=self.name, model=model, text=f"Answer [[{item['id']}:p0]]")

        result = Loop(store, Router(local_provider=Provider(), local_model="test")).run_turn(
            sid, "Read PDF", request_id="pdf_attachment_request_01", attachment_ids=[item["id"]])
        assert result["message"]["citations"] == [{"id": item["id"] + ":p0", "label": "Attachment passage 1"}]
        with pytest.raises(attachments.AttachmentError):
            attachments.extract(store, other, item["id"], resolve)
        with closing(Store(store.path)) as reopened:
            assert "First page" in attachments.passage(reopened, sid, item["id"], 0, resolve)["passage"]["text"]
            assert attachments.download(reopened, sid, item["id"], resolve)[1] == raw
    path = tmp_path / "pi.db"
    forgetting.forget(path, sid, forgetting.preview(path, sid)["confirmation"])
    with closing(Store(path)) as reopened:
        with pytest.raises(attachments.AttachmentError):
            attachments.passage(reopened, sid, item["id"], 0, resolve)
