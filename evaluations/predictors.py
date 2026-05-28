"""Predictor function for MLB Data Agent evaluation.

Invokes the LangGraph agent directly (same code as the Chainlit app)
rather than calling via HTTP. This avoids Chainlit websocket complexity.
"""

import os
import asyncio
from typing import Callable


def create_predict_fn(
    model_name: str = "",
    model_endpoint: str = "",
    trino_host: str = "",
    trino_port: int = 8080,
) -> Callable[[str], str]:
    """Create a prediction function that invokes the MLB agent.

    Args:
        model_name: LLM model name (default from env)
        model_endpoint: LLM endpoint URL (default from env)
        trino_host: Trino host (default from env)
        trino_port: Trino port (default from env)
    """
    _model_name = model_name or os.environ.get("MODEL_NAME", "qwen36-27b")
    _model_endpoint = model_endpoint or os.environ.get(
        "MODEL_ENDPOINT",
        "http://maas.apps.ocp.cloud.rhai-tmm.dev/prelude-maas/qwen36-27b/v1",
    )
    _trino_host = trino_host or os.environ.get("TRINO_QUERY_HOST", "trino")
    _trino_port = trino_port or int(os.environ.get("TRINO_QUERY_PORT", "8080"))

    os.environ["TRINO_QUERY_HOST"] = _trino_host
    os.environ["TRINO_QUERY_PORT"] = str(_trino_port)

    def predict_fn(question: str) -> str:
        """Invoke the MLB agent and return the response."""
        try:
            import nest_asyncio
            nest_asyncio.apply()
        except ImportError:
            pass

        from langchain_openai import ChatOpenAI
        from langgraph.prebuilt import create_react_agent
        from langchain_core.messages import HumanMessage

        import sys
        agent_dir = os.path.join(os.path.dirname(__file__), "..", "agents", "mlb-agent")
        if agent_dir not in sys.path:
            sys.path.insert(0, agent_dir)

        from tools import query_trino, describe_datasets, get_methodology

        prompt_path = os.path.join(agent_dir, "system_prompt.md")
        if os.path.exists(prompt_path):
            with open(prompt_path) as f:
                system_prompt = f.read()
        else:
            system_prompt = "You are a Major League Baseball data agent."

        llm = ChatOpenAI(
            model=_model_name,
            base_url=_model_endpoint,
            api_key=os.environ.get("OPENAI_API_KEY", "not-required"),
            temperature=0.3,
            max_tokens=8192,
            streaming=False,
            model_kwargs={
                "extra_body": {
                    "chat_template_kwargs": {"enable_thinking": False}
                }
            },
        )

        agent = create_react_agent(
            model=llm,
            tools=[query_trino, describe_datasets, get_methodology],
            prompt=system_prompt,
        )

        try:
            import mlflow
            with mlflow.start_span(name="mlb-agent-eval") as span:
                span.set_inputs({"question": question})
                result = asyncio.run(
                    agent.ainvoke({"messages": [HumanMessage(content=question)]})
                )
                output = ""
                for m in reversed(result.get("messages", [])):
                    if hasattr(m, "type") and m.type == "ai" and not getattr(m, "tool_calls", None):
                        output = m.content or ""
                        break
                span.set_outputs({"response": output[:500]})
                return output
        except Exception as e:
            print(f"Agent error: {e}")
            return f"Error: {e}"

    return predict_fn
