#!/usr/bin/env python3
"""Smoke-test the Auto/On/Off thinking toggle against a real provider.

Requires the SciKick dev server running on :8742.

Usage:
  python3 smoke_thinking.py <provider> <model> [--base-url URL]

For each thinking mode (auto, off, on) it configures the provider/model via
/chat/configure (in-memory, persist:false), then sends a trivial message
("hi") and an analytical one, and reports whether a "thinking" marker
appeared in the SSE stream.

Expected (for a toggle-capable reasoning model):
  auto + trivial    -> no thinking marker (fast)
  auto + analytical -> thinking marker (CoT)
  off  + either     -> no thinking marker
  on   + either     -> thinking marker

For models NOT in _MODEL_THINKING_FAMILY (e.g. gpt-4o) every mode behaves
identically (no thinking param ever sent) — a quick way to confirm gating.
"""
import json
import sys
import urllib.request

SERVER = "http://localhost:8742"
ANALYTICAL = "Why did the authors choose this experimental approach? What are its implications?"

# Known OpenAI-compat base URLs (fall back to provider defaults on the server;
# only pass --base-url for unusual setups).
DEFAULTS = {
    "deepseek": "https://api.deepseek.com",
    "glm": "https://open.bigmodel.cn/api/paas/v4",
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "kimi": "https://api.moonshot.cn/v1",
    "grok": "https://api.x.ai/v1",
    "minimax": "https://api.minimax.io/v1",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
}


def _post(path, body, timeout=300):
    req = urllib.request.Request(
        f"{SERVER}{path}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _send(message, timeout=300):
    req = urllib.request.Request(
        f"{SERVER}/chat/send", data=json.dumps({"message": message}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
    events = []
    for line in raw.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: "):].strip()))
    return events


def run_case(provider, model, mode, base_url):
    body = {"provider": provider, "model": model, "thinking_mode": mode, "persist": False}
    if base_url:
        body["base_url"] = base_url
    cur = _post("/chat/configure", body)["current"]
    capable = cur.get("thinking_capable")

    trivial = _send("hi")
    analytic = _send(ANALYTICAL)

    def summarize(events, label):
        types = [e["type"] for e in events]
        text = next((e.get("content", "") for e in events if e["type"] == "text"), "")[:90].replace("\n", " ")
        return (f"{label:10} thinking={'THINK' if 'thinking' in types else '----'}  "
                f"types={types}  text={text!r}")

    print(f"\n[ {provider}/{model}  mode={mode}  capable={capable} ]")
    print("  " + summarize(trivial, "'hi'"))
    print("  " + summarize(analytic, "analytical"))


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    provider, model = sys.argv[1], sys.argv[2]
    base_url = None
    if "--base-url" in sys.argv:
        base_url = sys.argv[sys.argv.index("--base-url") + 1]
    for mode in ("auto", "off", "on"):
        try:
            run_case(provider, model, mode, base_url)
        except Exception as exc:
            print(f"\n[ {provider}/{model}  mode={mode} ] FAILED: {exc}")


if __name__ == "__main__":
    main()
