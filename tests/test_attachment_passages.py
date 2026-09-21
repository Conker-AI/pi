import hashlib
from contextlib import closing

import pytest

from pi import attachment_passages, attachments, session_settings
from pi.loop import Loop
from pi.providers import Completion
from pi.routing import Router
from pi.store import Store


def test_passages_reconstruct_unicode_without_omission():
    text = "א🙂\n" * 1000
    parts = attachment_passages.split("attachment", text)
    assert "".join(part["text"] for part in parts) == text
    assert parts == attachment_passages.split("attachment", text)
    for index, part in enumerate(parts):
        assert part["id"] == f"attachment:p{index}"
        assert text[part["start"] : part["end"]] == part["text"]
        assert hashlib.sha256(part["text"].encode()).hexdigest() == part["sha256"]


def test_passage_resolves_exact_source_and_disappears_after_removal(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        other = store.create_session()
        resolve = session_settings.source_privacy
        item = attachments.upload(
            store,
            sid,
            attachments.Metadata(name="source.txt", type="text/plain"),
            ("a" * 1200 + "actual passage").encode(),
            resolve=resolve,
        )
        value = attachments.passage(store, sid, item["id"], 1, resolve)
        assert value["passage"]["text"] == "actual passage"
        assert value["passage"]["id"] == item["id"] + ":p1"
        with pytest.raises(attachments.AttachmentError):
            attachments.passage(store, other, item["id"], 1, resolve)
        with pytest.raises(attachments.AttachmentError):
            attachments.passage(store, sid, item["id"], 2, resolve)
        attachments.remove(store, sid, item["id"], resolve)
        with pytest.raises(attachments.AttachmentError):
            attachments.passage(store, sid, item["id"], 1, resolve)


def test_final_citations_only_accept_bound_existing_passages(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        item = attachments.upload(
            store,
            sid,
            attachments.Metadata(name="secret-name.txt", type="text/plain"),
            b"source text",
            resolve=session_settings.source_privacy,
        )

        class Provider:
            name = "local"

            def complete(self, messages, *, model):
                return Completion(
                    provider=self.name,
                    model=model,
                    text=(
                        f"Answer [[{item['id']}:p0]] [[{item['id']}:p99]] "
                        f"[[attachment_{'a' * 32}:p0]]"
                    ),
                    citations=[
                        {"id": "attachment_fake:p0", "label": "forged", "excerpt": "forged"}
                    ],
                )

        result = Loop(store, Router(local_provider=Provider(), local_model="test")).run_turn(
            sid, "Read", request_id="passage_citation_request", attachment_ids=[item["id"]]
        )
        expected = [{"id": item["id"] + ":p0", "label": "Attachment passage 1"}]
        assert result["message"]["citations"] == expected
        assert store.get_message(result["message"]["id"])["citations"] == expected
