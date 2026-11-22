"""List the CommandCode models with what the errand agent can actually use them for.

`/models` reports three endpoint combinations and only `/chat/completions` works with
`HttpLLMClient`. Reporting 104 models without that distinction would offer 12 that
cannot be driven by this agent at all.

    python scripts/list_models.py            # all, grouped, with a use column
    python scripts/list_models.py --chat     # only the ones this client can drive
    python scripts/list_models.py --pick     # choose interactively, print the env line
"""
from __future__ import annotations

import json
import pathlib
import re
import ssl
import sys
import urllib.request

BASE = "https://api.commandcode.ai/provider/v1"
CONFIG = pathlib.Path.home() / ".hermes" / "config.yaml"


def _key() -> str:
    text = CONFIG.read_text()
    block = text.split("name: Api.commandcode.ai", 1)[1]
    for line in block.splitlines():
        if line.strip().startswith("api_key:"):
            return line.split(":", 1)[1].strip()
    raise SystemExit("no api_key in the commandcode block of ~/.hermes/config.yaml")


def _ssl() -> ssl.SSLContext:
    candidates = [
        ssl.get_default_verify_paths().cafile,
        "/etc/ssl/cert.pem",
        "/opt/homebrew/etc/openssl@3/cert.pem",
        "/usr/local/etc/openssl@3/cert.pem",
    ]
    for path in candidates:
        if path and pathlib.Path(path).exists():
            return ssl.create_default_context(cafile=path)
    return ssl.create_default_context()


def fetch() -> list[dict]:
    req = urllib.request.Request(
        f"{BASE}/models",
        headers={
            "Authorization": f"Bearer {_key()}",
            "Accept": "application/json",
            # Cloudflare 403s the default urllib UA with code 1010.
            "User-Agent": "errand-agent/0.1 (+python-urllib)",
        },
    )
    with urllib.request.urlopen(req, timeout=30, context=_ssl()) as resp:
        return json.loads(resp.read())["data"]


def can_chat(m: dict) -> bool:
    return "/chat/completions" in (m.get("supported_endpoints") or [])


def family(m: dict) -> str:
    mid = m["id"]
    return mid.split("/")[0] if "/" in mid else mid.split("-")[0]


def main(argv: list[str]) -> int:
    models = fetch()
    chat = [m for m in models if can_chat(m)]

    if "--chat" in argv:
        models = chat

    if "--pick" in argv:
        ranked = sorted(chat, key=lambda m: (family(m), m["id"]))
        for index, m in enumerate(ranked, 1):
            ctx = m.get("context_length") or 0
            print(f"{index:3d}  {m['id']:52s} {ctx // 1000:>5}k ctx  {m.get('name', '')}")
        choice = input("\nnumber, or a model id: ").strip()
        if choice.isdigit():
            picked = ranked[int(choice) - 1]["id"]
        else:
            picked = choice
        if picked not in {m["id"] for m in chat}:
            print(f"\n{picked!r} does not support /chat/completions; "
                  "the errand agent cannot drive it.")
            return 1
        print("\nexport ERRAND_LLM_BASE_URL=" + BASE)
        print("export ERRAND_LLM_KEY=<the key already in config.yaml>")
        print(f"export ERRAND_LLM_MODEL={picked}")
        print("\npython -m errand.cli --llm real")
        return 0

    groups: dict[str, list[dict]] = {}
    for m in models:
        groups.setdefault(family(m), []).append(m)

    usable = 0
    for name in sorted(groups):
        rows = sorted(groups[name], key=lambda m: m["id"])
        print(f"\n{name}  ({len(rows)})")
        for m in rows:
            ok = "yes" if can_chat(m) else "no  (/messages only)"
            if can_chat(m):
                usable += 1
            ctx = (m.get("context_length") or 0) // 1000
            print(f"  {m['id']:48s} {ctx:>5}k  chat={ok}")

    print(f"\n{len(models)} models shown, {usable} drivable by HttpLLMClient "
          f"(/chat/completions).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
