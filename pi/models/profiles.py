"""Read-only model profiles computed from recorded outcomes."""

from __future__ import annotations

import json
import math
from typing import Any

from pi.models import roles as model_roles


def _median(values: list[int]) -> int | None:
    """Calculate median using nearest-rank method."""
    if not values:
        return None
    sorted_values = sorted(values)
    n = len(sorted_values)
    # Nearest-rank method: median index = ceil((n + 1) / 2) - 1
    median_index = (n + 1) // 2 - 1
    return sorted_values[median_index]


def _p90(values: list[int]) -> int | None:
    """Calculate 90th percentile using nearest-rank method."""
    if not values:
        return None
    sorted_values = sorted(values)
    n = len(sorted_values)
    # Nearest-rank method: p90 index = ceil(0.9 * n) - 1
    p90_index = math.ceil(0.9 * n) - 1
    return sorted_values[p90_index]


def _forgotten_exclusion(forgotten_sessions: set[str]) -> tuple[str, list[str]]:
    """Return (WHERE clause, params) for excluding forgotten sessions."""
    if forgotten_sessions:
        placeholders = ",".join("?" * len(forgotten_sessions))
        return f"AND t.session_id NOT IN ({placeholders})", list(forgotten_sessions)
    return "", []  # No exclusion when no sessions are forgotten


def profiles(store) -> dict[str, Any]:
    """Generate read-only model profiles from recorded outcomes."""
    config_data = model_roles.load(store)
    revision = config_data["revision"]
    configuration = config_data["configuration"]

    if configuration is None:
        return {"configurationRevision": revision, "models": []}

    with store._connect() as db:
        # Get forgotten session IDs to exclude
        forgotten_sessions = {
            row["session_id"]
            for row in db.execute("SELECT session_id FROM forgotten_sessions").fetchall()
        }
        exclude_clause, exclude_params = _forgotten_exclusion(forgotten_sessions)

        models = []
        for model in configuration["models"]:
            route = model["route"]

            # Aggregate completed turns for this model route
            turn_stats = db.execute(
                f"""
                SELECT COUNT(*) as count,
                       SUM(cost_usd) as total_cost,
                       SUM(input_tokens) as total_input_tokens,
                       SUM(output_tokens) as total_output_tokens,
                       COUNT(CASE WHEN latency_ms IS NOT NULL THEN 1 END) as latency_count,
                       COUNT(CASE WHEN cost_usd IS NOT NULL THEN 1 END) as cost_count
                FROM turns t
                WHERE t.model = ?
                AND t.status = 'complete'
                {exclude_clause}
                """,
                [route, *exclude_params],
            ).fetchone()

            answers = turn_stats["count"]

            # Calculate latency percentiles
            latency_ms: dict[str, int | None] = {"median": None, "p90": None}
            if turn_stats["latency_count"] > 0:
                latency_values = [
                    row["latency_ms"]
                    for row in db.execute(
                        f"""
                        SELECT t.latency_ms
                        FROM turns t
                        WHERE t.model = ?
                        AND t.status = 'complete'
                        AND t.latency_ms IS NOT NULL
                        {exclude_clause}
                        """,
                        [route, *exclude_params],
                    ).fetchall()
                ]
                latency_ms["median"] = _median(latency_values)
                latency_ms["p90"] = _p90(latency_values)

            # Calculate cost metrics
            total_cost = turn_stats["total_cost"] or 0.0
            cost_usd = {
                "total": total_cost,
                "perAnswer": (total_cost / turn_stats["cost_count"])
                if turn_stats["cost_count"] > 0
                else None,
            }

            # Token sums
            tokens = {
                "input": turn_stats["total_input_tokens"] or 0,
                "output": turn_stats["total_output_tokens"] or 0,
            }

            # Judge scores and wins
            judge_stats: dict[str, Any] = {"judged": 0, "averageScore": None, "wins": 0}
            judge_rows = db.execute(
                f"""
                SELECT rj.result
                FROM response_judgements rj
                WHERE 1=1 {exclude_clause.replace("t.session_id", "rj.session_id")}
                """,
                exclude_params,
            ).fetchall()

            route_scores: list[float] = []
            route_wins = 0
            route_judged = 0
            for row in judge_rows:
                result = json.loads(row["result"])
                # Stored judgements list scores as [{"message_id", "score", "reason"}].
                winner = result.get("winner")
                for score_data in result.get("scores", []):
                    mid = score_data["message_id"]
                    # Map message to its turn's model route
                    turn_row = db.execute(
                        "SELECT t.model FROM turn_messages tm JOIN turns t ON t.id=tm.turn_id "
                        "WHERE tm.message_id=? AND tm.purpose='final'",
                        (mid,),
                    ).fetchone()
                    if turn_row and turn_row["model"] == route:
                        route_judged += 1
                        route_scores.append(score_data["score"])
                        if mid == winner:
                            route_wins += 1

            if route_judged > 0:
                judge_stats["judged"] = route_judged
                judge_stats["averageScore"] = round(sum(route_scores) / len(route_scores), 2)
                judge_stats["wins"] = route_wins

            # Owner votes
            votes = {"up": 0, "down": 0, "winner": 0}
            vote_row = db.execute(
                f"""
                SELECT rv.vote, rv.message_id
                FROM response_votes rv
                WHERE 1=1 {exclude_clause.replace("t.session_id", "rv.session_id")}
                """,
                exclude_params,
            ).fetchall()

            for row in vote_row:
                # Map message to its turn's model route
                turn_row = db.execute(
                    "SELECT t.model FROM turn_messages tm JOIN turns t ON t.id=tm.turn_id "
                    "WHERE tm.message_id=? AND tm.purpose='final'",
                    (row["message_id"],),
                ).fetchone()
                if (
                    turn_row
                    and turn_row["model"] == route
                    and row["vote"] in ("up", "down", "winner")
                ):
                    votes[row["vote"]] += 1

            models.append(
                {
                    "id": model["id"],
                    "name": model["name"],
                    "providerId": model["providerId"],
                    "enabled": model["enabled"],
                    "answers": answers,
                    "latencyMs": latency_ms,
                    "costUsd": cost_usd,
                    "tokens": tokens,
                    "judge": judge_stats,
                    "votes": votes,
                }
            )

        return {"configurationRevision": revision, "models": models}
