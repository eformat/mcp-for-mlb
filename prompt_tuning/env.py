"""Gymnasium environment for RL-based prompt tuning."""

import json
import os

import gymnasium as gym
import numpy as np
from gymnasium import spaces

from prompt_tuning.prompt_sections import (
    load_and_split_prompt,
    reconstruct_prompt,
    validate_section,
)
from prompt_tuning.replay import (
    build_agent,
    fetch_resolved_predictions,
    format_prediction_question,
    normalize_team,
    parse_agent_predictions,
    run_agent_prediction,
    score_batch,
)


class PromptTuningEnv(gym.Env):
    """RL environment that tunes the Game Prediction Framework prompt section.

    Observation: current prompt section + accuracy metrics + error analysis.
    Action: a replacement prompt section (text).
    Reward: accuracy delta, with bonus for beating the best so far.
    """

    metadata = {"render_modes": ["human", "ansi"]}

    def __init__(
        self,
        trino_host: str = "localhost",
        trino_port: int = 8080,
        model_name: str = "",
        model_endpoint: str = "",
        mlflow_tracking_uri: str = "",
        mlflow_workspace: str = "",
        eval_batch_size: int = 40,
        max_steps: int = 100,
        accuracy_target: float = 0.65,
        render_mode: str | None = None,
    ):
        super().__init__()

        self.observation_space = spaces.Dict({
            "prompt_section": spaces.Text(min_length=100, max_length=10_000),
            "metrics": spaces.Text(min_length=10, max_length=5_000),
            "error_analysis": spaces.Text(min_length=0, max_length=5_000),
        })
        self.action_space = spaces.Text(min_length=100, max_length=10_000)

        self._trino_host = trino_host
        self._trino_port = trino_port
        self._model_name = model_name or os.environ.get("MODEL_NAME", "qwen38-27b")
        self._model_endpoint = model_endpoint or os.environ.get("MODEL_ENDPOINT", "")
        self._mlflow_tracking_uri = mlflow_tracking_uri
        self._mlflow_workspace = mlflow_workspace
        self._eval_batch_size = eval_batch_size
        self._max_steps = max_steps
        self._accuracy_target = accuracy_target
        self.render_mode = render_mode

        self._step_count = 0
        self._template = ""
        self._current_section = ""
        self._best_accuracy = 0.0
        self._best_section = ""
        self._accuracy_history: list[float] = []
        self._last_metrics: dict = {}
        self._eval_games: list[dict] = []
        self._holdout_games: list[dict] = []
        self._prompt_versions: list[dict] = []

    def reset(self, *, seed=None, options=None):
        super().reset(seed=seed)
        self._step_count = 0
        self._accuracy_history = []
        self._prompt_versions = []

        print("Loading system prompt...", flush=True)
        self._template, self._current_section = load_and_split_prompt()
        print(f"  Mutable section: {len(self._current_section)} chars", flush=True)

        print("Fetching resolved predictions from Trino...", flush=True)
        all_games = fetch_resolved_predictions(self._trino_host, self._trino_port)
        print(f"  Found {len(all_games)} resolved predictions", flush=True)

        rng = np.random.default_rng(seed)
        rng.shuffle(all_games)
        split = int(len(all_games) * 0.7)
        self._eval_games = all_games[:split]
        self._holdout_games = all_games[split:]
        print(f"  Eval set: {len(self._eval_games)} | Holdout: {len(self._holdout_games)}", flush=True)

        self._start_mlflow_run()

        print("Computing baseline accuracy...", flush=True)
        baseline = self._evaluate_prompt(self._current_section)
        self._best_accuracy = baseline["accuracy"]
        self._best_section = self._current_section
        self._accuracy_history.append(self._best_accuracy)
        self._last_metrics = baseline
        print(
            f"  Baseline: {baseline['correct']}/{baseline['total']} "
            f"({self._best_accuracy:.1%})",
            flush=True,
        )

        self._log_step_metrics(0, self._best_accuracy, 0.0)

        observation = self._build_observation(baseline, "Baseline — no prior errors.")
        info = {
            "baseline_accuracy": self._best_accuracy,
            "eval_games": len(self._eval_games),
            "holdout_games": len(self._holdout_games),
        }
        return observation, info

    def step(self, action: str):
        self._step_count += 1
        print(f"\n--- Step {self._step_count}/{self._max_steps} ---", flush=True)

        valid, issues = validate_section(action)
        if not valid:
            print(f"  INVALID prompt: {'; '.join(issues)}", flush=True)
            obs = self._build_observation(
                self._last_metrics,
                f"Invalid prompt: {'; '.join(issues)}",
            )
            return obs, -0.1, False, self._step_count >= self._max_steps, {
                "step": self._step_count,
                "error": "invalid_prompt",
                "issues": issues,
            }

        print(f"  Evaluating prompt ({len(action)} chars)...", flush=True)
        metrics = self._evaluate_prompt(action)
        current_acc = metrics["accuracy"]
        prev_acc = self._accuracy_history[-1]

        reward = current_acc - prev_acc
        if current_acc > self._best_accuracy:
            reward += 0.1
            self._best_accuracy = current_acc
            self._best_section = action
            print(f"  NEW BEST accuracy!", flush=True)

        self._accuracy_history.append(current_acc)
        self._current_section = action
        self._last_metrics = metrics

        print(
            f"  Accuracy: {metrics['correct']}/{metrics['total']} ({current_acc:.1%}) | "
            f"Best: {self._best_accuracy:.1%} | Reward: {reward:+.3f}",
            flush=True,
        )

        self._log_step_metrics(self._step_count, current_acc, reward)
        self._register_version(action, metrics)

        terminated = current_acc >= self._accuracy_target
        if terminated:
            print(f"  Target accuracy {self._accuracy_target:.1%} reached!", flush=True)
        if not terminated and len(self._accuracy_history) >= 4:
            last_4 = self._accuracy_history[-4:]
            if all(last_4[i] >= last_4[i + 1] for i in range(3)):
                terminated = True
                print("  Plateau detected (3 consecutive declines).", flush=True)

        truncated = self._step_count >= self._max_steps

        error_analysis = self._format_errors(metrics)
        observation = self._build_observation(metrics, error_analysis)

        info = {
            "step": self._step_count,
            "accuracy": current_acc,
            "best_accuracy": self._best_accuracy,
            "reward": reward,
        }

        return observation, reward, terminated, truncated, info

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _evaluate_prompt(self, section: str) -> dict:
        """Replay a batch of historical games and compute accuracy."""
        full_prompt = reconstruct_prompt(self._template, section)
        agent = build_agent(
            system_prompt=full_prompt,
            model_name=self._model_name,
            model_endpoint=self._model_endpoint,
            trino_host=self._trino_host,
            trino_port=self._trino_port,
        )

        batch = self._eval_games[: self._eval_batch_size]
        batch_size = 3
        total_batches = (len(batch) + batch_size - 1) // batch_size
        all_parsed = []
        for i in range(0, len(batch), batch_size):
            chunk = batch[i : i + batch_size]
            batch_num = i // batch_size + 1
            games_str = ", ".join(
                f"{g['away_team']} @ {g['home_team']}" for g in chunk
            )
            print(
                f"  Batch {batch_num}/{total_batches}: {games_str}",
                flush=True,
            )
            question = format_prediction_question(chunk)
            try:
                response = run_agent_prediction(agent, question)
                parsed = parse_agent_predictions(response)
                all_parsed.extend(parsed)
                print(f"    Parsed {len(parsed)} pick(s)", flush=True)
            except Exception as e:
                print(f"    ERROR: {e}", flush=True)

        result = score_batch(all_parsed, batch)
        print(
            f"  Result: {result['correct']}/{result['total']} ({result['accuracy']:.1%})",
            flush=True,
        )
        return result

    def _build_observation(self, metrics: dict, error_analysis: str) -> dict:
        by_tier = metrics.get("by_tier", {})
        tier_lines = []
        for tier in ("STRONG", "LEAN", "COIN FLIP"):
            c, t = by_tier.get(tier, (0, 0))
            pct = f"{c / t:.1%}" if t else "N/A"
            tier_lines.append(f"  {tier}: {c}/{t} ({pct})")

        metrics_text = (
            f"Overall accuracy: {metrics.get('correct', 0)}/{metrics.get('total', 0)} "
            f"({metrics.get('accuracy', 0):.1%})\n"
            f"Best accuracy so far: {self._best_accuracy:.1%}\n"
            f"Step: {self._step_count}/{self._max_steps}\n"
            f"Accuracy history: {[f'{a:.1%}' for a in self._accuracy_history]}\n"
            f"By confidence tier:\n" + "\n".join(tier_lines)
        )

        return {
            "prompt_section": self._current_section,
            "metrics": metrics_text,
            "error_analysis": error_analysis,
        }

    def _format_errors(self, metrics: dict) -> str:
        wrong = metrics.get("wrong", [])
        if not wrong:
            return "No wrong predictions in this batch."

        lines = [f"{len(wrong)} wrong prediction(s):\n"]
        for w in wrong[:10]:
            lines.append(
                f"- {w['away_team']} @ {w['home_team']}: "
                f"picked {w['picked']} ({w['confidence']}), "
                f"actual winner: {w['actual_winner']}"
            )
            if w.get("reasoning"):
                lines.append(f"  Reasoning: {w['reasoning'][:150]}")
        if len(wrong) > 10:
            lines.append(f"  ... and {len(wrong) - 10} more")
        return "\n".join(lines)

    def _start_mlflow_run(self):
        """Start an MLflow run to track the tuning session."""
        try:
            import mlflow

            mlflow.set_experiment("mlb-prompt-tuning")
            mlflow.start_run(run_name=f"tune-{self._model_name}")
            mlflow.log_param("model_name", self._model_name)
            mlflow.log_param("max_steps", self._max_steps)
            mlflow.log_param("eval_batch_size", self._eval_batch_size)
            mlflow.log_param("accuracy_target", self._accuracy_target)
            mlflow.log_param("eval_games", len(self._eval_games))
            mlflow.log_param("holdout_games", len(self._holdout_games))
        except Exception as e:
            print(f"  MLflow run start skipped: {e}", flush=True)

    def _log_step_metrics(self, step: int, accuracy: float, reward: float):
        """Log per-step metrics to MLflow."""
        try:
            import mlflow

            mlflow.log_metric("accuracy", accuracy, step=step)
            mlflow.log_metric("reward", reward, step=step)
            mlflow.log_metric("best_accuracy", self._best_accuracy, step=step)
        except Exception:
            pass

    def _register_version(self, section: str, metrics: dict):
        """Register the prompt variant in MLflow."""
        try:
            import mlflow

            full_prompt = reconstruct_prompt(self._template, section)
            result = mlflow.genai.register_prompt(
                name="mlb-agent.system",
                template=full_prompt,
                commit_message=(
                    f"RL tuning step {self._step_count}: "
                    f"accuracy={metrics['accuracy']:.1%}"
                ),
                tags={
                    "source": "rl-tuning",
                    "step": str(self._step_count),
                    "accuracy": f"{metrics['accuracy']:.3f}",
                },
            )
            self._prompt_versions.append({
                "version": result.version,
                "accuracy": metrics["accuracy"],
            })
        except Exception as e:
            print(f"  MLflow registration skipped: {e}")

    def promote_best_prompt(self):
        """Set @production alias to the best-performing prompt version.

        Only promotes if the best version beat the baseline accuracy.
        """
        if not self._prompt_versions:
            print("No prompt versions registered — nothing to promote.", flush=True)
            self._end_mlflow_run()
            return

        baseline_accuracy = self._accuracy_history[0] if self._accuracy_history else 0.0
        best = max(self._prompt_versions, key=lambda v: v["accuracy"])

        if best["accuracy"] > baseline_accuracy:
            try:
                import mlflow

                mlflow.genai.set_prompt_alias(
                    "mlb-agent.system",
                    alias="production",
                    version=best["version"],
                )
                print(
                    f"Promoted v{best['version']} to @production "
                    f"(accuracy: {best['accuracy']:.1%}, was {baseline_accuracy:.1%})",
                    flush=True,
                )
            except Exception as e:
                print(f"Could not promote prompt: {e}", flush=True)
        else:
            print(
                f"No promotion — best accuracy {best['accuracy']:.1%} "
                f"did not beat baseline {baseline_accuracy:.1%}",
                flush=True,
            )

        print(f"\nBest section ({len(self._best_section)} chars):", flush=True)
        print(self._best_section[:200] + "...", flush=True)
        self._end_mlflow_run()

    def _end_mlflow_run(self):
        """End the MLflow tracking run."""
        try:
            import mlflow

            mlflow.log_metric("final_best_accuracy", self._best_accuracy)
            mlflow.log_metric("total_steps", self._step_count)
            mlflow.end_run()
        except Exception:
            pass
