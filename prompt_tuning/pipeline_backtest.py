"""Kubeflow Pipeline for RL-based prompt tuning.

Uses a Gymnasium-inspired RL loop to iteratively improve the
Game Prediction Framework section of the MLB agent's system prompt.
Each step evaluates the prompt against historical predictions in Trino
and logs metrics + prompt versions to MLflow.

Pipeline steps:
1. setup_mlflow_op: Configure MLflow tracking
2. run_tuning_op: RL tuning loop (inline all logic — KFP pods can't import local modules)
3. report_results_op: Print scorecard

Usage:
    python prompt_tuning/pipeline.py --compile
"""

import kfp
from kfp import dsl
from kfp.dsl import component
from kfp import kubernetes


BASE_IMAGE = "python:3.12-slim"

COMMON_PACKAGES = [
    "mlflow>=3.10",
    "nest-asyncio>=1.6.0",
    "pydantic>=2.0.0",
    "httpx>=0.27.0",
]

TUNING_PACKAGES = COMMON_PACKAGES + [
    "numpy>=1.26",
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
# Step 2: Run RL Prompt Tuning
# =============================================================================
@component(base_image=BASE_IMAGE, packages_to_install=TUNING_PACKAGES)
def run_tuning_op(
    mlflow_tracking_uri: str,
    experiment_name: str,
    agent_model: str,
    llm_base_url: str,
    trino_host: str = "trino",
    trino_port: int = 8080,
    max_steps: int = 50,
    eval_batch_size: int = 40,
    accuracy_target: float = 0.65,
    mlflow_workspace: str = "",
    meta_model: str = "claude-sonnet-4-6",
) -> dict:
    """RL prompt tuning: iteratively improve the Game Prediction Framework."""
    import asyncio
    import json
    import os
    import re
    from pathlib import Path

    import numpy as np

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
        import mlflow.tracking.fluent as _fluent
        client = mlflow.MlflowClient()
        exps = client.search_experiments(filter_string=f"name = '{experiment_name}'")
        if exps:
            _fluent._active_experiment_id = exps[0].experiment_id
        else:
            _fluent._active_experiment_id = client.create_experiment(experiment_name)
    else:
        mlflow.set_experiment(experiment_name)

    # ------------------------------------------------------------------
    # Prompt splitting and validation
    # ------------------------------------------------------------------
    START_MARKER = "### Game Prediction Framework"
    END_MARKER = "### Statistics Definitions"
    PLACEHOLDER = "{PREDICTION_FRAMEWORK}"

    def split_prompt(full_text):
        start = full_text.find(START_MARKER)
        end = full_text.find(END_MARKER)
        if start == -1 or end == -1:
            raise ValueError("Could not find section markers in prompt")
        mutable = full_text[start:end].rstrip() + "\n"
        template = full_text[:start] + PLACEHOLDER + "\n" + full_text[end:]
        return template, mutable

    def reconstruct(template, section):
        return template.replace(PLACEHOLDER, section.rstrip() + "\n")

    def validate_section(section):
        issues = []
        required = ["Step 1", "Step 2", "Step 3", "Output Format", "Anti-Patterns"]
        for h in required:
            if h not in section:
                issues.append(f"Missing: {h}")
        if len(section) > 20000:
            issues.append(f"Too long: {len(section)} chars")
        if len(section) < 200:
            issues.append(f"Too short: {len(section)} chars")
        return (len(issues) == 0, issues)

    # ------------------------------------------------------------------
    # Inlined: prediction parsing (from load-predictions-trino.py)
    # ------------------------------------------------------------------
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

    def _extract_pitcher_name(text):
        if not text:
            return None
        cleaned = clean_markdown(text).strip()
        m = re.match(r"([A-Za-z\s.'-]+?)(?:\(|$)", cleaned)
        return m.group(1).strip() if m else cleaned[:50]

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
            m = re.search(r'(.+?)\s+@\s+(.+?)(?:\s*[—\-–]|\s*$)', matchup_text)
            if not m:
                continue
            away = normalize_team(m.group(1))
            home = normalize_team(m.group(2))
            picked = normalize_team(pick_text)
            if not picked:
                continue
            predictions.append({"away_team": away, "home_team": home, "picked_team": picked, "confidence": confidence, "reasoning": rationale[:200] if rationale else None})
        return predictions

    def _parse_header_format(text):
        predictions = []
        blocks = re.split(r'###\s*', text)
        for block in blocks:
            if not block.strip():
                continue
            block_clean = re.sub(r'^\d+\.\s*', '', block.strip())
            m = re.match(r'(.+?)\s+@\s+(.+?)(?:\s*[—\-–].+?)?\s*\n', block_clean)
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
            predictions.append({"away_team": away, "home_team": home, "picked_team": picked, "confidence": confidence, "reasoning": reasoning})
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
                mm = re.search(r'(.+?)\s+@\s+(.+?)(?:\s*[—\-–]|\s*$)', lines[j])
                if mm:
                    away = normalize_team(mm.group(1))
                    home = normalize_team(mm.group(2))
                    break
            predictions.append({"away_team": away, "home_team": home, "picked_team": picked, "confidence": None, "reasoning": None})
        return predictions

    # ------------------------------------------------------------------
    # Inlined: replay logic
    # ------------------------------------------------------------------
    from trino.dbapi import connect as trino_connect
    from langchain_openai import ChatOpenAI
    from langgraph.prebuilt import create_react_agent
    from langchain_core.messages import HumanMessage
    from langchain_core.tools import tool

    try:
        import nest_asyncio
        nest_asyncio.apply()
    except ImportError:
        pass

    BLOCKED_SQL = re.compile(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE)\b", re.IGNORECASE)
    STRING_LITERAL = re.compile(r"'[^']*'")

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
            conn = trino_connect(host=trino_host, port=trino_port, user="admin", catalog="lakehouse", schema="mlb")
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
        return json.dumps({"data_type": "Official MLB records", "collection": "Box scores and Statcast tracking"})

    def fetch_predictions():
        print("Fetching backtest results from Trino...", flush=True)
        conn = trino_connect(host=trino_host, port=trino_port, user="admin", catalog="lakehouse", schema="mlb")
        cur = conn.cursor()
        cur.execute("""
            SELECT game_pk AS prediction_id, game_date, away_team, home_team,
                   picked_team, confidence, was_correct,
                   actual_winner
            FROM lakehouse.mlb.backtest_results
            ORDER BY game_date
        """)
        columns = [d[0] for d in cur.description]
        rows = cur.fetchall()
        conn.close()
        result = [dict(zip(columns, row)) for row in rows]
        print(f"  Found {len(result)} backtest results", flush=True)
        return result

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
                model=agent_model, base_url=model_endpoint,
                api_key=os.environ.get("OPENAI_API_KEY", "x"),
                temperature=0.3, max_tokens=8192, streaming=False,
                model_kwargs={"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}},
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

    def format_question(games):
        lines = ["Pick the winners of these games:\n"]
        for g in games:
            away_p = g.get("away_pitcher") or "TBD"
            home_p = g.get("home_pitcher") or "TBD"
            lines.append(f"{g['away_team']} @ {g['home_team']}\n{away_p} vs {home_p}\n")
        return "\n".join(lines)

    def score_batch(parsed, actuals):
        actuals_by_matchup = {(g["away_team"], g["home_team"]): g for g in actuals}
        correct = total = 0
        by_tier = {"STRONG": [0, 0], "LEAN": [0, 0], "COIN FLIP": [0, 0]}
        wrong = []
        for pred in parsed:
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
                wrong.append({"away_team": away, "home_team": home, "picked": picked, "actual_winner": actual["actual_winner"], "confidence": confidence})
        return {"correct": correct, "total": total, "accuracy": correct / max(total, 1), "by_tier": {k: tuple(v) for k, v in by_tier.items()}, "wrong": wrong}

    def evaluate_prompt(template, section, agent_batch, batch_size=3):
        full_prompt = reconstruct(template, section)
        agent = build_agent(full_prompt)
        total_batches = (len(agent_batch) + batch_size - 1) // batch_size
        all_parsed = []
        for i in range(0, len(agent_batch), batch_size):
            chunk = agent_batch[i:i + batch_size]
            batch_num = i // batch_size + 1
            games_str = ", ".join(f"{g['away_team']} @ {g['home_team']}" for g in chunk)
            print(f"  Batch {batch_num}/{total_batches}: {games_str}", flush=True)
            try:
                response = run_prediction(agent, format_question(chunk))
                parsed = parse_predictions(response)
                all_parsed.extend(parsed)
                print(f"    Parsed {len(parsed)} pick(s)", flush=True)
            except Exception as e:
                print(f"    ERROR: {e}", flush=True)
        result = score_batch(all_parsed, agent_batch)
        print(f"  Result: {result['correct']}/{result['total']} ({result['accuracy']:.1%})", flush=True)
        return result

    # ------------------------------------------------------------------
    # Inlined: meta-agent (Claude via Vertex AI)
    # ------------------------------------------------------------------
    import httpx

    def _get_gcp_access_token():
        """Exchange ADC refresh token for an access token."""
        adc_paths = [
            "/adc/application_default_credentials.json",
            os.path.expanduser("~/.config/gcloud/application_default_credentials.json"),
        ]
        adc = None
        for p in adc_paths:
            if os.path.exists(p):
                adc = json.loads(open(p).read())
                break
        if not adc:
            raise RuntimeError("No ADC credentials found")
        resp = httpx.post("https://oauth2.googleapis.com/token", data={
            "client_id": adc["client_id"],
            "client_secret": adc["client_secret"],
            "refresh_token": adc["refresh_token"],
            "grant_type": "refresh_token",
        })
        resp.raise_for_status()
        return resp.json()["access_token"]

    def _call_claude(prompt_text, temperature=0.4, max_tokens=8192):
        """Call Claude via Vertex AI rawPredict."""
        gcp_project = os.environ.get("GCP_PROJECT_ID", "itpc-gcp-product-all-claude")
        vertex_region = os.environ.get("VERTEX_REGION", "us-east5")
        meta_model = os.environ.get("META_MODEL", "claude-sonnet-4-6")
        token = _get_gcp_access_token()
        url = (
            f"https://{vertex_region}-aiplatform.googleapis.com/v1/"
            f"projects/{gcp_project}/locations/{vertex_region}/"
            f"publishers/anthropic/models/{meta_model}:rawPredict"
        )
        body = {
            "messages": [{"role": "user", "content": prompt_text}],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "anthropic_version": "vertex-2023-10-16",
        }
        resp = httpx.post(url, json=body, headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }, timeout=120.0)
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"]

    def _call_openai_meta(prompt_text):
        """Call meta-agent via OpenAI-compatible endpoint (MaaS)."""
        model_endpoint = llm_base_url
        if not model_endpoint.endswith("/v1"):
            model_endpoint += "/v1"
        meta_llm = ChatOpenAI(
            model=meta_model, base_url=model_endpoint,
            api_key=os.environ.get("OPENAI_API_KEY", "x"),
            temperature=0.4, max_tokens=8192, streaming=False,
            model_kwargs={"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}},
        )
        response = meta_llm.invoke([HumanMessage(content=prompt_text)])
        return response.content

    def generate_improved_prompt(current_section, metrics_text, error_text):
        meta_prompt = f"""You are an expert prompt engineer optimizing an MLB game prediction system.
The prediction agent uses a "Game Prediction Framework" section in its system prompt to decide how to pick winners. Your job is to improve this section to increase prediction accuracy.

## Current Prediction Framework
{current_section}

## Performance Metrics
{metrics_text}

## Error Analysis (Wrong Predictions)
{error_text}

## Rules for Modification

### MUST preserve
- The overall structure: Step 1 (Query the Data), Step 2 (Weight the Factors), Step 3 (Decision Rules), Step 4 (Output Format), Anti-Patterns, Self-Learning
- The SQL query templates in Step 1 — they are correct, do not change them
- The output format structure in Step 4 — the parsing system depends on it
- The Self-Learning section — it allows the agent to check its track record

### CAN adjust
- Weight percentages in Step 2 (they should roughly sum to 100%)
- ERA thresholds in decision rules (e.g., sub-3.00, above-4.50, above-5.00)
- Run differential thresholds
- Confidence tier criteria (STRONG/LEAN/COIN FLIP definitions)
- Anti-patterns — add new ones, refine existing ones
- Decision rule ordering and wording
- Batch size for processing games

### INCREMENTAL EDITS ONLY
Make exactly ONE or TWO targeted changes per iteration. Do NOT rewrite the entire section.
Examples of good changes:
- "Increase bullpen weight from 15% to 20%, decrease streaks from 2% to 0%"
- "Add anti-pattern: never pick a team with negative run differential as STRONG"
- "Change ERA threshold from 4.50 to 4.00 in decision rule 1"
Large rewrites destroy what already works. Small, targeted edits let us measure what helps.

### Optimization guidance from error analysis
- If STRONG picks are wrong too often: tighten STRONG criteria
- If COIN FLIP picks lean wrong: adjust decision rules for close matchups
- If home field is over-weighted: reduce its weight or add a stronger anti-pattern
- If bullpen is under-weighted: increase bullpen weight percentage
- If pitcher ERA thresholds miss too often: adjust the ERA boundaries

## Output
Return ONLY the complete replacement text for the Game Prediction Framework section.
Start with "### Game Prediction Framework" and end with the Self-Learning section.
Do NOT include any commentary before or after the section.
IMPORTANT: Keep the section under 18000 characters. Make minimal changes from the current version.
"""
        print(f"  Meta-agent: {meta_model}", flush=True)
        if meta_model.startswith("claude"):
            text = _call_claude(meta_prompt)
        else:
            text = _call_openai_meta(meta_prompt)
        start = text.find("### Game Prediction Framework")
        if start == -1:
            return text.strip()
        text = text[start:]
        end = text.find("### Statistics Definitions")
        if end != -1:
            text = text[:end]
        return text.rstrip() + "\n"

    # ------------------------------------------------------------------
    # Main RL loop
    # ------------------------------------------------------------------
    print("=" * 60, flush=True)
    print("MLB PROMPT TUNING — RL LOOP", flush=True)
    print("=" * 60, flush=True)

    # Load system prompt from MLflow
    print("Loading system prompt from MLflow...", flush=True)
    prompt_name = "mlb-agent.system"
    system_prompt = None
    starting_version = None
    try:
        prompt_obj = mlflow.genai.load_prompt(f"prompts:/{prompt_name}@production", allow_missing=True)
        if prompt_obj:
            system_prompt = prompt_obj.template
            starting_version = prompt_obj.version
            print(f"  Loaded: {prompt_name} v{starting_version} ({len(system_prompt)} chars)", flush=True)
    except Exception as e:
        print(f"  Could not load prompt: {e}", flush=True)

    if not system_prompt:
        return {"error": "Cannot load system prompt from MLflow — required for tuning"}

    template, current_section = split_prompt(system_prompt)
    print(f"  Mutable section: {len(current_section)} chars", flush=True)

    # Fetch predictions
    all_games = fetch_predictions()
    if len(all_games) < 10:
        return {"error": f"Not enough resolved predictions ({len(all_games)}). Need at least 10."}

    rng = np.random.default_rng(42)
    rng.shuffle(all_games)
    split = int(len(all_games) * 0.7)
    eval_games = all_games[:split]
    holdout_games = all_games[split:]
    print(f"  Eval set: {len(eval_games)} | Holdout: {len(holdout_games)}", flush=True)

    # Start MLflow run
    mlflow.start_run(run_name=f"tune-bt-{agent_model}")
    mlflow.log_param("model", agent_model)
    mlflow.log_param("max_steps", max_steps)
    mlflow.log_param("eval_batch_size", eval_batch_size)
    mlflow.log_param("accuracy_target", accuracy_target)
    mlflow.log_param("eval_games", len(eval_games))

    # Baseline — use ALL eval games for stable signal
    print("\nComputing baseline accuracy...", flush=True)
    batch = eval_games
    baseline = evaluate_prompt(template, current_section, batch)
    best_accuracy = baseline["accuracy"]
    best_section = current_section
    accuracy_history = [best_accuracy]
    prompt_versions = []

    mlflow.log_metric("accuracy", best_accuracy, step=0)
    mlflow.log_metric("best_accuracy", best_accuracy, step=0)
    print(f"\nBaseline: {baseline['correct']}/{baseline['total']} ({best_accuracy:.1%})", flush=True)

    # Fetch prior run history for cross-run learning
    prior_run_summary = ""
    try:
        exp = client.get_experiment_by_name(experiment_name)
        if exp:
            prior_runs = client.search_runs(
                experiment_ids=[exp.experiment_id],
                order_by=["start_time DESC"],
                max_results=5,
            )
            if prior_runs:
                lines = ["Previous tuning runs (most recent first):"]
                for pr in prior_runs:
                    m = pr.data.metrics
                    lines.append(
                        f"  baseline={m.get('accuracy', '?'):.1%} → best={m.get('final_best_accuracy', '?'):.1%} "
                        f"in {int(m.get('total_steps', 0))} steps"
                    )
                prior_run_summary = "\n".join(lines)
                print(f"\n{prior_run_summary}", flush=True)
    except Exception:
        pass

    # RL loop
    for step_num in range(1, max_steps + 1):
        print(f"\n{'=' * 60}", flush=True)
        print(f"Step {step_num}/{max_steps}", flush=True)

        # Build metrics text for meta-agent
        by_tier = baseline.get("by_tier", {})
        tier_lines = []
        for tier in ("STRONG", "LEAN", "COIN FLIP"):
            c, t = by_tier.get(tier, (0, 0))
            pct = f"{c / t:.1%}" if t else "N/A"
            tier_lines.append(f"  {tier}: {c}/{t} ({pct})")
        metrics_text = (
            f"Overall accuracy: {baseline['correct']}/{baseline['total']} ({baseline['accuracy']:.1%})\n"
            f"Best accuracy so far: {best_accuracy:.1%}\n"
            f"Step: {step_num}/{max_steps}\n"
            f"By confidence tier:\n" + "\n".join(tier_lines)
        )
        if prior_run_summary:
            metrics_text += f"\n\n{prior_run_summary}\nBe conservative — avoid large rewrites that regressed accuracy in prior runs."

        wrong = baseline.get("wrong", [])
        if wrong:
            error_lines = [f"{len(wrong)} wrong prediction(s):"]
            for w in wrong[:10]:
                error_lines.append(f"- {w['away_team']} @ {w['home_team']}: picked {w['picked']} ({w['confidence']}), actual: {w['actual_winner']}")
            error_text = "\n".join(error_lines)
        else:
            error_text = "No wrong predictions."

        # Meta-agent generates improved prompt
        print("  Generating improved prompt...", flush=True)
        new_section = generate_improved_prompt(current_section, metrics_text, error_text)
        print(f"  Generated: {len(new_section)} chars", flush=True)

        # Validate
        valid, issues = validate_section(new_section)
        if not valid:
            print(f"  INVALID: {'; '.join(issues)}", flush=True)
            mlflow.log_metric("accuracy", accuracy_history[-1], step=step_num)
            continue

        # Evaluate
        print("  Evaluating...", flush=True)
        metrics = evaluate_prompt(template, new_section, batch)
        current_acc = metrics["accuracy"]
        prev_acc = accuracy_history[-1]

        reward = current_acc - prev_acc
        if current_acc > best_accuracy:
            reward += 0.1
            best_accuracy = current_acc
            best_section = new_section
            print("  *** NEW BEST ***", flush=True)

        accuracy_history.append(current_acc)
        current_section = new_section
        baseline = metrics

        print(f"  Accuracy: {metrics['correct']}/{metrics['total']} ({current_acc:.1%}) | Best: {best_accuracy:.1%} | Reward: {reward:+.3f}", flush=True)

        mlflow.log_metric("accuracy", current_acc, step=step_num)
        mlflow.log_metric("reward", reward, step=step_num)
        mlflow.log_metric("best_accuracy", best_accuracy, step=step_num)
        for tier in ("STRONG", "LEAN", "COIN FLIP"):
            c, t = metrics.get("by_tier", {}).get(tier, (0, 0))
            mlflow.log_metric(f"accuracy_{tier.lower().replace(' ', '_')}", c / max(t, 1), step=step_num)

        # Register prompt version
        try:
            full_prompt = reconstruct(template, new_section)
            result = mlflow.genai.register_prompt(
                name=prompt_name, template=full_prompt,
                commit_message=f"RL tuning step {step_num}: accuracy={current_acc:.1%}",
                tags={"source": "rl-tuning", "step": str(step_num), "accuracy": f"{current_acc:.3f}"},
            )
            prompt_versions.append({"version": result.version, "accuracy": current_acc})
            print(f"  Registered: v{result.version}", flush=True)
        except Exception as e:
            print(f"  Registration skipped: {e}", flush=True)

        # Check termination
        if current_acc >= accuracy_target:
            print(f"\nTarget accuracy {accuracy_target:.1%} reached!", flush=True)
            break
        if len(accuracy_history) >= 4:
            last_4 = accuracy_history[-4:]
            if all(last_4[i] >= last_4[i + 1] for i in range(3)):
                print("\nPlateau detected (3 consecutive declines).", flush=True)
                break

    # Holdout validation — evaluate best prompt on unseen games before promoting
    baseline_accuracy = accuracy_history[0]
    best_version = None
    promote = False

    if prompt_versions and best_accuracy > baseline_accuracy:
        best_entry = max(prompt_versions, key=lambda v: v["accuracy"])
        best_version = best_entry["version"]

        holdout_batch = holdout_games[:eval_batch_size]
        if holdout_batch:
            print(f"\nHoldout validation ({len(holdout_batch)} games)...", flush=True)
            holdout_result = evaluate_prompt(template, best_section, holdout_batch)
            holdout_acc = holdout_result["accuracy"]
            mlflow.log_metric("holdout_accuracy", holdout_acc)
            print(f"  Holdout: {holdout_result['correct']}/{holdout_result['total']} ({holdout_acc:.1%})", flush=True)

            if holdout_acc >= baseline_accuracy:
                promote = True
                print(f"  Holdout {holdout_acc:.1%} >= baseline {baseline_accuracy:.1%} — validated!", flush=True)
            else:
                print(f"  Holdout {holdout_acc:.1%} < baseline {baseline_accuracy:.1%} — overfitting detected", flush=True)
        else:
            promote = True
    elif prompt_versions:
        best_entry = max(prompt_versions, key=lambda v: v["accuracy"])
        best_version = best_entry["version"]
        print(f"\nNo improvement over baseline {baseline_accuracy:.1%}", flush=True)

    if promote:
        try:
            mlflow.genai.set_prompt_alias(prompt_name, alias="production", version=best_version)
            print(f"Promoted v{best_version} to @production (eval: {best_accuracy:.1%})", flush=True)
        except Exception as e:
            print(f"Could not promote: {e}", flush=True)
    else:
        print(f"Restoring @production to v{starting_version}", flush=True)
        try:
            mlflow.genai.set_prompt_alias(prompt_name, alias="production", version=starting_version)
        except Exception:
            pass

    mlflow.log_metric("final_best_accuracy", best_accuracy)
    mlflow.log_metric("total_steps", len(accuracy_history) - 1)
    mlflow.end_run()

    return {
        "best_accuracy": float(best_accuracy),
        "final_step": len(accuracy_history) - 1,
        "best_version": best_version,
        "accuracy_history": [float(a) for a in accuracy_history],
    }


# =============================================================================
# Step 3: Report Results
# =============================================================================
@component(base_image=BASE_IMAGE, packages_to_install=["pydantic>=2.0.0"])
def report_results_op(metrics: dict, mlflow_tracking_uri: str) -> str:
    """Print prompt tuning scorecard."""
    print("=" * 60)
    print("MLB PROMPT TUNING REPORT")
    print("=" * 60)

    if "error" in metrics:
        print(f"  ERROR: {metrics['error']}")
        return f"Tuning failed: {metrics['error']}"

    print(f"  Best accuracy:  {metrics.get('best_accuracy', 0):.1%}")
    print(f"  Total steps:    {metrics.get('final_step', 0)}")
    print(f"  Best version:   v{metrics.get('best_version', '?')}")

    history = metrics.get("accuracy_history", [])
    if history:
        print(f"  Accuracy trend: {' → '.join(f'{a:.1%}' for a in history)}")

    print(f"\nView in MLflow: {mlflow_tracking_uri}")
    return f"Tuning complete. Best accuracy: {metrics.get('best_accuracy', 0):.1%}"


# =============================================================================
# Pipeline Definition
# =============================================================================
@dsl.pipeline(
    name="MLB Prompt Tuning",
    description="RL-based system prompt optimization for game prediction accuracy"
)
def mlb_prompt_tuning_pipeline(
    mlflow_tracking_uri: str = "https://mlflow.redhat-ods-applications.svc:8443/mlflow",
    mlflow_workspace: str = "mlb-agent",
    mlflow_experiment_name: str = "mlb-prompt-tuning-backtest",
    agent_model: str = "qwen36-27b",
    llm_base_url: str = "https://maas.apps.ocp.cloud.rhai-tmm.dev/prelude-maas/qwen36-27b/v1",
    trino_host: str = "trino.mlb-agent.svc.cluster.local",
    trino_port: int = 8080,
    max_steps: int = 50,
    eval_batch_size: int = 40,
    accuracy_target: float = 0.65,
    llm_secret_name: str = "mlb-agent-maas-key",
    meta_model: str = "claude-sonnet-4-6",
    gcp_adc_secret_name: str = "gcp-adc",
):
    setup = setup_mlflow_op(
        mlflow_tracking_uri=mlflow_tracking_uri,
        mlflow_experiment_name=mlflow_experiment_name,
        mlflow_workspace=mlflow_workspace,
    )
    setup.set_caching_options(False)

    tuning = run_tuning_op(
        mlflow_tracking_uri=mlflow_tracking_uri,
        experiment_name=setup.output,
        agent_model=agent_model,
        llm_base_url=llm_base_url,
        trino_host=trino_host,
        trino_port=trino_port,
        max_steps=max_steps,
        eval_batch_size=eval_batch_size,
        accuracy_target=accuracy_target,
        mlflow_workspace=mlflow_workspace,
        meta_model=meta_model,
    )
    tuning.set_caching_options(False)
    kubernetes.use_secret_as_env(
        tuning,
        secret_name=llm_secret_name,
        secret_key_to_env={"api-key": "OPENAI_API_KEY"},
    )
    kubernetes.use_secret_as_volume(
        tuning,
        secret_name=gcp_adc_secret_name,
        mount_path="/adc",
    )

    report_results_op(
        metrics=tuning.output,
        mlflow_tracking_uri=mlflow_tracking_uri,
    )


# =============================================================================
# Main
# =============================================================================
if __name__ == "__main__":
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description="MLB Prompt Tuning Pipeline")
    parser.add_argument("--compile", action="store_true", help="Compile to YAML")
    parser.add_argument("--output-dir", default="pipelines_gen")
    args = parser.parse_args()

    if args.compile:
        from kfp import compiler

        script_dir = Path(__file__).parent
        output_dir = script_dir / args.output_dir
        output_dir.mkdir(exist_ok=True)

        output_file = output_dir / "mlb-tune-backtest-pipeline.yaml"
        compiler.Compiler().compile(
            pipeline_func=mlb_prompt_tuning_pipeline,
            package_path=str(output_file),
        )
        print(f"Pipeline compiled to: {output_file}")
    else:
        print("Usage: python prompt_tuning/pipeline.py --compile")
