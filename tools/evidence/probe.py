"""Print what the patched readers resolve, then make the real calls.

Run once per registry case, in a fresh process, with no proxy variable set.
Prints JSON on the last line for the driver to check.
"""

from __future__ import annotations

import asyncio
import json
import urllib.request

from slack_sdk.errors import SlackApiError
from slack_sdk.web import WebClient
from slack_sdk.web.async_client import AsyncWebClient

from kiro_crew.http_proxy import SLACK_API_URL, resolve_proxy
from kiro_crew.telegram.client import TelegramAuthError, TelegramClient
from kiro_crew.telegram.client_http import _api_base

out: dict = {}
out["getproxies"] = urllib.request.getproxies()
out["resolve_slack"] = resolve_proxy(SLACK_API_URL)
out["resolve_telegram"] = resolve_proxy(_api_base())
print("getproxies():", out["getproxies"])
print("resolve_proxy(SLACK_API_URL):", out["resolve_slack"])
print("resolve_proxy(telegram api base):", out["resolve_telegram"])

# Sync WebClient (cli_setup.py, slack/enterprise.py): urllib transport, which
# with proxy=None reads urllib's own getproxies(), registry included.
slack = WebClient(token="xoxb-0000-bogus", proxy=resolve_proxy(SLACK_API_URL), timeout=30)
print("slack WebClient.proxy:", slack.proxy)
for name, call in (
    ("sync auth.test", lambda: slack.auth_test()),
    ("sync apps.connections.open", lambda: slack.apps_connections_open(app_token="xapp-0-bogus")),
):
    try:
        resp = call()
        out[name] = f"ok:{resp.get('ok')}"
    except SlackApiError as exc:
        out[name] = f"slack_error:{exc.response.get('error')}"
    except Exception as exc:  # noqa: BLE001 - evidence: report any transport failure
        out[name] = f"transport_error:{type(exc).__name__}:{exc}"
    print(f"slack {name}:", out[name])


async def _slack_async() -> None:
    # The gateway's live Slack path (slack/events.py, slack/client.py) uses
    # AsyncWebClient, whose transport is aiohttp and reads only the environment.
    client = AsyncWebClient(token="xoxb-0000-bogus", proxy=resolve_proxy(SLACK_API_URL), timeout=30)
    print("slack AsyncWebClient.proxy:", client.proxy)
    for name, call in (
        ("async auth.test", lambda: client.auth_test()),
        ("async apps.connections.open", lambda: client.apps_connections_open(app_token="xapp-0-bogus")),
    ):
        try:
            resp = await call()
            out[name] = f"ok:{resp.get('ok')}"
        except SlackApiError as exc:
            out[name] = f"slack_error:{exc.response.get('error')}"
        except Exception as exc:  # noqa: BLE001 - evidence: report any transport failure
            out[name] = f"transport_error:{type(exc).__name__}:{exc}"
        print(f"slack {name}:", out[name])


asyncio.run(_slack_async())


async def _telegram() -> str:
    client = TelegramClient(token="123456:bogus-token-for-evidence")
    print("telegram client._proxy:", client._proxy)
    out["telegram_proxy"] = client._proxy
    try:
        await client.get_me()
        return "ok"
    except TelegramAuthError as exc:
        return f"auth_error:{exc}"
    except Exception as exc:  # noqa: BLE001 - evidence: report any transport failure
        return f"transport_error:{type(exc).__name__}:{exc}"
    finally:
        await client.close()


out["getMe"] = asyncio.run(_telegram())
print("telegram getMe:", out["getMe"])
print("RESULT " + json.dumps(out))
