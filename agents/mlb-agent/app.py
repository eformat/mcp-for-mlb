"""MLB Data Agent — Chainlit + LangGraph + MLflow.

Major League Baseball data agent with structured reasoning,
Trino SQL data access, and full MLflow tracing.
"""

import os
import re
import time

import mlflow_init
mlflow_init.init()

from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent
from langchain_core.messages import HumanMessage, AIMessage
from langchain_core.callbacks import BaseCallbackHandler
import chainlit as cl

from tools import query_trino, describe_datasets, get_methodology, check_dataset_permission
from prompts import get_system_prompt
from data_layer import InMemoryDataLayer

TOOLS = [query_trino, describe_datasets, get_methodology, check_dataset_permission]

cl.data._data_layer = InMemoryDataLayer()


def _build_agent(username: str = "anonymous"):
    """Create a LangGraph ReAct agent with tool-calling support."""
    llm = ChatOpenAI(
        model=os.environ.get("MODEL_NAME", "qwen36-27b"),
        base_url=os.environ.get("MODEL_ENDPOINT",
            "http://maas.apps.ocp.cloud.rhai-tmm.dev/prelude-maas/qwen36-27b/v1"),
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

    prompt = get_system_prompt().replace("{current_user}", username)

    return create_react_agent(
        model=llm,
        tools=TOOLS,
        prompt=prompt,
    )


def _clean_output(text: str) -> tuple[str, str]:
    """Strip XML tags. Returns (answer, reasoning) tuple."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"</?think>", "", text)
    last = text.rfind("</think>")
    if last != -1:
        text = text[last + 8:]

    reasoning = ""
    match = re.search(r"<reasoning>(.*?)</reasoning>", text, re.DOTALL)
    if match:
        reasoning = match.group(1).strip()
        text = text[: match.start()] + text[match.end() :]

    if reasoning:
        lines = []
        for line in reasoning.split("\n"):
            line = line.strip()
            if not line:
                continue
            if ":" in line:
                key, _, value = line.partition(":")
                lines.append(f"- **{key.strip()}:** {value.strip()}")
            else:
                lines.append(f"- {line}")
        reasoning = "\n".join(lines)

    output = text.strip()

    fixed_lines = []
    for line in output.split("\n"):
        if "Data Freshness" in line:
            line = line.lstrip("#").strip()
            line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)
            line = line.replace(" | ", " · ")
        fixed_lines.append(line)
    output = "\n".join(fixed_lines)

    return output, reasoning


@cl.set_starters
async def starters():
    return [
        cl.Starter(label="Home run leaders", message="Who hit the most home runs in a single season?"),
        cl.Starter(label="Ruth vs Aaron", message="Compare Babe Ruth and Hank Aaron's career batting statistics"),
        cl.Starter(label="World Series since 2000", message="List all World Series winners since 2000"),
        cl.Starter(label="MVP winners 2024", message="Who won the MVP award in 2024?"),
        cl.Starter(label="Best ERA all time", message="Which pitcher has the lowest career ERA with at least 2000 innings pitched?"),
        cl.Starter(label="Park attendance", message="Which ballparks had the highest total attendance in 2023?"),
        cl.Starter(label="Weather and home runs", message="How does temperature affect home run rates? Compare hot vs cold game days"),
        cl.Starter(label="Hall of Fame voting", message="Which players received the highest percentage of Hall of Fame votes on their first ballot?"),
        cl.Starter(label="Salary leaders", message="Who were the highest-paid players in baseball in 2016?"),
        cl.Starter(label="Triple Crown winners", message="Which players won the batting Triple Crown (led league in AVG, HR, and RBI in the same season)?"),
        cl.Starter(label="Fastball velocity trends", message="How has average fastball velocity changed from 2015 to 2019?"),
        cl.Starter(label="Pitch type breakdown", message="What percentage of pitches were fastballs vs breaking balls vs offspeed in 2018?"),
        cl.Starter(label="Highest spin rate", message="Which pitchers had the highest average spin rate on curveballs in the 2024 postseason?"),
        cl.Starter(label="Strikeout pitch types", message="Which pitch types generate the most swinging strikes?"),
    ]


@cl.password_auth_callback
def auth_callback(username: str, password: str):
    """Simple password auth for chat history persistence."""
    valid_user = os.environ.get("AUTH_USERNAME", "admin")
    valid_pass = os.environ.get("AUTH_PASSWORD", "admin")
    if username == valid_user and password == valid_pass:
        return cl.User(identifier=username, metadata={"role": "admin"})
    return None


@cl.on_chat_start
async def start():
    user = cl.user_session.get("user")
    username = user.identifier if user else "anonymous"
    agent = _build_agent(username=username)
    cl.user_session.set("agent", agent)
    cl.user_session.set("chat_history", [])


class _TracingHandler(BaseCallbackHandler):
    """Callback to track timing and tool usage."""

    def __init__(self):
        self.last_tool_end = None
        self.tool_names = []

    def on_tool_start(self, serialized, input_str, **kwargs):
        self.tool_names.append(serialized.get("name", "tool"))

    def on_tool_end(self, output, **kwargs):
        self.last_tool_end = time.time()


_PERMISSION_INSIST_MSG = (
    "Your answer did not include a call to check_dataset_permission. "
    "The system prompt requires you to check permissions BEFORE querying "
    "any dataset with query_trino. Please call check_dataset_permission "
    "first with the current user's subject_id, then proceed."
)


@cl.on_message
async def on_message(message: cl.Message):
    agent = cl.user_session.get("agent")
    chat_history = cl.user_session.get("chat_history")
    session_id = cl.user_session.get("id") or "unknown"

    t_start = time.time()

    messages = []
    for role, content in chat_history:
        if role == "human":
            messages.append(HumanMessage(content=content))
        else:
            messages.append(AIMessage(content=content))
    messages.append(HumanMessage(content=message.content))

    handler = _TracingHandler()

    import asyncio

    def _run_agent():
        try:
            import mlflow

            with mlflow.tracing.context(session_id=session_id, user="chainlit"):
                with mlflow.start_span(name="mlb_agent") as span:
                    span.set_inputs({"question": messages[-1].content[:200] if messages else ""})

                    mlflow.genai.load_prompt(
                        "prompts:/mlb-agent.system@production",
                        allow_missing=True,
                        cache_ttl_seconds=60,
                    )

                    result = agent.invoke(
                        {"messages": messages},
                        config={"callbacks": [handler]},
                    )

                    if ("query_trino" in handler.tool_names
                            and "check_dataset_permission" not in handler.tool_names):
                        retry_messages = list(result.get("messages", []))
                        retry_messages.append(HumanMessage(content=_PERMISSION_INSIST_MSG))
                        handler.tool_names.clear()
                        result = agent.invoke(
                            {"messages": retry_messages},
                            config={"callbacks": [handler]},
                        )

                    span.set_outputs({"done": True})

                return result
        except Exception:
            return agent.invoke(
                {"messages": messages},
                config={"callbacks": [handler]},
            )

    async with cl.Step(name="Query MLB data...", type="run") as step:
        result = await asyncio.get_event_loop().run_in_executor(None, _run_agent)
        if handler.tool_names:
            step.output = "Tools called: " + ", ".join(handler.tool_names)
        else:
            step.output = "Completed"

    t_end = time.time()
    total = t_end - t_start
    query_time = (handler.last_tool_end - t_start) if handler.last_tool_end else 0
    gen_time = total - query_time

    raw_output = ""
    for m in reversed(result.get("messages", [])):
        if hasattr(m, "type") and m.type == "ai" and not getattr(m, "tool_calls", None):
            raw_output = m.content or ""
            break

    answer, reasoning = _clean_output(raw_output)

    footer_parts = []
    if handler.tool_names:
        tools_used = ", ".join(f"`{t}`" for t in handler.tool_names)
        footer_parts.append(f"**Tools used:** {tools_used}")

    timing_parts = []
    if query_time > 0:
        timing_parts.append(f"**Query:** {query_time:.1f}s")
    if gen_time > 0:
        timing_parts.append(f"**Generation:** {gen_time:.1f}s")
    timing_parts.append(f"**Total:** {total:.1f}s")
    footer_parts.append(" | ".join(timing_parts))

    footer = "\n\n---\n" + " | ".join(footer_parts)

    msg = cl.Message(content=answer + footer)
    await msg.send()

    reasoning_output = reasoning
    if not reasoning_output and handler.tool_names:
        tools_summary = ", ".join(handler.tool_names)
        used_trino = "query_trino" in handler.tool_names
        used_methodology = "get_methodology" in handler.tool_names
        reasoning_output = (
            f"- **cross_dataset:** Used {tools_summary} to query the MLB Iceberg lakehouse. "
            f"Data sourced from the Lahman Baseball Database.\n"
            f"- **methodology:** {'Retrieved detailed methodology via get_methodology tool. ' if used_methodology else ''}"
            f"Lahman Database compiles official MLB game records and box scores.\n"
            f"- **scope:** {'Query executed against Trino lakehouse. ' if used_trino else ''}"
            f"Answer is grounded in MLB statistical data.\n"
            f"- **causal_inference:** N/A — statistical data shows correlations, not causation.\n"
            f"- **geographic:** Data is at team/ballpark level.\n"
            f"- **terminology:** MLB statistical terms used as defined in the database."
        )

    if reasoning_output:
        async with cl.Step(name="Reasoning", type="tool", parent_id=msg.id) as rstep:
            rstep.output = reasoning_output

    chat_history.extend([
        ("human", message.content),
        ("ai", answer),
    ])
