from contextlib import closing

from pi import project_sources, projects, session_settings, tasks
from pi.store import Store


def test_project_links_follow_live_privacy_and_archival(tmp_path):
    with closing(Store(tmp_path / "project.db")) as store:
        sid = store.create_session(title="Source conversation")

        def resolve(ref):
            return project_sources.resolve(store, ref)

        project = projects.create(
            store, projects.Fields(name="Project", description="", instructions="")
        )
        reference = {"kind": "conversation", "sessionId": sid}
        projects.mutate(
            store,
            project["id"],
            projects.Link(expected_revision=1, reference=reference),
            "link",
            resolve,
        )
        public = projects.Privacy(memoryDisabled=False, harnessDisabled=False)
        assert len(projects.context(store, project["id"], public, resolve)["references"]) == 1
        session_settings.save(
            store,
            sid,
            session_settings.Update(
                expected_revision=0,
                settings=session_settings.Settings(
                    agentId="companion",
                    privacy=session_settings.Privacy(memoryDisabled=True, harnessDisabled=False),
                ),
            ),
        )
        result = projects.context(store, project["id"], public, resolve)
        assert not result["references"]
        assert result["excluded"][0]["reason"] == "origin-private"
        store.close_session(sid, "closed")
        assert resolve(reference)["archived"]


def test_missing_and_unverified_file_sources_fail_closed(tmp_path):
    with closing(Store(tmp_path / "project.db")) as store:
        sid = store.create_session()
        for reference in (
            {"kind": "conversation", "sessionId": "missing"},
            {"kind": "task", "taskId": "missing"},
            {"kind": "file", "sessionId": sid, "fileId": "anything"},
        ):
            assert project_sources.resolve(store, reference) is None


def test_task_origin_comes_from_stored_task(tmp_path):
    with closing(Store(tmp_path / "project.db")) as store:
        sid = store.create_session()
        task = tasks.create(
            store,
            tasks.CreateTask(
                request_id="project_source_request",
                session_id=sid,
                outcome="Prepare report",
                criteria=["Report written"],
                run_ids=[],
            ),
        )
        source = project_sources.resolve(store, {"kind": "task", "taskId": task["id"]})
        assert source["originSessionId"] == sid
        assert source["label"] == "Prepare report"
        assert not source["archived"]
