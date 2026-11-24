"""NO_PROXY-only case: does sign-in traffic read the OS proxy?

Run with the OS system proxy pointed at the logging CONNECT proxy and
NO_PROXY=localhost,127.0.0.1 as the only proxy variable. Prints what
http_proxy.resolve_proxy returns for the sign-in host and makes one HTTPS
request through the same path the auth code uses (aiohttp, trust_env=False,
proxy=await resolve_proxy_off_loop(url)). Run once against the PR head and
once against main to show the change.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import aiohttp

from kiro_crew import http_proxy

# The AWS SSO-OIDC endpoint the Builder ID and IAM Identity Center sign-ins post to.
SIGN_IN_URL = "https://oidc.us-east-1.amazonaws.com/"


async def _request() -> str:
    resolve = getattr(http_proxy, "resolve_proxy_off_loop", None)
    proxy = await resolve(SIGN_IN_URL) if resolve else http_proxy.resolve_proxy(SIGN_IN_URL)
    timeout = aiohttp.ClientTimeout(total=20)
    try:
        async with aiohttp.ClientSession(trust_env=False, timeout=timeout) as session:
            async with session.get(SIGN_IN_URL, proxy=proxy) as resp:
                return f"http_status:{resp.status}"
    except Exception as exc:  # evidence only: report, never hide
        return f"error:{type(exc).__name__}"


def main() -> None:
    env = {k: v for k, v in os.environ.items() if "proxy" in k.lower()}
    result = {
        "label": sys.argv[1] if len(sys.argv) > 1 else "",
        "http_proxy_module": http_proxy.__file__,
        "proxy_variables": env,
        "resolve_proxy(sign-in)": http_proxy.resolve_proxy(SIGN_IN_URL),
        "request": asyncio.run(_request()),
    }
    print("RESULT " + json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
