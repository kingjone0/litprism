"""User-confirmed model capabilities and scoped input-meter calibration.

Only the specified gateway/model is covered. K/M use decimal token counts,
matching the read-only capability data. The safety margin is a budgeting estimate.
"""
from urllib.parse import urlparse


def confirmed_limits(base_url: str, model: str) -> dict:
    url = urlparse(base_url)
    if (url.hostname == 'api.commandcode.ai' and url.path.rstrip('/') == '/provider/v1'
            and model.lower() in ('deepseek/deepseek-v4.1-flash', 'deepseek-v4.1-flash')):
        return {'window_tokens': 1_000_000, 'max_output_tokens': 384_000,
                'source': 'user_confirmed'}
    return {}


def input_safety_percent(base_url: str, model: str, protocol: str | None, task: str | None) -> int:
    # Frozen N04 Chat Completions samples for this gateway underestimated long
    # QA inputs by about 6.0%. Round up and retain the existing default elsewhere.
    if protocol == 'openai' and task == 'qa' and confirmed_limits(base_url, model):
        return 7
    return 5
