"""Custom scorers for MLB Data Agent evaluation.

Maps to the 7 capability dimensions from the reasoning protocol:
1. Cross-dataset reasoning
2. Methodology awareness
3. Scope adherence
4. Causal inference boundaries
5. Geographic resolution knowledge
6. Terminology fluency
7. Confidence calibration

Plus built-in MLflow scorers: RelevanceToQuery, Safety
"""

from mlflow.genai.scorers import scorer, Guidelines, RelevanceToQuery, Safety


# ---------------------------------------------------------------------------
# Deterministic scorers (fast, no LLM calls)
# ---------------------------------------------------------------------------

@scorer
def contains_expected(inputs: dict, outputs: str, expectations: dict) -> bool:
    """Check if expected keywords appear in the response."""
    keywords = expectations.get("expected_keywords", [])
    if not keywords:
        return True
    output_lower = str(outputs).lower()
    return any(kw.lower() in output_lower for kw in keywords)


@scorer
def no_forbidden_content(inputs: dict, outputs: str, expectations: dict) -> bool:
    """Check that forbidden content does not appear in the response."""
    forbidden = expectations.get("forbidden_content", [])
    if not forbidden:
        return True
    output_lower = str(outputs).lower()
    return not any(f.lower() in output_lower for f in forbidden)


@scorer
def confidence_card_present(outputs: str) -> bool:
    """Check if the response includes a Data Confidence card."""
    return "data confidence" in str(outputs).lower()


@scorer
def response_length(outputs: str) -> float:
    """Ensure response has minimum substantive content."""
    length = len(str(outputs))
    return 1.0 if length >= 100 else 0.5


# ---------------------------------------------------------------------------
# LLM-as-Judge scorers (7 capability dimensions)
# ---------------------------------------------------------------------------

def get_capability_scorers(judge_model: str) -> list:
    """Create LLM-as-Judge scorers for 7 capability dimensions.

    Args:
        judge_model: MLflow model format, e.g. "openai:/gemma4"
    """
    return [
        Guidelines(
            name="cross_dataset_reasoning",
            guidelines=[
                "The response must state which dataset or table was used and why",
                "The response should mention alternatives considered if applicable",
                "For questions requiring joins (batting+people, games+weather), the join reasoning should be clear",
            ],
            model=judge_model,
        ),
        Guidelines(
            name="methodology_awareness",
            guidelines=[
                "The response should note era context when comparing across time periods",
                "The response should mention data collection characteristics (season aggregates, Statcast tracking)",
                "For historical data, the response should note relevant rule changes (DH, mound height, etc.)",
            ],
            model=judge_model,
        ),
        Guidelines(
            name="scope_adherence",
            guidelines=[
                "The response must NOT fabricate statistics not in the database",
                "If the question asks about unavailable data (WAR, post-2016 salaries, 2020-2023 pitches), it must say so",
                "The response should clearly state what the data can and cannot answer",
                "If the question is out of scope, the response should explain why and offer alternatives",
            ],
            model=judge_model,
        ),
        Guidelines(
            name="causal_inference_boundaries",
            guidelines=[
                "The response must NOT attribute causation from statistical correlations",
                "If the user implies causation (does X cause Y), the response should clarify limitations",
                "Associations and correlations should be distinguished from causation",
                "The response should frame findings as 'correlated with' not 'caused by'",
            ],
            model=judge_model,
        ),
        Guidelines(
            name="geographic_resolution",
            guidelines=[
                "The response should correctly handle team/park/city-level data",
                "If asked about a specific city, the response should identify the relevant team(s)",
                "Weather data is at city/station level and should be matched to ballparks appropriately",
            ],
            model=judge_model,
        ),
        Guidelines(
            name="terminology_fluency",
            guidelines=[
                "Lay terms should be mapped to correct database columns",
                "Computed stats (AVG, OBP, SLG, WHIP) should use the correct formula",
                "Team nicknames should be mapped to teamID codes",
                "Pitch type codes should be explained (FF=fastball, SL=slider, etc.)",
            ],
            model=judge_model,
        ),
        Guidelines(
            name="confidence_calibration",
            guidelines=[
                "The response should include a Data Confidence assessment (HIGH/MODERATE/LOW)",
                "The confidence level should reflect what data was actually retrieved",
                "Cross-era comparisons should have MODERATE confidence at most",
                "Questions about unavailable data should have LOW confidence",
            ],
            model=judge_model,
        ),
        RelevanceToQuery(model=judge_model),
        Safety(model=judge_model),
    ]


def get_all_scorers(judge_model: str = "") -> list:
    """Get all scorers — deterministic + LLM judges.

    Args:
        judge_model: MLflow model format. If empty, only deterministic scorers.
    """
    deterministic = [
        contains_expected,
        no_forbidden_content,
        confidence_card_present,
        response_length,
    ]

    if judge_model:
        return deterministic + get_capability_scorers(judge_model)
    return deterministic
