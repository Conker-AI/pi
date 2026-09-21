from contextlib import closing

from pi import attachments, forgetting, project_sources, projects, session_settings
from pi.store import Store


def test_file_project_source_and_forgetting_use_real_hooks(tmp_path):
    path = tmp_path / "attachments.db"
    secret = b"attachment-integration-secret-59317"
    with closing(Store(path)) as store:
        sid = store.create_session()
        file = attachments.upload(
            store,
            sid,
            attachments.Metadata(name="note.txt", type="text/plain"),
            secret,
            resolve=session_settings.source_privacy,
        )
        reference = {"kind": "file", "sessionId": sid, "fileId": file["id"]}

        def resolve(ref):
            return project_sources.resolve(store, ref)

        assert resolve(reference)["label"] == "note.txt"
        project = projects.create(
            store, projects.Fields(name="Work", description="", instructions="")
        )
        projects.mutate(
            store,
            project["id"],
            projects.Link(expected_revision=1, reference=reference),
            "link",
            resolve,
        )
        assert (
            len(
                projects.context(
                    store,
                    project["id"],
                    projects.Privacy(memoryDisabled=False, harnessDisabled=False),
                    resolve,
                )["references"]
            )
            == 1
        )
    forgetting.forget(path, sid, forgetting.preview(path, sid)["confirmation"])
    with closing(Store(path)) as store:
        assert project_sources.resolve(store, reference) is None
    for file in tmp_path.iterdir():
        if file.is_file():
            assert secret not in file.read_bytes()
