"""Kubeflow Pipeline for parallel backtesting of MLB game predictions.

Predicts the outcomes of ALL completed 2026 MLB games using the RL-tuned
system prompt, running predictions in parallel batches. Measures the true
accuracy of the prediction framework against a large dataset.

Pipeline steps:
1. setup_mlflow_op: Configure MLflow tracking
2. load_and_partition_op: Query completed games from Trino, partition into batches
3. predict_batch_op: Run inside ParallelFor — each instance predicts ~30 games
4. aggregate_score_op: Collect results, score against actuals, log to MLflow
5. report_results_op: Print scorecard

Usage:
    python backtesting/pipeline.py --compile
"""

import kfp
from kfp import dsl
from kfp.dsl import component
from kfp import kubernetes
from typing import List, NamedTuple


BASE_IMAGE = "python:3.12-slim"

COMMON_PACKAGES = [
    "mlflow>=3.10",
    "nest-asyncio>=1.6.0",
    "pydantic>=2.0.0",
    "httpx>=0.27.0",
]

AGENT_PACKAGES = COMMON_PACKAGES + [
    "langchain>=0.3",
    "langchain-openai>=0.3",
    "langchain-google-vertexai>=2.0",
    "langchain-core>=0.3",
    "langgraph>=0.4",
    "trino>=0.329",
    "openai>=1.0",
    "anthropic>=0.40",
]


# =============================================================================
# Step 1: Setup MLflow
# =============================================================================
@component(base_image=BASE_IMAGE, packages_to_install=COMMON_PACKAGES)
def setup_mlflow_op(
    mlflow_tracking_uri: str,
    mlflow_experiment_name: str,
    mlflow_workspace: str = "",
) -> str:
    """Configure MLflow tracking and return experiment name."""
    import os
    from pathlib import Path
    import mlflow

    os.environ["MLFLOW_TRACKING_INSECURE_TLS"] = "true"

    if not os.environ.get("MLFLOW_TRACKING_TOKEN"):
        sa_token_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
        if sa_token_path.exists():
            os.environ["MLFLOW_TRACKING_TOKEN"] = sa_token_path.read_text().strip()

    mlflow.set_tracking_uri(mlflow_tracking_uri)

    if mlflow_workspace:
        mlflow.set_workspace(mlflow_workspace)

    experiment_name = mlflow_experiment_name

    if mlflow_workspace:
        import mlflow.tracking.fluent as _fluent
        client = mlflow.MlflowClient()
        exps = client.search_experiments(filter_string=f"name = '{experiment_name}'")
        if exps:
            _fluent._active_experiment_id = exps[0].experiment_id
        else:
            _fluent._active_experiment_id = client.create_experiment(experiment_name)
    else:
        mlflow.set_experiment(experiment_name)

    print(f"MLflow: {mlflow_tracking_uri} | Experiment: {experiment_name}", flush=True)
    return experiment_name


# =============================================================================
# Step 2: Load completed games and partition into batches
# =============================================================================
@component(base_image=BASE_IMAGE, packages_to_install=COMMON_PACKAGES + ["trino>=0.329"])
def load_and_partition_op(
    trino_host: str,
    trino_port: int,
    batch_size: int = 30,
) -> NamedTuple("LoadOutput", [("partitions", List[str]), ("all_games_json", str)]):
    """Query all completed games from Trino and partition into batches.

    Returns:
        partitions: list of JSON strings, each containing a batch of game dicts
        all_games_json: single JSON string of all games (for scoring)
    """
    import json
    from collections import namedtuple
    from trino.dbapi import connect as trino_connect

    print("Connecting to Trino...", flush=True)
    conn = trino_connect(
        host=trino_host,
        port=trino_port,
        user="admin",
        catalog="lakehouse",
        schema="mlb",
    )
    cur = conn.cursor()
    cur.execute("""
        SELECT game_pk, game_date, away_team_name, home_team_name,
               away_score, home_score
        FROM lakehouse.mlb.live_games
        WHERE game_status = 'Final'
        ORDER BY game_date
    """)
    columns = [d[0] for d in cur.description]
    rows = cur.fetchall()

    # Create/recreate backtest_results table for this run
    print("Creating backtest_results table...", flush=True)
    cur.execute("DROP TABLE IF EXISTS lakehouse.mlb.backtest_results")
    cur.execute("""
        CREATE TABLE lakehouse.mlb.backtest_results (
            game_pk INTEGER,
            game_date VARCHAR,
            away_team VARCHAR,
            home_team VARCHAR,
            picked_team VARCHAR,
            confidence VARCHAR,
            actual_winner VARCHAR,
            was_correct INTEGER,
            reasoning VARCHAR,
            agent_model VARCHAR
        )
    """)
    print("  backtest_results table ready", flush=True)
    conn.close()

    # Build game dicts with actual winner computed
    games = []
    for row in rows:
        g = dict(zip(columns, row))
        g["actual_winner"] = (
            g["home_team_name"]
            if g["home_score"] > g["away_score"]
            else g["away_team_name"]
        )
        games.append(g)

    # Sort by game_date (already ordered by query, but be explicit)
    games.sort(key=lambda g: g["game_date"])

    print(f"Total completed games: {len(games)}", flush=True)

    # Partition into batches
    batches = []
    for i in range(0, len(games), batch_size):
        batch = games[i : i + batch_size]
        batches.append(json.dumps(batch))

    print(f"Partitioned into {len(batches)} batches of ~{batch_size}", flush=True)

    LoadOutput = namedtuple("LoadOutput", ["partitions", "all_games_json"])
    return LoadOutput(partitions=batches, all_games_json=json.dumps(games))


