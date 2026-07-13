"""Replay historical predictions through the agent and score accuracy."""

import asyncio
import json
import os
import sys

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
_SCRIPTS_DIR = os.path.join(_REPO_ROOT, "scripts")
_AGENT_DIR = os.path.join(_REPO_ROOT, "agents", "mlb-agent")


def fetch_resolved_predictions(trino_host: str, trino_port: int) -> list[dict]:
    """Fetch all resolved predictions with outcomes from Trino."""
    from trino.dbapi import connect as trino_connect
    from trino.exceptions import TrinoUserError

    conn = trino_connect(
        host=trino_host,
        port=trino_port,
        user="admin",
        catalog="lakehouse",
        schema="mlb",
    )
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT prediction_id, game_date, away_team, home_team,
                   picked_team, confidence, was_correct,
                   away_pitcher, home_pitcher, reasoning_summary,
                   actual_winner, away_score, home_score
            FROM lakehouse.mlb.prediction_history
            WHERE was_correct IS NOT NULL
            ORDER BY game_date
        """)
    except TrinoUserError as e:
        conn.close()
        if "TABLE_NOT_FOUND" in str(e):
            raise RuntimeError(
                "Table lakehouse.mlb.prediction_history does not exist. "
                "Load it first: make load-predictions"
            ) from e
        raise
    columns = [d[0] for d in cur.description]
    rows = cur.fetchall()
    conn.close()
    return [dict(zip(columns, row)) for row in rows]


def format_prediction_question(games: list[dict]) -> str:
    """Format a batch of games as a prediction question for the agent."""
    lines = ["Pick the winners of these games:\n"]
    for g in games:
        away_p = g.get("away_pitcher") or "TBD"
        home_p = g.get("home_pitcher") or "TBD"
        lines.append(
            f"{g['away_team']} @ {g['home_team']}\n"
            f"{away_p} vs {home_p}\n"
        )
    return "\n".join(lines)


def build_agent(
    system_prompt: str,
    model_name: str,
    model_endpoint: str,
    trino_host: str,
    trino_port: int,
):
    """Build a LangGraph ReAct agent with the given system prompt."""
    os.environ["TRINO_QUERY_HOST"] = trino_host
    os.environ["TRINO_QUERY_PORT"] = str(trino_port)

    if _AGENT_DIR not in sys.path:
        sys.path.insert(0, _AGENT_DIR)

    from langchain_openai import ChatOpenAI
    from langgraph.prebuilt import create_react_agent

    from tools import query_trino, describe_datasets, get_methodology

    llm = ChatOpenAI(
        model=model_name,
        base_url=model_endpoint,
        api_key=os.environ.get("OPENAI_API_KEY", "not-required"),
        temperature=0.3,
        max_tokens=8192,
        streaming=False,
        model_kwargs={
            "extra_body": {"chat_template_kwargs": {"enable_thinking": False}}
        },
    )

    return create_react_agent(
        model=llm,
        tools=[query_trino, describe_datasets, get_methodology],
        prompt=system_prompt,
    )


def run_agent_prediction(agent, question: str) -> str:
    """Invoke the agent and return the final AI response text."""
    try:
        import nest_asyncio

        nest_asyncio.apply()
    except ImportError:
        pass

    from langchain_core.messages import HumanMessage

    result = asyncio.run(
        agent.ainvoke(
            {"messages": [HumanMessage(content=question)]},
            config={"recursion_limit": 150},
        )
    )
    for m in reversed(result.get("messages", [])):
        if hasattr(m, "type") and m.type == "ai" and not getattr(m, "tool_calls", None):
            return m.content or ""
    return ""


def parse_agent_predictions(response_text: str) -> list[dict]:
    """Parse prediction picks from agent output.

    Reuses parse_predictions() from the load-predictions script.
    """
    if _SCRIPTS_DIR not in sys.path:
        sys.path.insert(0, _SCRIPTS_DIR)
    from importlib import import_module

    mod = import_module("load-predictions-trino")
    return mod.parse_predictions(response_text)


def normalize_team(name: str) -> str:
    """Normalize a team name to its canonical form."""
    if _SCRIPTS_DIR not in sys.path:
        sys.path.insert(0, _SCRIPTS_DIR)
    from importlib import import_module

    mod = import_module("load-predictions-trino")
    return mod.normalize_team(name)


def score_batch(
    parsed_predictions: list[dict],
    actual_games: list[dict],
) -> dict:
    """Score parsed predictions against actual game outcomes.

    Returns:
        {
            "correct": int,
            "total": int,
            "accuracy": float,
            "by_tier": {"STRONG": (correct, total), ...},
            "wrong": [{"game": ..., "picked": ..., "actual_winner": ...}, ...],
        }
    """
    actuals_by_matchup = {}
    for g in actual_games:
        key = (g["away_team"], g["home_team"])
        actuals_by_matchup[key] = g

    correct = 0
    total = 0
    by_tier: dict[str, list[int]] = {
        "STRONG": [0, 0],
        "LEAN": [0, 0],
        "COIN FLIP": [0, 0],
    }
    wrong = []

    for pred in parsed_predictions:
        away = normalize_team(pred.get("away_team", ""))
        home = normalize_team(pred.get("home_team", ""))
        picked = normalize_team(pred.get("picked_team", ""))
        confidence = (pred.get("confidence") or "LEAN").upper()
        if confidence not in by_tier:
            confidence = "LEAN"

        actual = actuals_by_matchup.get((away, home))
        if not actual or not picked:
            continue

        total += 1
        by_tier[confidence][1] += 1

        if picked == actual["actual_winner"]:
            correct += 1
            by_tier[confidence][0] += 1
        else:
            wrong.append({
                "away_team": away,
                "home_team": home,
                "picked": picked,
                "actual_winner": actual["actual_winner"],
                "confidence": confidence,
                "reasoning": pred.get("reasoning", ""),
            })

    return {
        "correct": correct,
        "total": total,
        "accuracy": correct / max(total, 1),
        "by_tier": {k: tuple(v) for k, v in by_tier.items()},
        "wrong": wrong,
    }
