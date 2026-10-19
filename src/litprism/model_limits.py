"""User-confirmed model capabilities, separate from request budgets.

Only the specified gateway/model is covered. K/M use decimal token counts,
matching the model-window editor. This is configuration, not a service probe.
"""
from urllib.parse import urlparse


def confirmed_limits(base_url: str, model: str) -> dict:
    url = urlparse(base_url)
    if (url.hostname == 'api.commandcode.ai' and url.path.rstrip('/') == '/provider/v1'
            and model.lower() in ('deepseek/deepseek-v4.1-flash', 'deepseek-v4.1-flash')):
        return {'window_tokens': 1_000_000, 'max_output_tokens': 384_000,
                'source': 'user_confirmed'}
    return {}
