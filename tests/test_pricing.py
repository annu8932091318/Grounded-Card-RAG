"""Cost per conversation is only as good as the price table: a configured model without a price reads as $0."""
from src.const.config import config
from src.modules.llm.pricing import PRICES_PER_MTOK, cost_usd

DOTENV_CHAIN = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3-flash-preview", "gemini-2.5-flash",
                "openai/gpt-oss-120b", "openai/gpt-oss-20b"]


def test_every_chain_model_has_a_price():
    for model in set(config.gemini_models + config.grok_models + DOTENV_CHAIN):
        assert model in PRICES_PER_MTOK, f"{model}: add it to src/modules/llm/pricing.py"


def test_cost_formula():
    # 3,750 input + 217 output tokens (measured mean per chat turn) on gemini-3.5-flash-lite
    assert round(cost_usd("gemini-3.5-flash-lite", 3750, 217), 6) == round((3750 * 0.30 + 217 * 2.50) / 1e6, 6)
    assert cost_usd("unknown-model", 1000, 1000) == 0.0
