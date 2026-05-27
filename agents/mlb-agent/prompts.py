"""System prompt with MLflow Prompt Registry integration."""

import os
from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent

_SYSTEM_PROMPT_FILE = _PROMPTS_DIR / "system_prompt.md"
_SYSTEM_PROMPT_DEFAULT = _SYSTEM_PROMPT_FILE.read_text() if _SYSTEM_PROMPT_FILE.exists() else "You are a Major League Baseball data agent."

_PROMPT_REGISTRY = {
    "system": ("mlb-agent.system", _SYSTEM_PROMPT_DEFAULT),
}

_mlflow_prompts_enabled = False


def register_prompts():
    """Register prompts in MLflow if not already present."""
    global _mlflow_prompts_enabled
    mlflow_uri = os.environ.get("MLFLOW_TRACKING_URI", "").strip()
    if not mlflow_uri:
        return

    try:
        import mlflow

        for key, (name, template) in _PROMPT_REGISTRY.items():
            try:
                existing = mlflow.genai.load_prompt(name, version=1, allow_missing=True)
                if existing is None:
                    mlflow.genai.register_prompt(
                        name=name,
                        template=template,
                        commit_message="Initial registration",
                        tags={"agent": key, "source": "prompts.py"},
                    )
                    mlflow.genai.set_prompt_alias(name, alias="production", version=1)
                    print(f"[prompts] Registered '{name}' v1 in MLflow", flush=True)
                else:
                    print(f"[prompts] '{name}' already exists (v{existing.version})", flush=True)
            except Exception as exc:
                print(f"[prompts] Failed to register '{name}': {exc}", flush=True)

        _mlflow_prompts_enabled = True
    except Exception as exc:
        print(f"[prompts] MLflow unavailable: {exc}", flush=True)


def get_system_prompt() -> str:
    """Load system prompt from MLflow or fallback to hardcoded."""
    if not _mlflow_prompts_enabled:
        return _SYSTEM_PROMPT_DEFAULT
    try:
        import mlflow
        prompt = mlflow.genai.load_prompt(
            "prompts:/mlb-agent.system@production",
            allow_missing=True,
            cache_ttl_seconds=60,
        )
        if prompt:
            return prompt.template
    except Exception:
        pass
    return _SYSTEM_PROMPT_DEFAULT