# =============================================================================
# Step 3: Predict a batch of games (runs inside ParallelFor)
# =============================================================================
@component(base_image=BASE_IMAGE, packages_to_install=AGENT_PACKAGES)
def predict_batch_op(
    games_json: str,
    mlflow_tracking_uri: str,
    agent_model: str,
    llm_base_url: str,
    prompt_name: str,
    trino_host: str,
    trino_port: int,
    mlflow_workspace: str = "",
) -> str:
    """Predict outcomes for a batch of ~30 games. Fully self-contained."""
    import asyncio
    import json
    import os
    import re
    from pathlib import Path

    # ---- MLflow setup ----
    os.environ["MLFLOW_TRACKING_INSECURE_TLS"] = "true"
    if not os.environ.get("MLFLOW_TRACKING_TOKEN"):
        sa_token_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
        if sa_token_path.exists():
            os.environ["MLFLOW_TRACKING_TOKEN"] = sa_token_path.read_text().strip()

    adc_path = Path("/adc/application_default_credentials.json")
    if adc_path.exists():
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(adc_path)

    import mlflow
    mlflow.set_tracking_uri(mlflow_tracking_uri)
    if mlflow_workspace:
        mlflow.set_workspace(mlflow_workspace)

    try:
        import nest_asyncio
        nest_asyncio.apply()
    except ImportError:
        pass

    # ---- Prediction parsing (inlined) ----
    TEAM_ALIASES = {
        "arizona diamondbacks": "Arizona Diamondbacks", "diamondbacks": "Arizona Diamondbacks", "d-backs": "Arizona Diamondbacks", "ari": "Arizona Diamondbacks",
        "atlanta braves": "Atlanta Braves", "braves": "Atlanta Braves", "atl": "Atlanta Braves",
        "baltimore orioles": "Baltimore Orioles", "orioles": "Baltimore Orioles", "bal": "Baltimore Orioles",
        "boston red sox": "Boston Red Sox", "red sox": "Boston Red Sox", "bos": "Boston Red Sox",
        "chicago cubs": "Chicago Cubs", "cubs": "Chicago Cubs", "chc": "Chicago Cubs",
        "chicago white sox": "Chicago White Sox", "white sox": "Chicago White Sox", "cws": "Chicago White Sox", "chw": "Chicago White Sox",
        "cincinnati reds": "Cincinnati Reds", "reds": "Cincinnati Reds", "cin": "Cincinnati Reds",
        "cleveland guardians": "Cleveland Guardians", "guardians": "Cleveland Guardians", "cle": "Cleveland Guardians",
        "colorado rockies": "Colorado Rockies", "rockies": "Colorado Rockies", "col": "Colorado Rockies",
        "detroit tigers": "Detroit Tigers", "tigers": "Detroit Tigers", "det": "Detroit Tigers",
        "houston astros": "Houston Astros", "astros": "Houston Astros", "hou": "Houston Astros",
        "kansas city royals": "Kansas City Royals", "royals": "Kansas City Royals", "kc": "Kansas City Royals",
        "los angeles angels": "Los Angeles Angels", "angels": "Los Angeles Angels", "laa": "Los Angeles Angels",
        "los angeles dodgers": "Los Angeles Dodgers", "dodgers": "Los Angeles Dodgers", "lad": "Los Angeles Dodgers",
        "miami marlins": "Miami Marlins", "marlins": "Miami Marlins", "mia": "Miami Marlins",
        "milwaukee brewers": "Milwaukee Brewers", "brewers": "Milwaukee Brewers", "mil": "Milwaukee Brewers",
        "minnesota twins": "Minnesota Twins", "twins": "Minnesota Twins", "min": "Minnesota Twins",
        "new york mets": "New York Mets", "mets": "New York Mets", "nym": "New York Mets",
        "new york yankees": "New York Yankees", "yankees": "New York Yankees", "nyy": "New York Yankees",
        "oakland athletics": "Athletics", "athletics": "Athletics", "a's": "Athletics", "oak": "Athletics",
        "philadelphia phillies": "Philadelphia Phillies", "phillies": "Philadelphia Phillies", "phi": "Philadelphia Phillies",
        "pittsburgh pirates": "Pittsburgh Pirates", "pirates": "Pittsburgh Pirates", "pit": "Pittsburgh Pirates",
        "san diego padres": "San Diego Padres", "padres": "San Diego Padres", "sd": "San Diego Padres",
        "san francisco giants": "San Francisco Giants", "giants": "San Francisco Giants", "sf": "San Francisco Giants",
        "seattle mariners": "Seattle Mariners", "mariners": "Seattle Mariners", "sea": "Seattle Mariners",
        "st. louis cardinals": "St. Louis Cardinals", "cardinals": "St. Louis Cardinals", "stl": "St. Louis Cardinals",
        "tampa bay rays": "Tampa Bay Rays", "rays": "Tampa Bay Rays", "tb": "Tampa Bay Rays",
        "texas rangers": "Texas Rangers", "rangers": "Texas Rangers", "tex": "Texas Rangers",
        "toronto blue jays": "Toronto Blue Jays", "blue jays": "Toronto Blue Jays", "tor": "Toronto Blue Jays",
        "washington nationals": "Washington Nationals", "nationals": "Washington Nationals", "wsh": "Washington Nationals",
        "boston": "Boston Red Sox", "cleveland": "Cleveland Guardians", "colorado": "Colorado Rockies",
        "cincinnati": "Cincinnati Reds", "baltimore": "Baltimore Orioles", "arizona": "Arizona Diamondbacks",
        "atlanta": "Atlanta Braves", "st. louis": "St. Louis Cardinals", "st louis": "St. Louis Cardinals",
        "detroit": "Detroit Tigers", "houston": "Houston Astros", "kansas city": "Kansas City Royals",
        "miami": "Miami Marlins", "milwaukee": "Milwaukee Brewers", "minnesota": "Minnesota Twins",
        "philadelphia": "Philadelphia Phillies", "pittsburgh": "Pittsburgh Pirates",
        "san diego": "San Diego Padres", "san francisco": "San Francisco Giants",
        "seattle": "Seattle Mariners", "tampa bay": "Tampa Bay Rays", "texas": "Texas Rangers",
        "toronto": "Toronto Blue Jays", "washington": "Washington Nationals", "oakland": "Athletics",
    }

    def clean_markdown(text):
        if not text:
            return text
        text = text.replace("**", "")
        text = re.sub(r'[\U00002705\U0000274c\U0001f3c6\U0001f4ca]', '', text)
        return text.strip()

    def normalize_team(name):
        if not name:
            return None
        cleaned = clean_markdown(name).strip()
        cleaned = re.sub(r'^\d+\.\s*', '', cleaned).strip()
        return TEAM_ALIASES.get(cleaned.lower(), cleaned)

    def parse_predictions(output_text):
        if not output_text:
            return []
        text = clean_markdown(output_text)
        preds = _parse_table_format(text)
        if not preds:
            preds = _parse_header_format(text)
        if not preds:
            preds = _parse_pick_lines(text)
        return preds

    def _parse_table_format(text):
        predictions = []
        lines = text.split("\n")
        header_idx = matchup_col = pick_col = conf_col = rationale_col = None
        for i, line in enumerate(lines):
            if "|" not in line:
                continue
            cells = [c.strip() for c in line.split("|") if c.strip()]
            lower_cells = [c.lower() for c in cells]
            if "matchup" in lower_cells:
                header_idx = i
                matchup_col = lower_cells.index("matchup")
                pick_col = lower_cells.index("pick") if "pick" in lower_cells else None
                conf_col = lower_cells.index("confidence") if "confidence" in lower_cells else None
                rationale_col = None
                for label in ("rationale", "key factors", "reasoning"):
                    if label in lower_cells:
                        rationale_col = lower_cells.index(label)
                        break
                continue
            if header_idx is None or pick_col is None:
                continue
            if re.match(r'^[\s|:-]+$', line):
                continue
            if len(cells) <= max(matchup_col, pick_col):
                continue
            matchup_text = cells[matchup_col]
            pick_text = cells[pick_col]
            confidence = cells[conf_col].upper() if conf_col and conf_col < len(cells) else None
            rationale = cells[rationale_col] if rationale_col and rationale_col < len(cells) else None
            if confidence:
                conf_map = {"HIGH": "STRONG", "MODERATE": "LEAN", "LOW": "COIN FLIP"}
                confidence = conf_map.get(confidence, confidence)
            m = re.search(r'(.+?)\s+@\s+(.+?)(?:\s*[---]|\s*$)', matchup_text)
            if not m:
                continue
            away = normalize_team(m.group(1))
            home = normalize_team(m.group(2))
            picked = normalize_team(pick_text)
            if not picked:
                continue
            predictions.append({
                "away_team": away, "home_team": home,
                "picked_team": picked, "confidence": confidence,
                "reasoning": rationale[:200] if rationale else None,
            })
        return predictions

    def _parse_header_format(text):
        predictions = []
        blocks = re.split(r'###\s*', text)
        for block in blocks:
            if not block.strip():
                continue
            block_clean = re.sub(r'^\d+\.\s*', '', block.strip())
            m = re.match(r'(.+?)\s+@\s+(.+?)(?:\s*[---].+?)?\s*\n', block_clean)
            if not m:
                continue
            away = normalize_team(m.group(1))
            home = normalize_team(m.group(2))
            pick_m = re.search(r'Pick:\s*(.+?)(?:\s*$)', block, re.M)
            picked = normalize_team(pick_m.group(1)) if pick_m else None
            conf_m = re.search(r'Confidence:\s*(STRONG|LEAN|COIN\s*FLIP|HIGH|MODERATE|LOW)', block, re.I)
            confidence = None
            if conf_m:
                conf_map = {"HIGH": "STRONG", "MODERATE": "LEAN", "LOW": "COIN FLIP"}
                confidence = conf_map.get(conf_m.group(1).upper(), conf_m.group(1).upper())
            reason_m = re.search(r'(?:[-•]\s+|Rationale:\s*)(.+)', block)
            reasoning = reason_m.group(1).strip()[:200] if reason_m else None
            if not picked:
                continue
            predictions.append({
                "away_team": away, "home_team": home,
                "picked_team": picked, "confidence": confidence,
                "reasoning": reasoning,
            })
        return predictions

    def _parse_pick_lines(text):
        predictions = []
        lines = text.split("\n")
        for i, line in enumerate(lines):
            pick_m = re.search(r'Pick:\s*(.+?)(?:\s*$)', line)
            if not pick_m:
                continue
            picked = normalize_team(pick_m.group(1))
            if not picked:
                continue
            away = home = None
            for j in range(max(0, i - 10), i):
                mm = re.search(r'(.+?)\s+@\s+(.+?)(?:\s*[---]|\s*$)', lines[j])
                if mm:
                    away = normalize_team(mm.group(1))
                    home = normalize_team(mm.group(2))
                    break
            predictions.append({
                "away_team": away, "home_team": home,
                "picked_team": picked, "confidence": None,
                "reasoning": None,
            })
        return predictions

    # ---- SQL blocking regex (with string-literal fix) ----
    BLOCKED_SQL = re.compile(
        r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE)\b", re.IGNORECASE
    )
    STRING_LITERAL = re.compile(r"'[^']*'")

    # ---- Trino tools (inlined) ----
    from trino.dbapi import connect as trino_connect
    from langchain_core.tools import tool

    @tool
    def query_trino_tool(sql: str) -> str:
        """Execute a read-only SQL query against the MLB Iceberg lakehouse.
        Schema: lakehouse.mlb. Tables: batting, pitching, fielding, people, teams, parks,
        series_post, awards_players, hall_of_fame, salaries, home_games, weather_stations,
        weather_daily, pitch_pitches, pitch_atbats, pitch_games, statcast_pitches,
        live_games, live_boxscore_batting, live_boxscore_pitching, live_plays,
        live_pitches, live_standings, prediction_history. Only SELECT allowed."""
        if BLOCKED_SQL.search(STRING_LITERAL.sub("''", sql)):
            return json.dumps({"error": "Only SELECT queries allowed."})
        try:
            conn = trino_connect(
                host=trino_host, port=trino_port,
                user="admin", catalog="lakehouse", schema="mlb",
            )
            cur = conn.cursor()
            cur.execute(sql)
            columns = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchmany(500)
            conn.close()
            return json.dumps({"results": [dict(zip(columns, r)) for r in rows], "row_count": len(rows)})
        except Exception as e:
            return json.dumps({"error": str(e)})

    @tool
    def describe_datasets(topic: str = "") -> str:
        """List available MLB datasets."""
        return json.dumps({"datasets": [
            {"name": "batting/pitching/fielding", "years": "1871-2025"},
            {"name": "live_* (2026 season)", "years": "2026"},
            {"name": "prediction_history", "years": "2026"},
        ]})

    @tool
    def get_methodology(dataset_name: str) -> str:
        """Get methodology for a specific MLB dataset."""
        return json.dumps({
            "data_type": "Official MLB records",
            "collection": "Box scores and Statcast tracking",
        })

    # ---- Agent builder ----
    from langchain_openai import ChatOpenAI
    from langgraph.prebuilt import create_react_agent
    from langchain_core.messages import HumanMessage

    def build_agent(system_prompt):
        if agent_model.startswith("claude"):
            from langchain_google_vertexai.model_garden import ChatAnthropicVertex
            gcp_project = os.environ.get("GCP_PROJECT_ID", "itpc-gcp-product-all-claude")
            vertex_region = os.environ.get("VERTEX_REGION", "us-east5")
            llm = ChatAnthropicVertex(
                model_name=agent_model,
                project=gcp_project,
                location=vertex_region,
                temperature=0.3,
                max_tokens=8192,
            )
            print(f"  Agent: {agent_model} via Vertex AI ({vertex_region})", flush=True)
        else:
            model_endpoint = llm_base_url
            if not model_endpoint.endswith("/v1"):
                model_endpoint += "/v1"
            llm = ChatOpenAI(
                model=agent_model,
                base_url=model_endpoint,
                api_key=os.environ.get("OPENAI_API_KEY", "x"),
                temperature=0.3,
                max_tokens=8192,
                streaming=False,
                model_kwargs={
                    "extra_body": {"chat_template_kwargs": {"enable_thinking": False}}
                },
            )
            print(f"  Agent: {agent_model} via MaaS", flush=True)

        return create_react_agent(
            model=llm,
            tools=[query_trino_tool, describe_datasets, get_methodology],
            prompt=system_prompt,
        )

    def run_prediction(agent, question):
        result = asyncio.run(agent.ainvoke(
            {"messages": [HumanMessage(content=question)]},
            config={"recursion_limit": 150},
        ))
        for m in reversed(result.get("messages", [])):
            if hasattr(m, "type") and m.type == "ai" and not getattr(m, "tool_calls", None):
                return m.content or ""
        return ""

    # ---- Load system prompt from MLflow ----
    print("Loading system prompt from MLflow...", flush=True)
    system_prompt = None
    try:
        prompt_obj = mlflow.genai.load_prompt(
            f"prompts:/{prompt_name}@production", allow_missing=True,
        )
        if prompt_obj:
            system_prompt = prompt_obj.template
            print(f"  Loaded: {prompt_name} v{prompt_obj.version} ({len(system_prompt)} chars)", flush=True)
    except Exception as e:
        print(f"  Could not load prompt: {e}", flush=True)

    if not system_prompt:
        return json.dumps([{"error": "Cannot load system prompt from MLflow"}])

    # ---- Build agent ----
    agent = build_agent(system_prompt)

    # ---- Process games ----
    batch_games = json.loads(games_json)
    batch_num = batch_games[0].get("game_pk", "?") if batch_games else "empty"
    print(f"Batch starting at game_pk={batch_num}: {len(batch_games)} games", flush=True)

    all_predictions = []
    chunk_size = 3

    for i in range(0, len(batch_games), chunk_size):
        chunk = batch_games[i : i + chunk_size]

        # Format question without pitcher names — agent will query them
        lines = ["Pick the winners of these games:\n"]
        for g in chunk:
            lines.append(
                f"{g['away_team_name']} @ {g['home_team_name']}\n"
                f"TBD vs TBD\n"
            )
        question = "\n".join(lines)

        try:
            response = run_prediction(agent, question)
            parsed = parse_predictions(response)
            print(
                f"  Chunk {i // chunk_size + 1}: parsed {len(parsed)} pick(s)",
                flush=True,
            )

            # Match parsed predictions back to game_pk
            for pred in parsed:
                pred_away = normalize_team(pred.get("away_team", ""))
                pred_home = normalize_team(pred.get("home_team", ""))
                for g in chunk:
                    g_away = normalize_team(g["away_team_name"])
                    g_home = normalize_team(g["home_team_name"])
                    if pred_away == g_away and pred_home == g_home:
                        all_predictions.append({
                            "game_pk": g["game_pk"],
                            "away_team": g["away_team_name"],
                            "home_team": g["home_team_name"],
                            "picked_team": pred.get("picked_team"),
                            "confidence": pred.get("confidence"),
                            "reasoning": pred.get("reasoning"),
                        })
                        break
        except Exception as e:
            print(f"  Chunk {i // chunk_size + 1} ERROR: {e}", flush=True)

    # Score this batch
    correct = 0
    total = 0
    by_tier = {"STRONG": [0, 0], "LEAN": [0, 0], "COIN FLIP": [0, 0]}
    games_by_pk = {g["game_pk"]: g for g in batch_games}
    for pred in all_predictions:
        gpk = pred.get("game_pk")
        game = games_by_pk.get(gpk)
        if not game or not pred.get("picked_team"):
            continue
        total += 1
        conf = (pred.get("confidence") or "LEAN").upper()
        if conf not in by_tier:
            conf = "LEAN"
        by_tier[conf][1] += 1
        if pred["picked_team"] == game["actual_winner"]:
            correct += 1
            by_tier[conf][0] += 1

    accuracy = correct / max(total, 1)
    print(
        f"Batch complete: {len(all_predictions)}/{len(batch_games)} predictions, "
        f"accuracy: {correct}/{total} ({accuracy:.1%})",
        flush=True,
    )

    # Write results to Trino backtest_results table
    try:
        conn = trino_connect(host=trino_host, port=trino_port, user="admin",
                             catalog="lakehouse", schema="mlb")
        cur = conn.cursor()
        for pred in all_predictions:
            gpk = pred.get("game_pk")
            game = games_by_pk.get(gpk, {})
            picked = pred.get("picked_team", "")
            conf = (pred.get("confidence") or "LEAN").upper()
            actual = game.get("actual_winner", "")
            was_correct = 1 if picked == actual else 0
            reasoning = (pred.get("reasoning") or "")[:200].replace("'", "''")
            cur.execute(f"""
                INSERT INTO lakehouse.mlb.backtest_results
                VALUES ({int(gpk)}, '{game.get('game_date', '')}',
                        '{game.get('away_team_name', '')}', '{game.get('home_team_name', '')}',
                        '{picked}', '{conf}', '{actual}', {was_correct},
                        '{reasoning}', '{agent_model}')
            """)
        conn.close()
        print(f"  Wrote {len(all_predictions)} results to backtest_results", flush=True)
    except Exception as e:
        print(f"  Failed to write to Trino: {e}", flush=True)

    result = {
        "correct": correct,
        "total": total,
        "accuracy": accuracy,
    }
    return json.dumps(result)


