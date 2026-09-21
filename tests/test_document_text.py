import io
import zipfile
from contextlib import closing

import pytest

from pi import attachments, document_text, session_settings
from pi.loop import Loop
from pi.routing import Router
from pi.store import Store
from tests.test_attachment_turns import Provider


def archive(xml):
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as file:
        file.writestr("word/document.xml", xml)
    return result.getvalue()


XML = (
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    "<w:body><w:p><w:r><w:t>First paragraph</w:t><w:tab/><w:t>42</w:t></w:r></w:p>"
    "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Table cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
    "</w:body></w:document>"
)


def test_docx_text_reaches_model_through_exact_attachment(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        item = attachments.upload(
            store,
            sid,
            attachments.Metadata(name="report.docx", type=document_text.DOCX),
            archive(XML),
            resolve=session_settings.source_privacy,
        )
        assert item["processing"]["status"] == "extracted"
        provider = Provider()
        Loop(store, Router(local_provider=provider, local_model="test")).run_turn(
            sid, "Read document", request_id="document_request_01", attachment_ids=[item["id"]]
        )
        assert any(
            "First paragraph" in message.content and "Table cell" in message.content
            for message in provider.calls[0]
        )


@pytest.mark.parametrize(
    "xml",
    [
        "invalid",
        '<!DOCTYPE x [<!ENTITY x "secret">]>' + XML,
        "<wrong/>",
        XML.replace("First paragraph", "x" * 200001),
    ],
    ids=["invalid-xml", "entity-declaration", "wrong-root", "text-limit"],
)
def test_invalid_or_oversized_docx_is_explicitly_unsupported(xml):
    assert attachments._extract(archive(xml), document_text.DOCX)[1] == "unsupported"


@pytest.mark.parametrize("media", ["text/markdown", "text/csv", "application/json"])
def test_text_formats_remain_inert(media):
    value = '=IMPORTXML("https://example.test")'
    assert attachments._extract(value.encode(), media)[:2] == (value, "extracted")


def test_document_limits_and_invalid_archive():
    assert attachments._extract(b"not a zip", document_text.DOCX)[1] == "unsupported"
    with pytest.raises(ValueError):
        document_text.docx(archive(XML), 5)
