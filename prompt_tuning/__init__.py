import gymnasium

gymnasium.register(
    id="PromptTuningEnv-v0",
    entry_point="prompt_tuning.env:PromptTuningEnv",
    max_episode_steps=200,
)
