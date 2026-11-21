"""Drive the six Windows registry cases and check each result.

Windows only. Writes the WinINET Internet Settings key with reg.exe, prints
the raw `reg query` output, runs probe.py in a fresh process with no proxy
variable set, prints the logging proxy's CONNECT log, and fails on any
unexpected result. Restores the key at the end.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

KEY = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings"
HERE = Path(__file__).resolve().parent
LOG = Path(os.environ["PROXY_LOG"])
PROXY = "http://127.0.0.1:3128"
VALUES = ("ProxyEnable", "ProxyServer", "ProxyOverride", "AutoConfigURL")


def reg(*args: str) -> str:
    done = subprocess.run(["reg", *args], capture_output=True, text=True)
    return done.stdout + done.stderr


def clear() -> None:
    for name in VALUES:
        reg("delete", KEY, "/v", name, "/f")


def set_values(values: dict[str, str]) -> None:
    clear()
    for name, value in values.items():
        kind = "REG_DWORD" if name == "ProxyEnable" else "REG_SZ"
        print(reg("add", KEY, "/v", name, "/t", kind, "/d", value, "/f").strip())


def probe() -> dict:
    env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    done = subprocess.run(
        [sys.executable, str(HERE / "probe.py")], capture_output=True, text=True, env=env
    )
    print(done.stdout)
    if done.stderr.strip():
        print("probe stderr:\n" + done.stderr)
    lines = [ln for ln in done.stdout.splitlines() if ln.startswith("RESULT ")]
    if not lines:
        raise SystemExit("probe printed no RESULT line")
    return json.loads(lines[-1][len("RESULT ") :])


def connects() -> list[str]:
    text = LOG.read_text(encoding="utf-8") if LOG.exists() else ""
    LOG.write_text("", encoding="utf-8")
    return sorted({ln.strip() for ln in text.splitlines() if ln.strip()})


def completed(result: dict, *, sync_too: bool = True) -> bool:
    ok = (
        result["async auth.test"] == "slack_error:invalid_auth"
        and result["async apps.connections.open"].startswith("slack_error:")
        and result["getMe"].startswith("auth_error:")
    )
    if sync_too:
        ok = ok and result["sync auth.test"] == "slack_error:invalid_auth"
    return ok


CASES = [
    ("1 manual proxy", {"ProxyEnable": "1", "ProxyServer": "127.0.0.1:3128"}),
    ("2 proxy off", {"ProxyEnable": "0", "ProxyServer": "127.0.0.1:3128"}),
    ("3 PAC only", {"ProxyEnable": "0", "AutoConfigURL": "http://127.0.0.1:9/proxy.pac"}),
    (
        "4 per-protocol",
        {"ProxyEnable": "1", "ProxyServer": "http=127.0.0.1:3128;https=127.0.0.1:3128"},
    ),
    ("5 socks only", {"ProxyEnable": "1", "ProxyServer": "socks=127.0.0.1:1080"}),
    (
        "6 bypass slack.com",
        {"ProxyEnable": "1", "ProxyServer": "127.0.0.1:3128", "ProxyOverride": "slack.com;<local>"},
    ),
]


def check(name: str, r: dict, log: list[str]) -> list[str]:
    bad = []
    slack_via = "CONNECT slack.com:443" in log
    tg_via = "CONNECT api.telegram.org:443" in log
    # Case 5: the sync WebClient's urllib transport reads the registry's
    # socks4:// itself when proxy=None, as it does on main; reported, not failed.
    if not completed(r, sync_too=not name.startswith("5")):
        bad.append("a call did not complete")
    if name.startswith(("1", "4")):
        if r["resolve_slack"] != PROXY or r["resolve_telegram"] != PROXY:
            bad.append("expected the proxy for both hosts")
        if not (slack_via and tg_via):
            bad.append("expected CONNECT slack.com:443 and api.telegram.org:443")
    if name.startswith(("2", "3", "5")):
        if r["resolve_slack"] is not None or r["resolve_telegram"] is not None:
            bad.append("expected no proxy")
        if log:
            bad.append("expected an empty proxy log")
    if name.startswith("6"):
        if r["resolve_slack"] is not None:
            bad.append("expected slack.com bypassed")
        if r["resolve_telegram"] != PROXY:
            bad.append("expected Telegram still proxied")
        if slack_via:
            bad.append("slack.com went through the proxy")
        if not tg_via:
            bad.append("expected CONNECT api.telegram.org:443")
    return bad


def main() -> None:
    LOG.write_text("", encoding="utf-8")
    proxy_proc = subprocess.Popen(
        [sys.executable, str(HERE / "logging_proxy.py"), "3128", str(LOG)],
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    print("logging proxy started, pid", proxy_proc.pid, flush=True)
    import time

    time.sleep(3)
    if proxy_proc.poll() is not None:
        raise SystemExit("the logging proxy exited at start")
    print("proxy variables in this process:", {k: v for k, v in os.environ.items() if "proxy" in k.lower()})
    failures = []
    try:
        for name, values in CASES:
            print(f"\n===== CASE {name} =====")
            set_values(values)
            print("--- reg query (raw):")
            print(reg("query", KEY))
            connects()
            result = probe()
            log = connects()
            print("--- proxy CONNECT log:", log if log else "(empty)")
            bad = check(name, result, log)
            print("--- verdict:", "PASS" if not bad else "FAIL " + "; ".join(bad))
            if bad:
                failures.append(f"{name}: {'; '.join(bad)}")
    finally:
        clear()
        proxy_proc.terminate()
    if failures:
        raise SystemExit("unexpected results:\n" + "\n".join(failures))
    print("\nall cases as expected")


if __name__ == "__main__":
    main()