# =============================================================================
# Step 4: Aggregate results from Trino and log to MLflow
# =============================================================================
@component(base_image=BASE_IMAGE, packages_to_install=COMMON_PACKAGES + ["trino>=0.329"])
def aggregate_score_op(
    mlflow_tracking_uri: str,
    trino_host: str,
    trino_port: int,
    mlflow_workspace: str = "",
    experiment_name: str = "",
    agent_model: str = "",
) -> dict:
    """Query backtest_results from Trino, compute accuracy, log to MLflow."""
    import json
    import os
    from pathlib import Path

    os.environ["MLFLOW_TRACKING_INSECURE_TLS"] = "true"
    if not os.environ.get("MLFLOW_TRACKING_TOKEN"):
        sa_token_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
        if sa_token_path.exists():
            os.environ["MLFLOW_TRACKING_TOKEN"] = sa_token_path.read_text().strip()

    import mlflow
    mlflow.set_tracking_uri(mlflow_tracking_uri)
    if mlflow_workspace:
        mlflow.set_workspace(mlflow_workspace)
        import mlflow.tracking.fluent as _fluent
        client = mlflow.MlflowClient()
        exps = client.search_experiments(filter_string=f"name = '{experiment_name}'")
        if exps:
            _fluent._active_experiment_id = exps[0].experiment_id
        else:
            _fluent._active_experiment_id = client.create_experiment(experiment_name)
    else:
        mlflow.set_experiment(experiment_name)

    from trino.dbapi import connect as trino_connect

    print("Querying backtest_results from Trino...", flush=True)
    conn = trino_connect(host=trino_host, port=trino_port, user="admin",
                         catalog="lakehouse", schema="mlb")
    cur = conn.cursor()

    # Overall accuracy
    cur.execute("""
        SELECT COUNT(*) AS total, SUM(was_correct) AS correct,
               ROUND(CAST(SUM(was_correct) AS DOUBLE) / NULLIF(COUNT(*), 0), 4) AS accuracy
        FROM lakehouse.mlb.backtest_results
    """)
    row = cur.fetchone()
    total, correct, accuracy = int(row[0]), int(row[1] or 0), float(row[2] or 0)

    # Total games in dataset
    cur.execute("SELECT COUNT(*) FROM lakehouse.mlb.live_games WHERE game_status = 'Final'")
    total_games = int(cur.fetchone()[0])

    # Per-tier accuracy
    cur.execute("""
        SELECT confidence, COUNT(*) AS picks, SUM(was_correct) AS correct,
               ROUND(CAST(SUM(was_correct) AS DOUBLE) / NULLIF(COUNT(*), 0), 4) AS accuracy
        FROM lakehouse.mlb.backtest_results
        GROUP BY confidence ORDER BY accuracy DESC
    """)
    tier_rows = cur.fetchall()

    # Per-team accuracy (which teams do we predict best/worst)
    cur.execute("""
        SELECT picked_team, COUNT(*) AS picks, SUM(was_correct) AS correct,
               ROUND(CAST(SUM(was_correct) AS DOUBLE) / NULLIF(COUNT(*), 0), 4) AS accuracy
        FROM lakehouse.mlb.backtest_results
        GROUP BY picked_team HAVING COUNT(*) >= 5
        ORDER BY accuracy DESC
    """)
    team_rows = cur.fetchall()

    # Wrong picks sample
    cur.execute("""
        SELECT game_date, away_team, home_team, picked_team, confidence, actual_winner
        FROM lakehouse.mlb.backtest_results
        WHERE was_correct = 0
        ORDER BY game_date DESC LIMIT 50
    """)
    wrong_picks = cur.fetchall()
    conn.close()

    # Print report
    print(f"\n{'=' * 60}", flush=True)
    print(f"BACKTEST RESULTS ({agent_model})", flush=True)
    print(f"{'=' * 60}", flush=True)
    print(f"  Games in dataset: {total_games}", flush=True)
    print(f"  Predictions made: {total}", flush=True)
    print(f"  Coverage: {total / max(total_games, 1):.1%}", flush=True)
    print(f"  Overall accuracy: {correct}/{total} ({accuracy:.1%})", flush=True)
    print(f"\n  By confidence tier:", flush=True)
    for tr in tier_rows:
        print(f"    {tr[0]:12s} {int(tr[2] or 0)}/{int(tr[1])} ({float(tr[3] or 0):.1%})", flush=True)
    print(f"\n  Best teams to predict:", flush=True)
    for tr in team_rows[:5]:
        print(f"    {tr[0]:25s} {int(tr[2] or 0)}/{int(tr[1])} ({float(tr[3] or 0):.1%})", flush=True)
    print(f"\n  Worst teams to predict:", flush=True)
    for tr in team_rows[-5:]:
        print(f"    {tr[0]:25s} {int(tr[2] or 0)}/{int(tr[1])} ({float(tr[3] or 0):.1%})", flush=True)

    # Log to MLflow
    mlflow.start_run(run_name=f"backtest-{agent_model}")
    mlflow.log_param("model", agent_model)
    mlflow.log_param("total_games", total_games)
    mlflow.log_param("predictions_made", total)

    mlflow.log_metric("overall_accuracy", round(accuracy, 4))
    mlflow.log_metric("correct", correct)
    mlflow.log_metric("total_scored", total)
    mlflow.log_metric("coverage", round(total / max(total_games, 1), 4))

    for tr in tier_rows:
        tier_key = tr[0].lower().replace(" ", "_") if tr[0] else "unknown"
        mlflow.log_metric(f"accuracy_{tier_key}", round(float(tr[3] or 0), 4))
        mlflow.log_metric(f"count_{tier_key}", int(tr[1]))

    if wrong_picks:
        import tempfile
        wrong_data = [{"game_date": str(w[0]), "away": w[1], "home": w[2],
                        "picked": w[3], "confidence": w[4], "actual": w[5]}
                       for w in wrong_picks]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, prefix="wrong_picks_") as f:
            f.write(json.dumps(wrong_data, indent=2))
            mlflow.log_artifact(f.name, "wrong_picks")

    mlflow.end_run()

    return {
        "overall_accuracy": round(accuracy, 4),
        "correct": correct,
        "total_scored": total,
        "total_games": total_games,
        "matched": matched,
        "missed": missed,
        "by_tier": {k: {"correct": v[0], "total": v[1]} for k, v in by_tier.items()},
        "wrong_count": len(wrong_list),
    }


