"""LLM-based prompt optimizer for the RL tuning loop."""

import os
import re


class PromptOptimizerAgent:
    """Generates improved Game Prediction Framework sections from observations."""

    def __init__(
        self,
        model_name: str = "",
        model_endpoint: str = "",
        temperature: float = 0.7,
    ):
        from langchain_openai import ChatOpenAI

        self._llm = ChatOpenAI(
            model=model_name or os.environ.get("MODEL_NAME", "qwen38-27b"),
            base_url=model_endpoint
            or os.environ.get("MODEL_ENDPOINT", ""),
            api_key=os.environ.get("OPENAI_API_KEY", "not-required"),
            temperature=temperature,
            max_tokens=8192,
            streaming=False,
            model_kwargs={
                "extra_body": {"chat_template_kwargs": {"enable_thinking": False}}
            },
        )

    def generate_prompt(self, observation: dict) -> str:
        """Generate an improved prompt section from the observation."""
        from langchain_core.messages import HumanMessage

        meta_prompt = self._build_meta_prompt(observation)
        response = self._llm.invoke([HumanMessage(content=meta_prompt)])
        return self._extract_section(response.content)

    def _build_meta_prompt(self, observation: dict) -> str:
        return f"""You are an expert prompt engineer optimizing an MLB game prediction system.
The prediction agent uses a "Game Prediction Framework" section in its system prompt to decide how to pick winners. Your job is to improve this section to increase prediction accuracy.

## Current Prediction Framework
{observation["prompt_section"]}

## Performance Metrics
{observation["metrics"]}

## Error Analysis (Wrong Predictions)
{observation["error_analysis"]}

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
- Notes column in the weight factors table
- Batch size for processing games

### Optimization guidance from error analysis
- If STRONG picks are wrong too often: tighten STRONG criteria or add conditions
- If COIN FLIP picks lean wrong: adjust decision rules for close matchups
- If home field is over-weighted: reduce its weight or add a stronger anti-pattern
- If bullpen is under-weighted: increase bullpen weight percentage
- If pitcher ERA thresholds miss too often: adjust the ERA boundaries
- If head-to-head is unreliable: reduce its weight
- Look at the specific wrong picks and identify patterns

## Output

Return ONLY the complete replacement text for the Game Prediction Framework section.
Start with "### Game Prediction Framework" and end with the Self-Learning section.
Do NOT include "### Statistics Definitions" or anything after it.
Do NOT include any commentary before or after the section — just the raw section text.
IMPORTANT: Keep the section under 10000 characters. Be concise — tighter rules beat verbose ones.
"""

    def _extract_section(self, response: str) -> str:
        """Extract the prompt section from the LLM response."""
        start_marker = "### Game Prediction Framework"
        end_marker = "### Statistics Definitions"

        start = response.find(start_marker)
        if start == -1:
            return response.strip()

        text = response[start:]

        end = text.find(end_marker)
        if end != -1:
            text = text[:end]

        return text.rstrip() + "\n"
