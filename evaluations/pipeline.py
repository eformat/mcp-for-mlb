"""Kubeflow Pipeline for MLB Data Agent Evaluation.

Evaluates the MLB baseball data agent on 7 capability dimensions
using mlflow.genai.evaluate() with Guidelines scorers.

Pipeline steps:
1. setup_mlflow_op: Configure MLflow tracking
2. create_dataset_op: Create evaluation dataset in MLflow
3. generate_variants_op: Generate question variants via SDG Hub
4. run_eval_op: Run evaluation with LLM-as-judge scorers
5. report_results_op: Print scorecard

Usage:
    python evaluations/pipeline.py --compile
"""

from typing import NamedTuple

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

SDG_PACKAGES = COMMON_PACKAGES + [
    "sdg-hub>=0.7.0,<1.0",
    "pandas>=2.0",
    "pyyaml>=6.0",
]

AGENT_PACKAGES = COMMON_PACKAGES + [
    "langchain>=0.3",
    "langchain-openai>=0.3",
    "langchain-core>=0.3",
    "langgraph>=0.4",
    "trino>=0.329",
    "openai>=1.0",
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
    if not experiment_name.endswith("-eval"):
        experiment_name = f"{experiment_name}-eval"

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

    print(f"MLflow: {mlflow_tracking_uri} | Experiment: {experiment_name}")
    return experiment_name


# =============================================================================
# Step 2: Create Dataset
# =============================================================================
@component(base_image=BASE_IMAGE, packages_to_install=COMMON_PACKAGES)
def create_dataset_op(
    mlflow_tracking_uri: str,
    experiment_name: str,
    dataset_name: str,
    mlflow_workspace: str = "",
) -> NamedTuple("DatasetOutput", [("experiment_name", str), ("dataset_id", str)]):
    """Create MLB evaluation dataset in MLflow."""
    import os
    from typing import NamedTuple
    from pathlib import Path
    import mlflow
    from mlflow.genai.datasets import create_dataset

    os.environ["MLFLOW_TRACKING_INSECURE_TLS"] = "true"
    if not os.environ.get("MLFLOW_TRACKING_TOKEN"):
        sa_token_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
        if sa_token_path.exists():
            os.environ["MLFLOW_TRACKING_TOKEN"] = sa_token_path.read_text().strip()

    mlflow.set_tracking_uri(mlflow_tracking_uri)
    if mlflow_workspace:
        mlflow.set_workspace(mlflow_workspace)
        import mlflow.tracking.fluent as _fluent
        client = mlflow.MlflowClient()
        exps = client.search_experiments(filter_string=f"name = '{experiment_name}'")
        if exps:
            _fluent._active_experiment_id = exps[0].experiment_id
    else:
        mlflow.set_experiment(experiment_name)

    # 30 MLB seed questions
    test_cases = [
        # Data retrieval (5)
        {"inputs": {"question": "How many home runs did Barry Bonds hit in 2001?"}, "expectations": {"expected_keywords": ["73", "Bonds"], "question_type": "data_retrieval"}},
        {"inputs": {"question": "Who won the 2024 World Series?"}, "expectations": {"expected_keywords": ["Dodgers"], "question_type": "data_retrieval"}},
        {"inputs": {"question": "What are the current AL East standings?"}, "expectations": {"expected_keywords": ["wins", "losses"], "question_type": "data_retrieval"}},
        {"inputs": {"question": "What was the average fastball velocity in 2018?"}, "expectations": {"expected_keywords": ["mph", "fastball"], "question_type": "data_retrieval"}},
        {"inputs": {"question": "Who won the MVP award in 2025?"}, "expectations": {"expected_keywords": ["MVP"], "question_type": "data_retrieval"}},
        # Cross-dataset reasoning (5)
        {"inputs": {"question": "Compare Babe Ruth and Hank Aaron's career batting statistics"}, "expectations": {"expected_keywords": ["Ruth", "Aaron", "home runs"], "question_type": "cross_dataset"}},
        {"inputs": {"question": "How does weather affect home runs at Wrigley Field?"}, "expectations": {"expected_keywords": ["temperature", "Wrigley"], "question_type": "cross_dataset"}},
        {"inputs": {"question": "Which pitcher had the most strikeouts in 2026 and what was their average fastball velocity?"}, "expectations": {"expected_keywords": ["strikeouts", "velocity"], "question_type": "cross_dataset"}},
        {"inputs": {"question": "How has the average player salary changed from 1985 to 2016?"}, "expectations": {"expected_keywords": ["salary", "1985", "2016"], "question_type": "cross_dataset"}},
        {"inputs": {"question": "Compare postseason batting stats to regular season for 2018"}, "expectations": {"expected_keywords": ["postseason", "regular season"], "question_type": "cross_dataset"}},
        # Scope boundary (5)
        {"inputs": {"question": "What is Mike Trout's WAR this season?"}, "expectations": {"expected_keywords": ["not available", "WAR"], "question_type": "scope_boundary", "forbidden_content": ["WAR is 5", "WAR is 6", "WAR is 7"]}},
        {"inputs": {"question": "Show me pitch-by-pitch data for the 2022 World Series"}, "expectations": {"expected_keywords": ["not available"], "question_type": "scope_boundary", "forbidden_content": ["here are the 2022 pitches"]}},
        {"inputs": {"question": "What were the player salaries in 2025?"}, "expectations": {"expected_keywords": ["2016", "salary"], "question_type": "scope_boundary", "forbidden_content": ["salary in 2025"]}},
        {"inputs": {"question": "What was the weather during yesterday's Yankees game?"}, "expectations": {"expected_keywords": ["weather"], "question_type": "scope_boundary"}},
        {"inputs": {"question": "Show me the box score for today's games"}, "expectations": {"expected_keywords": ["completed"], "question_type": "scope_boundary"}},
        # Causal inference (5)
        {"inputs": {"question": "Does the pitch clock cause fewer strikeouts?"}, "expectations": {"expected_keywords": ["correlation", "cannot"], "question_type": "causal_inference", "forbidden_content": ["pitch clock causes"]}},
        {"inputs": {"question": "Did steroids cause the home run surge in the late 1990s?"}, "expectations": {"expected_keywords": ["cannot", "causal"], "question_type": "causal_inference", "forbidden_content": ["steroids caused", "proven that"]}},
        {"inputs": {"question": "Does higher spin rate lead to more strikeouts?"}, "expectations": {"expected_keywords": ["correlation"], "question_type": "causal_inference", "forbidden_content": ["higher spin rate causes"]}},
        {"inputs": {"question": "Does cold weather cause more pitcher injuries?"}, "expectations": {"expected_keywords": ["cannot", "injury"], "question_type": "causal_inference", "forbidden_content": ["causes injuries"]}},
        {"inputs": {"question": "Does the designated hitter improve team offense?"}, "expectations": {"expected_keywords": ["designated hitter"], "question_type": "causal_inference", "forbidden_content": ["DH definitively improves"]}},
        # Era context (5)
        {"inputs": {"question": "Who is the greatest home run hitter of all time?"}, "expectations": {"expected_keywords": ["era", "context"], "question_type": "era_context", "forbidden_content": ["definitively", "objectively the greatest"]}},
        {"inputs": {"question": "Compare pitching ERAs across the 1960s and 2020s"}, "expectations": {"expected_keywords": ["mound", "1969"], "question_type": "era_context"}},
        {"inputs": {"question": "How do Negro League statistics compare to MLB?"}, "expectations": {"expected_keywords": ["incomplete", "Negro League"], "question_type": "era_context"}},
        {"inputs": {"question": "Has the strikeout rate increased over time?"}, "expectations": {"expected_keywords": ["strikeout"], "question_type": "era_context"}},
        {"inputs": {"question": "Compare the 1927 Yankees to the 2023 Rangers"}, "expectations": {"expected_keywords": ["era", "context"], "question_type": "era_context"}},
        # Terminology / geographic (5)
        {"inputs": {"question": "What's the food poisoning rate in baseball?"}, "expectations": {"expected_keywords": ["baseball"], "question_type": "terminology"}},
        {"inputs": {"question": "Show me the slugging percentage leaders for 2024"}, "expectations": {"expected_keywords": ["slugging", "SLG"], "question_type": "terminology"}},
        {"inputs": {"question": "How did the Bronx Bombers do last year?"}, "expectations": {"expected_keywords": ["Yankees"], "question_type": "terminology"}},
        {"inputs": {"question": "What's the WHIP for Clayton Kershaw in 2018?"}, "expectations": {"expected_keywords": ["WHIP", "Kershaw"], "question_type": "terminology"}},
        {"inputs": {"question": "Show home run stats for all California teams"}, "expectations": {"expected_keywords": ["home runs"], "question_type": "geographic"}},
    ]

    from datetime import datetime
    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dataset_name = f"{dataset_name}_{run_ts}"

    dataset = create_dataset(
        name=run_dataset_name,
        tags={"stage": "validation", "seeds": str(len(test_cases)), "agent": "mlb-data-agent"},
    )
    dataset = dataset.merge_records(test_cases)
    print(f"Dataset: {run_dataset_name} | {len(test_cases)} seeds | ID: {dataset.dataset_id}", flush=True)

    DatasetOutput = NamedTuple("DatasetOutput", [("experiment_name", str), ("dataset_id", str)])
    return DatasetOutput(experiment_name=experiment_name, dataset_id=dataset.dataset_id)


# =============================================================================
# Step 2b: Generate Question Variants via SDG Hub
# =============================================================================
@component(base_image=BASE_IMAGE, packages_to_install=SDG_PACKAGES)
def generate_variants_op(
    mlflow_tracking_uri: str,
    experiment_name: str,
    dataset_id: str,
    llm_base_url: str,
    gen_model: str,
    variants_per_seed: int = 3,
    mlflow_workspace: str = "",
) -> NamedTuple("GenOutput", [("experiment_name", str), ("dataset_id", str)]):
    """Generate question variants from seeds using SDG Hub."""
    import os
    import sys
    import json
    import tempfile
    from typing import NamedTuple
    from pathlib import Path

    os.environ["PYTHONUNBUFFERED"] = "1"
    sys.stdout.reconfigure(line_buffering=True)
    os.environ["MLFLOW_TRACKING_INSECURE_TLS"] = "true"

    if not os.environ.get("MLFLOW_TRACKING_TOKEN"):
        sa_token_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
        if sa_token_path.exists():
            os.environ["MLFLOW_TRACKING_TOKEN"] = sa_token_path.read_text().strip()

    import mlflow
    from mlflow.genai.datasets import get_dataset

    mlflow.set_tracking_uri(mlflow_tracking_uri)
    if mlflow_workspace:
        mlflow.set_workspace(mlflow_workspace)
        import mlflow.tracking.fluent as _fluent
        client = mlflow.MlflowClient()
        exps = client.search_experiments(filter_string=f"name = '{experiment_name}'")
        if exps:
            _fluent._active_experiment_id = exps[0].experiment_id
    else:
        mlflow.set_experiment(experiment_name)

    dataset = get_dataset(dataset_id=dataset_id)
    df = dataset.to_df()
    seed_count = len(df)
    print(f"Dataset: {dataset.name} | {seed_count} seeds", flush=True)

    import pandas as pd
    seeds = []
    for _, row in df.iterrows():
        inputs = row.get("inputs", {})
        expectations = row.get("expectations", {})
        if isinstance(inputs, str):
            inputs = json.loads(inputs)
        if isinstance(expectations, str):
            expectations = json.loads(expectations)
        seeds.append({
            "question": inputs.get("question", ""),
            "question_type": expectations.get("question_type", "data_retrieval"),
            "expected_keywords": json.dumps(expectations.get("expected_keywords", [])),
        })
    seed_df = pd.DataFrame(seeds)

    gen_base = llm_base_url.rstrip("/")
    if not gen_base.endswith("/v1"):
        gen_base = gen_base + "/v1"
    api_key = os.environ.get("OPENAI_API_KEY", "")

    all_variants = []

    # Direct HTTP generation (simpler, no SDG Hub dependency issues)
    import httpx
    url = f"{gen_base}/chat/completions"
    for seed in seeds:
        prompt = (
            f"Generate {variants_per_seed} variant evaluation questions for an MLB baseball data agent.\n\n"
            f"Seed: {seed['question']}\nType: {seed['question_type']}\n\n"
            f"Change the player, team, year, or stat while keeping the same question type.\n"
            f"Available data: Lahman Database (1871-2025), Statcast pitches (2015-2019, 2024-2025), "
            f"weather (1872-2019), live 2026 season, standings.\n\n"
            f'Respond with a JSON object containing a "variants" array. '
            f'Each variant needs: "question", "question_type", "expected_keywords" (array). No markdown.'
        )
        try:
            r = httpx.post(url, json={
                "model": gen_model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.7, "max_tokens": 1024,
            }, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, timeout=30)
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            clean = content.strip()
            if clean.startswith("```"):
                lines = clean.split("\n")
                lines = [l for l in lines if not l.strip().startswith("```")]
                clean = "\n".join(lines)
            parsed = json.loads(clean)
            for v in parsed.get("variants", [])[:variants_per_seed]:
                q = v.get("question", "")
                if q:
                    all_variants.append({
                        "inputs": {"question": q},
                        "expectations": {
                            "expected_keywords": v.get("expected_keywords", []),
                            "question_type": v.get("question_type", seed["question_type"]),
                        },
                    })
        except Exception as ex:
            print(f"  Error for '{seed['question'][:40]}': {ex}", flush=True)
    print(f"Generated {len(all_variants)} variant questions", flush=True)

    if all_variants:
        dataset = dataset.merge_records(all_variants)
        total = len(dataset.to_df())
        print(f"Dataset: {seed_count} seeds + {len(all_variants)} variants = {total} total", flush=True)
    else:
        print("No variants generated, using seeds only", flush=True)

    GenOutput = NamedTuple("GenOutput", [("experiment_name", str), ("dataset_id", str)])
    return GenOutput(experiment_name=experiment_name, dataset_id=dataset.dataset_id)


# =============================================================================
# Step 3: Run Evaluation
# =============================================================================
@component(base_image=BASE_IMAGE, packages_to_install=AGENT_PACKAGES)
def run_eval_op(
    mlflow_tracking_uri: str,
    experiment_name: str,
    dataset_id: str,
    llm_base_url: str,
    agent_model: str,
    judge_model: str,
    trino_host: str = "trino",
    trino_port: int = 8080,
    mlflow_workspace: str = "",
) -> dict:
    """Run MLB agent evaluation with 7 capability dimension scorers."""
    import os
    import re
    import sys
    import json
    import warnings
    from pathlib import Path

    os.environ["PYTHONUNBUFFERED"] = "1"
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)

    warnings.filterwarnings("ignore")
    os.environ["MLFLOW_TRACKING_INSECURE_TLS"] = "true"
    os.environ["MLFLOW_GENAI_EVAL_MAX_WORKERS"] = "2"
    os.environ["MLFLOW_GENAI_EVAL_MAX_SCORER_WORKERS"] = "2"
    os.environ["MLFLOW_GENAI_EVAL_MAX_RETRIES"] = "3"
    os.environ["MLFLOW_GENAI_EVAL_SKIP_TRACE_VALIDATION"] = "True"
    os.environ["TRINO_QUERY_HOST"] = trino_host
    os.environ["TRINO_QUERY_PORT"] = str(trino_port)
    judge_base = llm_base_url.rstrip("/")
    judge_chat_url = judge_base + "/chat/completions"

    if not os.environ.get("MLFLOW_TRACKING_TOKEN"):
        sa_token_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
        if sa_token_path.exists():
            os.environ["MLFLOW_TRACKING_TOKEN"] = sa_token_path.read_text().strip()

    try:
        import nest_asyncio
        nest_asyncio.apply()
    except ImportError:
        pass

    import mlflow
    from mlflow.genai.scorers import scorer
    from mlflow.genai.datasets import get_dataset

    mlflow.set_tracking_uri(mlflow_tracking_uri)
    if mlflow_workspace:
        mlflow.set_workspace(mlflow_workspace)
        import mlflow.tracking.fluent as _fluent
        client = mlflow.MlflowClient()
        exps = client.search_experiments(filter_string=f"name = '{experiment_name}'")
        if exps:
            _fluent._active_experiment_id = exps[0].experiment_id
    else:
        mlflow.set_experiment(experiment_name)

    # ── Deterministic scorers ──
    @scorer
    def contains_expected(inputs: dict, outputs: str, expectations: dict) -> bool:
        keywords = expectations.get("expected_keywords", [])
        if not keywords:
            return True
        out = str(outputs).lower()
        return any(kw.lower() in out for kw in keywords)

    @scorer
    def no_forbidden_content(inputs: dict, outputs: str, expectations: dict) -> bool:
        forbidden = expectations.get("forbidden_content", [])
        if not forbidden:
            return True
        out = str(outputs).lower()
        return not any(f.lower() in out for f in forbidden)

    @scorer
    def confidence_card_present(outputs: str) -> bool:
        return "data confidence" in str(outputs).lower()

    @scorer
    def response_adequate_length(outputs: str) -> float:
        return 1.0 if len(str(outputs)) >= 100 else 0.5

    # ── LLM-as-Judge scorers ──
    import httpx
    api_key = os.environ.get("OPENAI_API_KEY", "")
    print(f"Judge: {judge_model} via {judge_chat_url}", flush=True)

    def _call_judge(question: str, response: str, criterion: str) -> bool:
        prompt = (
            f"Question: {question}\nResponse: {response}\n\n"
            f"{criterion}\n\nReply with only YES or NO."
        )
        try:
            r = httpx.post(judge_chat_url, json={
                "model": judge_model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 10, "temperature": 0,
            }, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, timeout=30)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip().upper().startswith("YES")
        except Exception as e:
            print(f"  Judge error: {e}", flush=True)
            return False

    @scorer
    def cross_dataset_reasoning(inputs: dict, outputs: str) -> bool:
        return _call_judge(str(inputs), str(outputs),
            "Does the response state which dataset or table was used and why?")

    @scorer
    def methodology_awareness(inputs: dict, outputs: str) -> bool:
        return _call_judge(str(inputs), str(outputs),
            "Does the response describe era context, data collection methodology, "
            "or note relevant rule changes when applicable?")

    @scorer
    def scope_adherence(inputs: dict, outputs: str) -> bool:
        return _call_judge(str(inputs), str(outputs),
            "Does the response stay within the scope of available data "
            "and avoid fabricating statistics not in the database?")

    @scorer
    def causal_inference_boundaries(inputs: dict, outputs: str) -> bool:
        return _call_judge(str(inputs), str(outputs),
            "Does the response correctly avoid causal claims from "
            "statistical/observational data?")

    @scorer
    def geographic_resolution(inputs: dict, outputs: str) -> bool:
        return _call_judge(str(inputs), str(outputs),
            "Does the response correctly handle geographic scope "
            "(team/park/city level) and identify relevant teams when asked?")

    @scorer
    def terminology_fluency(inputs: dict, outputs: str) -> bool:
        return _call_judge(str(inputs), str(outputs),
            "Are baseball terms correctly mapped to database columns "
            "(e.g., AVG=H/AB, WHIP=(BB+H)*3/IPouts, team nicknames to IDs)?")

    @scorer
    def confidence_calibration(inputs: dict, outputs: str) -> bool:
        return _call_judge(str(inputs), str(outputs),
            "Does the response include a Data Confidence level "
            "(HIGH/MODERATE/LOW) that reflects what data was actually retrieved?")

    capability_scorers = [
        cross_dataset_reasoning, methodology_awareness, scope_adherence,
        causal_inference_boundaries, geographic_resolution,
        terminology_fluency, confidence_calibration,
    ]

    all_scorers = [
        contains_expected, no_forbidden_content,
        confidence_card_present, response_adequate_length,
    ] + capability_scorers

    # ── Predictor ──
    from langchain_openai import ChatOpenAI
    from langgraph.prebuilt import create_react_agent
    from langchain_core.messages import HumanMessage
    from langchain_core.tools import tool
    from trino.dbapi import connect as trino_connect

    BLOCKED_SQL = re.compile(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE)\b", re.IGNORECASE)

    @tool
    def query_trino(sql: str) -> str:
        """Execute a read-only SQL query against the MLB Iceberg lakehouse.
        Schema: lakehouse.mlb. Tables: batting, pitching, fielding, people, teams, parks,
        series_post, awards_players, hall_of_fame, salaries, home_games, weather_stations,
        weather_daily, pitch_pitches, pitch_atbats, pitch_games, statcast_pitches,
        live_games, live_boxscore_batting, live_boxscore_pitching, live_plays,
        live_pitches, live_standings. Only SELECT allowed."""
        if BLOCKED_SQL.search(sql):
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
            {"name": "pitch_pitches (Statcast)", "years": "2015-2019"},
            {"name": "statcast_pitches", "years": "2024-2025 postseason"},
            {"name": "live_* (2026 season)", "years": "2026"},
            {"name": "weather_daily", "years": "~1872-2019"},
            {"name": "salaries", "years": "1985-2016"},
        ]})

    @tool
    def get_methodology(dataset_name: str) -> str:
        """Get methodology for a specific MLB dataset."""
        return json.dumps({"data_type": "Official MLB records", "collection": "Box scores and Statcast tracking"})

    agent_base_url = llm_base_url.replace(f"/{judge_model}", f"/{agent_model}")
    if not agent_base_url.endswith("/v1"):
        agent_base_url = agent_base_url + "/v1"
    print(f"Agent: {agent_model} via {agent_base_url}", flush=True)

    agent_llm = ChatOpenAI(
        model=agent_model, base_url=agent_base_url,
        api_key=os.environ.get("OPENAI_API_KEY", "x"),
        temperature=0.3, max_tokens=4096, streaming=False,
        model_kwargs={"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}},
    )

    prompt_name = "mlb-agent.system"
    system_prompt = None
    prompt_version = "unknown"
    try:
        prompt_obj = mlflow.genai.load_prompt(
            f"prompts:/{prompt_name}@production", allow_missing=True,
        )
        if prompt_obj:
            system_prompt = prompt_obj.template
            prompt_version = str(prompt_obj.version)
            print(f"Loaded prompt: {prompt_name} v{prompt_version} ({len(system_prompt)} chars)", flush=True)
            mlflow.log_param("prompt_name", prompt_name)
            mlflow.log_param("prompt_version", prompt_version)
    except Exception as e:
        print(f"Could not load prompt from MLflow: {e}", flush=True)

    if not system_prompt:
        print("Using fallback prompt", flush=True)
        system_prompt = (
            "You are a Major League Baseball data agent. "
            "Use query_trino to execute SQL against the MLB Iceberg lakehouse (lakehouse.mlb). "
            "Always include Data Confidence (HIGH/MODERATE/LOW) and Data Freshness."
        )

    agent = create_react_agent(
        model=agent_llm,
        tools=[query_trino, describe_datasets, get_methodology],
        prompt=system_prompt,
    )

    _q_count = [0]

    def predict_fn(question: str) -> str:
        _q_count[0] += 1
        print(f"[predict {_q_count[0]}] Q: {question[:80]}...", flush=True)
        try:
            with mlflow.start_span(name="mlb_agent_eval") as span:
                span.set_inputs({"question": question[:200]})
                mlflow.genai.load_prompt(
                    f"prompts:/{prompt_name}@production",
                    allow_missing=True, cache_ttl_seconds=300,
                )
                result = agent.invoke({"messages": [HumanMessage(content=question)]})
                for m in reversed(result.get("messages", [])):
                    if hasattr(m, "type") and m.type == "ai" and not getattr(m, "tool_calls", None):
                        answer = m.content or ""
                        span.set_outputs({"answer_length": len(answer)})
                        print(f"[predict {_q_count[0]}] A: {len(answer)} chars", flush=True)
                        return answer
            return "No response"
        except Exception as e:
            print(f"[predict {_q_count[0]}] Error: {e}", flush=True)
            return f"Error: {e}"

    # ── Run evaluation ──
    dataset = get_dataset(dataset_id=dataset_id)
    print(f"Dataset: {dataset.name} | Records: {len(dataset.to_df())}", flush=True)
    print(f"Scorers: {len(all_scorers)} (4 deterministic + {len(capability_scorers)} LLM judges)", flush=True)

    print("Starting mlflow.genai.evaluate()...", flush=True)
    result = mlflow.genai.evaluate(
        data=dataset,
        predict_fn=predict_fn,
        scorers=all_scorers,
    )
    print("Evaluation complete.", flush=True)

    metrics = {}
    if hasattr(result, "metrics") and result.metrics:
        for k, v in result.metrics.items():
            metrics[k] = round(v, 4) if isinstance(v, float) else v

    print(f"\nResults: {metrics}")
    return metrics


# =============================================================================
# Step 4: Report Results
# =============================================================================
@component(base_image=BASE_IMAGE, packages_to_install=["pydantic>=2.0.0"])
def report_results_op(metrics: dict, mlflow_tracking_uri: str) -> str:
    """Print evaluation scorecard."""
    print("=" * 60)
    print("MLB DATA AGENT EVALUATION REPORT")
    print("=" * 60)
    for k, v in sorted(metrics.items()):
        if isinstance(v, float):
            print(f"  {k}: {v:.2%}")
        else:
            print(f"  {k}: {v}")
    print(f"\nView in MLflow: {mlflow_tracking_uri}")
    return f"Evaluation complete. {len(metrics)} metrics. View at {mlflow_tracking_uri}"


# =============================================================================
# Pipeline Definition
# =============================================================================
@dsl.pipeline(
    name="MLB Data Agent Evaluation",
    description="Evaluate MLB agent on 7 capability dimensions using LLM-as-judge"
)
def mlb_eval_pipeline(
    mlflow_tracking_uri: str = "https://mlflow.redhat-ods-applications.svc:8443/mlflow",
    mlflow_workspace: str = "mlb-agent",
    mlflow_experiment_name: str = "mlb-data-agent",
    dataset_name: str = "mlb_data_eval",
    llm_base_url: str = "http://maas.apps.ocp.cloud.rhai-tmm.dev/prelude-maas/gemma4/v1",
    agent_model: str = "qwen36-27b",
    judge_model: str = "gemma4",
    trino_host: str = "trino.mlb-agent.svc.cluster.local",
    trino_port: int = 8080,
    llm_secret_name: str = "mlb-agent-maas-key",
):
    setup = setup_mlflow_op(
        mlflow_tracking_uri=mlflow_tracking_uri,
        mlflow_experiment_name=mlflow_experiment_name,
        mlflow_workspace=mlflow_workspace,
    )
    setup.set_caching_options(False)

    dataset = create_dataset_op(
        mlflow_tracking_uri=mlflow_tracking_uri,
        experiment_name=setup.output,
        dataset_name=dataset_name,
        mlflow_workspace=mlflow_workspace,
    )
    dataset.set_caching_options(False)

    sdg_task = generate_variants_op(
        mlflow_tracking_uri=mlflow_tracking_uri,
        experiment_name=dataset.outputs["experiment_name"],
        dataset_id=dataset.outputs["dataset_id"],
        llm_base_url=llm_base_url,
        gen_model=judge_model,
        variants_per_seed=3,
        mlflow_workspace=mlflow_workspace,
    )
    sdg_task.set_caching_options(False)
    kubernetes.use_secret_as_env(
        sdg_task,
        secret_name=llm_secret_name,
        secret_key_to_env={"api-key": "OPENAI_API_KEY"},
    )

    eval_task = run_eval_op(
        mlflow_tracking_uri=mlflow_tracking_uri,
        experiment_name=sdg_task.outputs["experiment_name"],
        dataset_id=sdg_task.outputs["dataset_id"],
        llm_base_url=llm_base_url,
        agent_model=agent_model,
        judge_model=judge_model,
        trino_host=trino_host,
        trino_port=trino_port,
        mlflow_workspace=mlflow_workspace,
    )
    eval_task.set_caching_options(False)
    kubernetes.use_secret_as_env(
        eval_task,
        secret_name=llm_secret_name,
        secret_key_to_env={"api-key": "OPENAI_API_KEY"},
    )

    report_results_op(
        metrics=eval_task.output,
        mlflow_tracking_uri=mlflow_tracking_uri,
    )


# =============================================================================
# Main
# =============================================================================
if __name__ == "__main__":
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description="MLB Eval Pipeline")
    parser.add_argument("--compile", action="store_true", help="Compile to YAML")
    parser.add_argument("--output-dir", default="pipelines_gen")
    args = parser.parse_args()

    if args.compile:
        from kfp import compiler

        script_dir = Path(__file__).parent
        output_dir = script_dir / args.output_dir
        output_dir.mkdir(exist_ok=True)

        output_file = output_dir / "mlb-eval-pipeline.yaml"
        compiler.Compiler().compile(
            pipeline_func=mlb_eval_pipeline,
            package_path=str(output_file),
        )
        print(f"Pipeline compiled to: {output_file}")
    else:
        print("Usage: python evaluations/pipeline.py --compile")