# =============================================================================
# Step 5: Report results
# =============================================================================
@component(base_image=BASE_IMAGE, packages_to_install=["pydantic>=2.0.0"])
def report_results_op(metrics: dict, mlflow_tracking_uri: str) -> str:
    """Print backtesting scorecard."""
    print("=" * 60)
    print("MLB BACKTESTING REPORT")
    print("=" * 60)

    if "error" in metrics:
        print(f"  ERROR: {metrics['error']}")
        return f"Backtesting failed: {metrics['error']}"

    accuracy = metrics.get("overall_accuracy", 0)
    correct = metrics.get("correct", 0)
    total = metrics.get("total_scored", 0)
    total_games = metrics.get("total_games", 0)
    matched = metrics.get("matched", 0)

    print(f"  Overall accuracy:  {accuracy:.1%} ({correct}/{total})")
    print(f"  Games in dataset:  {total_games}")
    print(f"  Coverage:          {matched}/{total_games} ({matched / max(total_games, 1):.1%})")
    print()

    by_tier = metrics.get("by_tier", {})
    for tier in ("STRONG", "LEAN", "COIN FLIP"):
        tier_data = by_tier.get(tier, {})
        c = tier_data.get("correct", 0)
        t = tier_data.get("total", 0)
        pct = f"{c / t:.1%}" if t else "N/A"
        print(f"  {tier:12s}: {c}/{t} ({pct})")

    print(f"\n  Wrong picks: {metrics.get('wrong_count', 0)}")
    print(f"\nView in MLflow: {mlflow_tracking_uri}")
    print("=" * 60)

    return (
        f"Backtesting complete. Accuracy: {accuracy:.1%} ({correct}/{total}) "
        f"over {total_games} games"
    )


