"""Every package's tables, triggers and migrations, installed in dependency order."""

from pi.actions import identities as actions
from pi.agents import characters, continuity, skills, team_execution
from pi.agents import profiles as agents
from pi.agents import teams as collaboration
from pi.calls import runtime as calls
from pi.content import artifacts, attachments
from pi.context import compaction as context_compaction
from pi.context import controls as context_controls
from pi.context import decisions, project_context, projects, research_usage, universal_search
from pi.context import retrieval as context_retrieval
from pi.context import summaries as context_summaries
from pi.conversation import citations as message_citations
from pi.conversation import drafts, fact_checks, judgements, naming, output_settings, submissions
from pi.conversation import forks as message_forks
from pi.conversation import observation as session_observation
from pi.conversation import retries as response_retries
from pi.conversation import settings as session_settings
from pi.conversation import versions as response_versions
from pi.host import actions as system_actions
from pi.host import filesystem_reads
from pi.host import inventory as system_inventory
from pi.memory import edits as memory_edits
from pi.memory import proposals as memory_proposals
from pi.memory import store as memory_store
from pi.models import evaluations as model_evaluations
from pi.models import roles as model_roles
from pi.owner import preferences as owner_preferences
from pi.schedule import jobs, proposals, tasks
from pi.setup import choices as setup_choices
from pi.setup import model_probes as setup_model_probes
from pi.setup import protection as setup_protection
from pi.setup import receipts as setup_receipts
from pi.setup import rehearsal as setup_rehearsal
from pi.turns import context as turn_context
from pi.turns import control as turn_control
from pi.turns import queue as turn_queue
from pi.turns import steering as turn_steering
from pi.turns import thinking as background_thinking


def install(db):
    """Install all package schemas, triggers, and run migrations in dependency order."""
    db.executescript(session_settings.SCHEMA)
    db.executescript(skills.SCHEMA)
    setup_receipts.initialize(db)
    setup_choices.initialize(db)
    setup_model_probes.initialize(db)
    setup_protection.initialize(db)
    setup_rehearsal.initialize(db)
    db.executescript(memory_store.SCHEMA)
    db.executescript(memory_edits.SCHEMA)
    db.executescript(memory_proposals.SCHEMA)
    db.executescript(proposals.SCHEMA)
    memory_store.migrate(db)
    db.executescript(actions.SCHEMA)
    db.executescript(tasks.SCHEMA)
    db.executescript(submissions.SCHEMA)
    db.executescript(turn_control.SCHEMA)
    db.executescript(turn_queue.SCHEMA)
    db.executescript(message_forks.SCHEMA)
    db.executescript(turn_context.SCHEMA)
    db.executescript(response_versions.SCHEMA)
    db.executescript(response_retries.SCHEMA)
    db.executescript(judgements.SCHEMA)
    db.executescript(fact_checks.SCHEMA)
    db.executescript(turn_steering.SCHEMA)
    db.executescript(research_usage.SCHEMA)
    research_usage.migrate(db)
    db.executescript(universal_search.SCHEMA)
    db.executescript(message_citations.SCHEMA)
    owner_preferences.initialize(db)
    agents.initialize(db)
    db.executescript(projects.SCHEMA)
    db.executescript(project_context.SCHEMA)
    db.executescript(context_controls.SCHEMA)
    db.executescript(context_compaction.SCHEMA)
    db.executescript(context_retrieval.SCHEMA)
    db.executescript(collaboration.SCHEMA)
    collaboration.initialize(db)
    db.executescript(team_execution.SCHEMA)
    db.executescript(artifacts.SCHEMA)
    db.executescript(model_roles.SCHEMA)
    db.executescript(background_thinking.SCHEMA)
    db.executescript(jobs.SCHEMA)
    continuity.initialize(db)
    db.executescript(drafts.SCHEMA)
    db.executescript(context_summaries.SCHEMA)
    attachments.initialize(db)
    db.executescript(model_evaluations.SCHEMA)
    db.executescript(calls.SCHEMA)
    db.executescript(characters.SCHEMA)
    db.executescript(system_inventory.SCHEMA)
    system_inventory.initialize(db)
    db.executescript(filesystem_reads.SCHEMA)
    db.executescript(system_actions.SCHEMA)
    session_observation.initialize(db)
    db.executescript(output_settings.SCHEMA)
    db.executescript(naming.SCHEMA)
    db.executescript(decisions.SCHEMA)
