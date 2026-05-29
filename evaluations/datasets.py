"""MLB Data Agent evaluation dataset.

30 seed questions covering data retrieval, cross-dataset reasoning,
scope boundaries, causal inference, era context, and terminology.
"""

MLB_EVAL_DATASET = [
    # ── Data Retrieval (5) ────────────────────────────────────
    {
        "inputs": {"question": "How many home runs did Barry Bonds hit in 2001?"},
        "expectations": {
            "expected_keywords": ["73", "Bonds"],
            "question_type": "data_retrieval",
            "can_server_answer": "yes",
            "expected_tools": ["query_trino"],
        },
    },
    {
        "inputs": {"question": "Who won the 2024 World Series?"},
        "expectations": {
            "expected_keywords": ["Dodgers"],
            "question_type": "data_retrieval",
            "can_server_answer": "yes",
            "expected_tools": ["query_trino"],
        },
    },
    {
        "inputs": {"question": "What are the current AL East standings?"},
        "expectations": {
            "expected_keywords": ["wins", "losses"],
            "question_type": "data_retrieval",
            "can_server_answer": "yes",
            "expected_tools": ["query_trino"],
        },
    },
    {
        "inputs": {"question": "What was the average fastball velocity in 2018?"},
        "expectations": {
            "expected_keywords": ["mph", "fastball"],
            "question_type": "data_retrieval",
            "can_server_answer": "yes",
            "expected_tools": ["query_trino"],
        },
    },
    {
        "inputs": {"question": "Who won the MVP award in 2025?"},
        "expectations": {
            "expected_keywords": ["MVP"],
            "question_type": "data_retrieval",
            "can_server_answer": "yes",
            "expected_tools": ["query_trino"],
        },
    },

    # ── Cross-Dataset Reasoning (5) ──────────────────────────
    {
        "inputs": {"question": "Compare Babe Ruth and Hank Aaron's career batting statistics"},
        "expectations": {
            "expected_keywords": ["Ruth", "Aaron", "home runs"],
            "question_type": "cross_dataset",
            "can_server_answer": "yes",
            "expected_tools": ["query_trino"],
        },
    },
    {
        "inputs": {"question": "How does weather affect home runs at Wrigley Field?"},
        "expectations": {
            "expected_keywords": ["temperature", "Wrigley"],
            "question_type": "cross_dataset",
            "can_server_answer": "partially",
            "expected_tools": ["query_trino"],
        },
    },
    {
        "inputs": {"question": "Which pitcher had the most strikeouts in 2026 and what was their average fastball velocity?"},
        "expectations": {
            "expected_keywords": ["strikeouts", "velocity"],
            "question_type": "cross_dataset",
            "can_server_answer": "yes",
            "expected_tools": ["query_trino"],
        },
    },
    {
        "inputs": {"question": "How has the average player salary changed from 1985 to 2016?"},
        "expectations": {
            "expected_keywords": ["salary", "1985", "2016"],
            "question_type": "cross_dataset",
            "can_server_answer": "yes",
            "expected_tools": ["query_trino"],
        },
    },
    {
        "inputs": {"question": "Compare postseason batting stats to regular season for 2018"},
        "expectations": {
            "expected_keywords": ["postseason", "regular season"],
            "question_type": "cross_dataset",
            "can_server_answer": "yes",
            "expected_tools": ["query_trino"],
        },
    },

    # ── Scope Boundary (5) ───────────────────────────────────
    {
        "inputs": {"question": "What is Mike Trout's WAR this season?"},
        "expectations": {
            "expected_keywords": ["not available", "WAR"],
            "question_type": "scope_boundary",
            "can_server_answer": "no",
            "expected_tools": [],
            "forbidden_content": ["WAR is", "wins above replacement is 5", "wins above replacement is 6", "wins above replacement is 7"],
        },
    },
    {
        "inputs": {"question": "Show me pitch-by-pitch data for the 2022 World Series"},
        "expectations": {
            "expected_keywords": ["not available", "2020", "2023"],
            "question_type": "scope_boundary",
            "can_server_answer": "no",
            "expected_tools": [],
            "forbidden_content": ["here are the 2022 pitches", "pitch data for 2022"],
        },
    },
    {
        "inputs": {"question": "What were the player salaries in 2025?"},
        "expectations": {
            "expected_keywords": ["2016", "salary"],
            "question_type": "scope_boundary",
            "can_server_answer": "no",
            "expected_tools": [],
            "forbidden_content": ["salary in 2025", "earned in 2025"],
        },
    },
    {
        "inputs": {"question": "What was the weather during yesterday's Yankees game?"},
        "expectations": {
            "expected_keywords": ["weather"],
            "question_type": "scope_boundary",
            "can_server_answer": "partially",
            "expected_tools": ["query_trino"],
        },
    },
    {
        "inputs": {"question": "Show me the box score for today's games"},
        "expectations": {
            "expected_keywords": ["completed", "game"],
            "question_type": "scope_boundary",
            "can_server_answer": "partially",
            "expected_tools": ["query_trino"],
        },
    },

    # ── Causal Inference (5) ──────────────────────────────────
    {
        "inputs": {"question": "Does the pitch clock cause fewer strikeouts?"},
        "expectations": {
            "expected_keywords": ["correlation", "cannot"],
            "question_type": "causal_inference",
            "can_server_answer": "partially",
            "expected_tools": ["query_trino"],
            "forbidden_content": ["pitch clock causes", "proven that the pitch clock"],
        },
    },
    {
        "inputs": {"question": "Did steroids cause the home run surge in the late 1990s?"},
        "expectations": {
            "expected_keywords": ["cannot", "causal"],
            "question_type": "causal_inference",
            "can_server_answer": "no",
            "forbidden_content": ["steroids caused", "PEDs caused", "proven that"],
        },
    },
    {
        "inputs": {"question": "Does higher spin rate lead to more strikeouts?"},
        "expectations": {
            "expected_keywords": ["correlation"],
            "question_type": "causal_inference",
            "can_server_answer": "partially",
            "expected_tools": ["query_trino"],
            "forbidden_content": ["higher spin rate causes"],
        },
    },
    {
        "inputs": {"question": "Does cold weather cause more pitcher injuries?"},
        "expectations": {
            "expected_keywords": ["cannot", "injury"],
            "question_type": "causal_inference",
            "can_server_answer": "no",
            "forbidden_content": ["cold weather causes injuries", "causes injuries"],
        },
    },
    {
        "inputs": {"question": "Does the designated hitter improve team offense?"},
        "expectations": {
            "expected_keywords": ["designated hitter", "DH"],
            "question_type": "causal_inference",
            "can_server_answer": "partially",
            "expected_tools": ["query_trino"],
            "forbidden_content": ["DH causes better", "DH definitively improves"],
        },
    },

    # ── Era Context / Methodology (5) ────────────────────────
    {
        "inputs": {"question": "Who is the greatest home run hitter of all time?"},
        "expectations": {
            "expected_keywords": ["era", "context"],
            "question_type": "era_context",
            "can_server_answer": "partially",
            "expected_tools": ["query_trino"],
            "forbidden_content": ["definitively", "objectively the greatest"],
        },
    },
    {
        "inputs": {"question": "Compare pitching ERAs across the 1960s and 2020s"},
        "expectations": {
            "expected_keywords": ["mound", "1969"],
            "question_type": "era_context",
            "can_server_answer": "yes",
            "expected_tools": ["query_trino"],
        },
    },
    {
        "inputs": {"question": "How do Negro League statistics compare to MLB?"},
        "expectations": {
            "expected_keywords": ["incomplete", "Negro League"],
            "question_type": "era_context",
            "can_server_answer": "partially",
            "expected_tools": ["query_trino"],
        },
    },
    {
        "inputs": {"question": "Has the strikeout rate increased over time?"},
        "expectations": {
            "expected_keywords": ["strikeout"],
            "question_type": "era_context",
            "can_server_answer": "yes",
            "expected_tools": ["query_trino"],
        },
    },
    {
        "inputs": {"question": "Compare the 1927 Yankees to the 2023 Rangers"},
        "expectations": {
            "expected_keywords": ["era", "context"],
            "question_type": "era_context",
            "can_server_answer": "yes",
            "expected_tools": ["query_trino"],
        },
    },

    # ── Terminology / Geographic (5) ─────────────────────────
    {
        "inputs": {"question": "What's the food poisoning rate in baseball?"},
        "expectations": {
            "expected_keywords": ["baseball", "data"],
            "question_type": "terminology",
            "can_server_answer": "no",
            "expected_tools": [],
        },
    },
    {
        "inputs": {"question": "Show me the slugging percentage leaders for 2024"},
        "expectations": {
            "expected_keywords": ["slugging", "SLG"],
            "question_type": "terminology",
            "can_server_answer": "yes",
            "expected_tools": ["query_trino"],
        },
    },
    {
        "inputs": {"question": "How did the Bronx Bombers do last year?"},
        "expectations": {
            "expected_keywords": ["Yankees"],
            "question_type": "terminology",
            "can_server_answer": "yes",
            "expected_tools": ["query_trino"],
        },
    },
    {
        "inputs": {"question": "What's the WHIP for Clayton Kershaw in 2018?"},
        "expectations": {
            "expected_keywords": ["WHIP", "Kershaw"],
            "question_type": "terminology",
            "can_server_answer": "yes",
            "expected_tools": ["query_trino"],
        },
    },
    {
        "inputs": {"question": "Show home run stats for all California teams"},
        "expectations": {
            "expected_keywords": ["home runs"],
            "question_type": "geographic",
            "can_server_answer": "yes",
            "expected_tools": ["query_trino"],
        },
    },
]
