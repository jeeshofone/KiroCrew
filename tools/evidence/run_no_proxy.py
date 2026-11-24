"""Drive the NO_PROXY-only case on Windows or macOS, for the PR head and main.

Points the OS system proxy at the logging CONNECT proxy (Windows: the WinINET
Internet Settings key; macOS: networksetup on every network service), then
runs no_proxy_only.py in a fresh process for each source tree:
  - with NO_PROXY=localhost,127.0.0.1 as the only proxy variable;
  - control: with no proxy variable at all.
Prints each RESULT line and the CONNECT log after it. Restores the settings.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG = Path(os.environ["PROXY_LOG"])
TREES = {"head": os.environ["HEAD_SRC"], "main": os.environ["MAIN_SRC"]}
WIN_KEY = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings"


def run(*args: str) -> str:
    done = subprocess.run(list(args), capture_output=True, text=True)
    return (done.stdout + done.stderr).strip()


def mac_services() -> list[str]:
    out = run("networksetup", "-listallnetworkservices").splitlines()
    return [s.lstrip("*").strip() for s in out[1:] if s.strip()]


def set_os_proxy(on: bool) -> None:
    if sys.platform == "win32":
        if on:
            print(run("reg", "add", WIN_KEY, "/v", "ProxyEnable", "/t", "REG_DWORD", "/d", "1", "/f"))
            print(run("reg", "add", WIN_KEY, "/v", "ProxyServer", "/t", "REG_SZ", "/d", "127.0.0.1:3128", "/f"))
        else:
            for name in ("ProxyEnable", "ProxyServer"):
                run("reg", "delete", WIN_KEY, "/v", name, "/f")
        print(run("reg", "query", WIN_KEY))
        return
    for svc in mac_services():
        if on:
            run("sudo", "networksetup", "-setwebproxy", svc, "127.0.0.1", "3128")
            run("sudo", "networksetup", "-setsecurewebproxy", svc, "127.0.0.1", "3128")
        else:
            run("sudo", "networksetup", "-setwebproxystate", svc, "off")
            run("sudo", "networksetup", "-setsecurewebproxystate", svc, "off")
        print(f"[{svc}]", run("networksetup", "-getsecurewebproxy", svc).replace("\n", " | "))
    print("scutil --proxy:\n" + run("scutil", "--proxy"))


def probe(tree: str, label: str, no_proxy: bool) -> dict:
    env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    if no_proxy:
        env["NO_PROXY"] = "localhost,127.0.0.1"
    env["PYTHONPATH"] = str(Path(TREES[tree]) / "src")
    done = subprocess.run(
        [sys.executable, str(HERE / "no_proxy_only.py"), label],
        capture_output=True,
        text=True,
        env=env,
    )
    print(done.stdout.strip())
    if done.stderr.strip():
        print("stderr:", done.stderr.strip()[-1500:])
    lines = [ln for ln in done.stdout.splitlines() if ln.startswith("RESULT ")]
    return json.loads(lines[-1][len("RESULT ") :]) if lines else {}


def connects() -> list[str]:
    text = LOG.read_text(encoding="utf-8") if LOG.exists() else ""
    LOG.write_text("", encoding="utf-8")
    return sorted({ln.strip() for ln in text.splitlines() if ln.strip()})


def main() -> None:
    LOG.write_text("", encoding="utf-8")
    proxy = subprocess.Popen([sys.executable, str(HERE / "logging_proxy.py"), "3128", str(LOG)])
    time.sleep(3)
    if proxy.poll() is not None:
        raise SystemExit("logging proxy exited at start")
    set_os_proxy(True)
    try:
        for tree in ("head", "main"):
            for no_proxy in (True, False):
                label = f"{tree} {'NO_PROXY only' if no_proxy else 'no proxy variable (control)'}"
                print(f"\n===== {label} =====")
                connects()
                probe(tree, label, no_proxy)
                log = connects()
                print("--- proxy CONNECT log:", log if log else "(empty)")
    finally:
        set_os_proxy(False)
        proxy.terminate()


if __name__ == "__main__":
    main()