# =============================================================================
# Pipeline Definition
# =============================================================================
@dsl.pipeline(
    name="MLB Backtesting",
    description="Parallel backtesting on all completed 2026 games",
)
def mlb_backtest_pipeline(
    mlflow_tracking_uri: str = "https://mlflow.redhat-ods-applications.svc:8443/mlflow",
    mlflow_workspace: str = "mlb-agent",
    mlflow_experiment_name: str = "mlb-backtesting",
    agent_model: str = "qwen36-27b",
    llm_base_url: str = "https://maas.apps.ocp.cloud.rhai-tmm.dev/prelude-maas/qwen36-27b/v1",
    prompt_name: str = "mlb-agent.system",
    trino_host: str = "trino.mlb-agent.svc.cluster.local",
    trino_port: int = 8080,
    batch_size: int = 30,
    parallelism: int = 16,
    llm_secret_name: str = "mlb-agent-maas-key",
    gcp_adc_secret_name: str = "gcp-adc",
):
    # Step 1: Setup MLflow
    setup = setup_mlflow_op(
        mlflow_tracking_uri=mlflow_tracking_uri,
        mlflow_experiment_name=mlflow_experiment_name,
        mlflow_workspace=mlflow_workspace,
    )
    setup.set_caching_options(False)

    # Step 2: Load and partition games
    partitions = load_and_partition_op(
        trino_host=trino_host,
        trino_port=trino_port,
        batch_size=batch_size,
    )
    partitions.set_caching_options(False)
    partitions.after(setup)

    # Step 3: Parallel prediction over all batches
    with dsl.ParallelFor(
        items=partitions.outputs["partitions"],
        parallelism=16,
    ) as batch_item:
        predict = predict_batch_op(
            games_json=batch_item,
            mlflow_tracking_uri=mlflow_tracking_uri,
            agent_model=agent_model,
            llm_base_url=llm_base_url,
            prompt_name=prompt_name,
            trino_host=trino_host,
            trino_port=trino_port,
            mlflow_workspace=mlflow_workspace,
        )
        predict.set_caching_options(False)

        # Mount secrets for LLM API keys
        kubernetes.use_secret_as_env(
            predict,
            secret_name=llm_secret_name,
            secret_key_to_env={"api-key": "OPENAI_API_KEY"},
        )
        kubernetes.use_secret_as_volume(
            predict,
            secret_name=gcp_adc_secret_name,
            mount_path="/adc",
        )

    # Step 4: Aggregate from Trino and log to MLflow (runs after all ParallelFor iterations)
    aggregate = aggregate_score_op(
        mlflow_tracking_uri=mlflow_tracking_uri,
        trino_host=trino_host,
        trino_port=trino_port,
        mlflow_workspace=mlflow_workspace,
        experiment_name=setup.output,
        agent_model=agent_model,
    )
    aggregate.set_caching_options(False)

    # Step 5: Report
    report_results_op(
        metrics=aggregate.output,
        mlflow_tracking_uri=mlflow_tracking_uri,
    )


# =============================================================================
# Main
# =============================================================================
if __name__ == "__main__":
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description="MLB Backtesting Pipeline")
    parser.add_argument("--compile", action="store_true", help="Compile to YAML")
    parser.add_argument("--output-dir", default="pipelines_gen")
    args = parser.parse_args()

    if args.compile:
        from kfp import compiler

        script_dir = Path(__file__).parent
        output_dir = script_dir / args.output_dir
        output_dir.mkdir(exist_ok=True)

        output_file = output_dir / "mlb-backtest-pipeline.yaml"
        compiler.Compiler().compile(
            pipeline_func=mlb_backtest_pipeline,
            package_path=str(output_file),
        )
        print(f"Pipeline compiled to: {output_file}")
    else:
        print("Usage: python backtesting/pipeline.py --compile")
