#!/usr/bin/env python3
"""Run MLB Data Agent evaluation locally.

Usage:
    # Simple mode (deterministic scorers only, no LLM judge)
    python evaluations/run_eval.py --mode simple

    # Full mode (deterministic + 7 LLM judge dimensions)
    python evaluations/run_eval.py --mode llm-judge

    # Save dataset to MLflow
    python evaluations/run_eval.py --save-dataset

Environment variables:
    MLFLOW_TRACKING_URI     MLflow server URL
    MLFLOW_WORKSPACE        MLflow workspace (for RHOAI)
    OPENAI_API_KEY          MaaS API key
    MODEL_NAME              Agent model (default: qwen36-27b)
    MODEL_ENDPOINT          Agent model endpoint
    JUDGE_MODEL             Judge model (default: gemma4)
    TRINO_QUERY_HOST        Trino host (default: localhost)
    TRINO_QUERY_PORT        Trino port (default: 8090)
"""

import argparse
import os
import sys
import warnings

warnings.filterwarnings("ignore")


def main():
    parser = argparse.ArgumentParser(description="MLB Data Agent Evaluation")
    parser.add_argument("--mode", choices=["simple", "llm-judge"], default="llm-judge")
    parser.add_argument("--save-dataset", action="store_true")
    parser.add_argument("--dataset-name", default="mlb_data_eval")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    os.environ.setdefault("MLFLOW_TRACKING_INSECURE_TLS", "true")

    import mlflow

    mlflow_uri = os.environ.get("MLFLOW_TRACKING_URI", "")
    if not mlflow_uri:
        print("ERROR: Set MLFLOW_TRACKING_URI")
        sys.exit(1)

    mlflow.set_tracking_uri(mlflow_uri)
    workspace = os.environ.get("MLFLOW_WORKSPACE", "")
    if workspace:
        mlflow.set_workspace(workspace)

    experiment_name = os.environ.get("MLFLOW_EXPERIMENT_NAME", "mlb-data-agent-eval")
    if workspace:
        import mlflow.tracking.fluent as _fluent
        client = mlflow.MlflowClient()
        exps = client.search_experiments(filter_string=f"name = '{experiment_name}'")
        if exps:
            _fluent._active_experiment_id = exps[0].experiment_id
        else:
            _fluent._active_experiment_id = client.create_experiment(experiment_name)
    else:
        mlflow.set_experiment(experiment_name)

    print(f"MLflow: {mlflow_uri} | Experiment: {experiment_name}")

    from datasets import MLB_EVAL_DATASET
    from mlflow.genai.datasets import create_dataset

    dataset = create_dataset(
        name=args.dataset_name,
        tags={"stage": "validation", "agent": "mlb-data-agent"},
    )
    dataset = dataset.merge_records(MLB_EVAL_DATASET)
    print(f"Dataset: {dataset.dataset_id} | Records: {len(MLB_EVAL_DATASET)}")

    if args.save_dataset:
        print(f"Dataset saved to MLflow: {args.dataset_name}")
        return

    from predictors import create_predict_fn
    predict_fn = create_predict_fn()

    if args.mode == "simple":
        from scorers import get_all_scorers
        scorers = get_all_scorers()
        print(f"Mode: simple | Scorers: {len(scorers)} (deterministic)")
    else:
        judge_model = os.environ.get("JUDGE_MODEL", "gemma4")
        judge_endpoint = os.environ.get(
            "JUDGE_ENDPOINT",
            "http://maas.apps.ocp.cloud.rhai-tmm.dev/prelude-maas/gemma4/v1",
        )
        os.environ["OPENAI_API_BASE"] = judge_endpoint
        os.environ["OPENAI_BASE_URL"] = judge_endpoint

        from scorers import get_all_scorers
        scorers = get_all_scorers(judge_model=f"openai:/{judge_model}")
        print(f"Mode: llm-judge | Scorers: {len(scorers)} | Judge: {judge_model}")

    print("\nRunning evaluation...")
    result = mlflow.genai.evaluate(
        data=dataset,
        predict_fn=predict_fn,
        scorers=scorers,
    )

    print("\n" + "=" * 60)
    print("MLB DATA AGENT EVALUATION RESULTS")
    print("=" * 60)
    if hasattr(result, "metrics") and result.metrics:
        for k, v in sorted(result.metrics.items()):
            if isinstance(v, float):
                print(f"  {k}: {v:.2%}")
            else:
                print(f"  {k}: {v}")

    print(f"\nView in MLflow: {mlflow_uri}")


if __name__ == "__main__":
    main()
