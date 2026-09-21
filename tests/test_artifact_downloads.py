import io
import zipfile
from contextlib import closing

import pytest
from docx import Document
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from pi import artifacts as a
from pi.artifacts_api import router
from pi.store import Store


@pytest.fixture
def api(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        state = {"owner": False, "unknown": False}

        def authorize():
            if not state["owner"]:
                raise HTTPException(403)

        def resolve(db, session_id):
            return None if state["unknown"] else {"memoryDisabled": False, "harnessDisabled": False}

        app = FastAPI()
        app.include_router(router(lambda: store, authorize, resolve))
        with TestClient(app) as client:
            yield store, client, state, resolve


def test_xlsx_literal_cells_download_auth_and_version(api):
    store, client, state, _ = api
    table = {
        "kind": "table",
        "columns": ["Name", "Value"],
        "rows": [
            ["Unicode \u05e9\u05dc\u05d5\u05dd", '=WEBSERVICE("https://invalid")'],
            ["Count", "0012"],
            ["Markup", "<script>&"],
        ],
    }
    record = a.create(store, a.Create(title="../../CON", content=table))
    path = f"/artifacts/{record['id']}/download?format=xlsx&version=1"
    assert client.get(path).status_code == 403
    state["owner"] = True
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"] == 'attachment; filename="artifact-CON-v1.xlsx"'
    book = load_workbook(io.BytesIO(response.content))
    assert list(book.active.values) == [tuple(table["columns"]), *map(tuple, table["rows"])]
    assert book.active["B2"].data_type == "s" and book.active.freeze_panes == "A2"
    book.close()
    with zipfile.ZipFile(io.BytesIO(response.content)) as package:
        assert not any("externalLinks" in n or "vbaProject" in n for n in package.namelist())
        assert b"<f>" not in package.read("xl/worksheets/sheet1.xml")
    assert client.get(path.replace("version=1", "version=99")).status_code == 422
    assert client.get(path.replace("format=xlsx", "format=docx")).status_code == 422


def test_docx_editable_headings_and_source_privacy(api):
    store, client, state, resolve = api
    session = store.create_session()
    turn = store.start_turn(session)
    message = store.complete_turn(turn, "# Report\nHello & <world>\n\u05e9\u05dc\u05d5\u05dd")
    record = a.create(
        store,
        a.FromMessage(title="Report", sessionId=session, messageId=message["id"]),
        resolve=resolve,
    )
    state["owner"] = True
    path = f"/artifacts/{record['id']}/download?format=docx"
    response = client.get(path)
    assert response.status_code == 200
    document = Document(io.BytesIO(response.content))
    assert [p.text for p in document.paragraphs] == [
        "Report",
        "Hello & <world>",
        "\u05e9\u05dc\u05d5\u05dd",
    ]
    assert document.paragraphs[0].style.name == "Heading 1"
    state["unknown"] = True
    assert client.get(path).status_code != 200
    assert b"Hello" not in client.get(path).content


def test_native_html_stays_attachment_and_office_controls_are_rejected(api):
    store, client, state, _ = api
    state["owner"] = True
    record = a.create(
        store, a.Create(title="Page", content={"kind": "html", "text": "<script>x</script>"})
    )
    response = client.get(f"/artifacts/{record['id']}/download")
    assert response.content == b"<script>x</script>"
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["content-disposition"].endswith('.html.txt"')
    record = a.create(
        store, a.Create(title="Control", content={"kind": "markdown", "text": "bad\x00text"})
    )
    response = client.get(f"/artifacts/{record['id']}/download?format=docx")
    assert response.status_code == 422
