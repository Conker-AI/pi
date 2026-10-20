"""One summary shape for agents, teams, templates, skills, cron jobs and projects."""

from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from pi.agents import definitions, profiles, skills, team_models
from pi.agents import teams as collaboration
from pi.api import server as api
from pi.context import projects
from pi.conversation.store import Store
from pi.schedule import jobs
from tests.test_collaboration import config, team, template
from tests.test_jobs import definition
from tests.test_skills import document

ADMIN = {"X-Pi-Key": "synthetic-admin"}


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def populate(store):
    agent = profiles.create(store, config())
    draft = collaboration.save(store, "template", template())
    published = collaboration.save(
        store, "template", template().model_copy(update={"name": "Published template"})
    )
    collaboration.publish(store, published["id"], team_models.Revision(expected_revision=1))
    crew = collaboration.save(store, "team", team(agent["id"]))
    skill = skills.import_skill(
        store, skills.Import(markdown=document("review"), expected_revision=0)
    )
    job = jobs.create(store, definition(enabled=False))
    project = projects.create(
        store,
        projects.Fields(name="Build Conker", description="Deliver", instructions="Be critical"),
    )
    return agent, draft, published, crew, skill, job, project


def test_every_kind_appears_in_the_same_shape(store):
    agent, draft, published, crew, skill, job, project = populate(store)
    rows = definitions.summaries(store)["results"]
    by_id = {row["id"]: row for row in rows}
    assert [row["kind"] for row in rows] == sorted(
        (row["kind"] for row in rows), key=definitions.KINDS.index
    )
    assert by_id[agent["id"]]["href"] == f"/agents/{agent['id']}"
    assert by_id[draft["id"]]["state"] == "draft"
    assert by_id[published["id"]]["state"] == "published"
    assert (
        by_id[crew["id"]]["kind"] == "team"
        and by_id[crew["id"]]["description"] == "Review evidence"
    )
    assert by_id[skill["id"]]["state"] == "active"
    assert by_id[job["id"]]["state"] == "disabled" and by_id[job["id"]]["kind"] == "job"
    assert by_id[project["id"]]["name"] == "Build Conker"
    for row in rows:
        assert set(row) == set(definitions.Summary.model_fields)


def test_filters_by_kind_search_and_archived(store):
    _, _, _, _, _, _, project = populate(store)
    assert {row["kind"] for row in definitions.summaries(store, "template")["results"]} == {
        "template"
    }
    found = definitions.summaries(store, search="published")["results"]
    assert [row["name"] for row in found] == ["Published template"]
    projects.mutate(
        store, project["id"], projects.Archive(expected_revision=1, archived=True), "archive"
    )
    assert project["id"] not in {row["id"] for row in definitions.summaries(store)["results"]}
    archived = definitions.summaries(store, "project", archived=True)["results"]
    assert archived[0]["state"] == "archived" and archived[0]["archivedAt"]


def test_long_descriptions_are_shortened_and_job_instructions_stay_out(store):
    projects.create(
        store, projects.Fields(name="Long", description="word " * 300, instructions="Be critical")
    )
    jobs.create(store, definition(instructions="private schedule instructions"))
    row = definitions.summaries(store, "project")["results"][0]
    assert len(row["description"]) == definitions.DESCRIPTION and row["description"].endswith("…")
    assert definitions.summaries(store, "job")["results"][0]["description"] == ""


def test_route_is_admin_or_owner_only_and_validates_kind(tmp_path, monkeypatch):
    with closing(Store(tmp_path / "pi.db")) as store:
        monkeypatch.setattr(api.app.state, "store", store, raising=False)
        monkeypatch.setattr(api.app.state, "admin_key", "synthetic-admin", raising=False)
        client = TestClient(api.app)
        assert client.get("/definitions").status_code == 401
        assert client.get("/definitions", headers=ADMIN).json()["schemaVersion"] == 1
        assert client.get("/definitions?kind=tool", headers=ADMIN).status_code == 422
