#!/usr/bin/env python3
"""Run RL-based prompt tuning for MLB prediction accuracy.

Usage:
    python3 prompt_tuning/run_tuning.py --max-steps 50 --batch-size 20
    python3 prompt_tuning/run_tuning.py --dry-run

Environment variables:
    MODEL_NAME              LLM model name (default: qwen36-27b)
    MODEL_ENDPOINT          LLM endpoint URL
    OPENAI_API_KEY          API key for the LLM endpoint
    TRINO_QUERY_HOST        Trino host (default: localhost)
    TRINO_QUERY_PORT        Trino port (default: 8080)
    MLFLOW_TRACKING_URI     MLflow server URL
    MLFLOW_WORKSPACE        MLflow workspace name
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def main():
    parser = argparse.ArgumentParser(
        description="RL prompt tuning for MLB game predictions"
    )
    parser.add_argument(
        "--max-steps", type=int, default=50, help="Maximum tuning steps (default: 50)"
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=20,
        help="Games per evaluation batch (default: 20)",
    )
    parser.add_argument(
        "--accuracy-target",
        type=float,
        default=0.65,
        help="Stop when accuracy reaches this target (default: 0.65)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show baseline metrics without modifying prompts",
    )
    args = parser.parse_args()

    import gymnasium as gym

    import prompt_tuning.env  # noqa: F401 — triggers gymnasium.register()

    from prompt_tuning.meta_agent import PromptOptimizerAgent

    env = gym.make(
        "PromptTuningEnv-v0",
        trino_host=os.environ.get("TRINO_QUERY_HOST", "localhost"),
        trino_port=int(os.environ.get("TRINO_QUERY_PORT", "8080")),
        model_name=os.environ.get("MODEL_NAME", "qwen36-27b"),
        model_endpoint=os.environ.get("MODEL_ENDPOINT", ""),
        mlflow_tracking_uri=os.environ.get("MLFLOW_TRACKING_URI", ""),
        mlflow_workspace=os.environ.get("MLFLOW_WORKSPACE", ""),
        eval_batch_size=args.batch_size,
        max_steps=args.max_steps,
        accuracy_target=args.accuracy_target,
    )

    observation, info = env.reset()
    print(f"Baseline accuracy: {info['baseline_accuracy']:.1%}")
    print(f"Eval games: {info['eval_games']} | Holdout: {info['holdout_games']}")

    if args.dry_run:
        print("\n[DRY RUN] Baseline metrics only — no prompt modifications.")
        print(f"\nMetrics:\n{observation['metrics']}")
        env.close()
        return

    agent = PromptOptimizerAgent()

    for step in range(args.max_steps):
        print(f"\n{'=' * 60}")
        print(f"Step {step + 1}/{args.max_steps}")

        action = agent.generate_prompt(observation)
        print(f"  Generated prompt section: {len(action)} chars")

        observation, reward, terminated, truncated, info = env.step(action)

        print(
            f"  Accuracy: {info.get('accuracy', 0):.1%} | "
            f"Best: {info.get('best_accuracy', 0):.1%} | "
            f"Reward: {reward:+.3f}"
        )

        if info.get("error"):
            print(f"  Error: {info['error']}")
            if info.get("issues"):
                for issue in info["issues"]:
                    print(f"    - {issue}")

        if terminated:
            print("\nTerminated: accuracy target reached or plateau detected.")
            break
        if truncated:
            print("\nTruncated: max steps reached.")
            break

    env.unwrapped.promote_best_prompt()
    env.close()

    print(f"\nFinal accuracy history: {env.unwrapped._accuracy_history}")


if __name__ == "__main__":
    main()
