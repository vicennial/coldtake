"""Fake provider executables and a fake OpenRouter transport. No real CLI, network,
home directory or login is ever touched."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path
from typing import Any

import httpx

FAKE_SCRIPT = r"""#!{python}
import json, os, sys, time
D, KIND = {dir!r}, {kind!r}
args = sys.argv[1:]
stdin = sys.stdin.read()
role = "judge" if "COLDTAKE_ROLE: judge" in stdin else "advisor"
schema = None
if "--output-schema" in args:
    schema = json.load(open(args[args.index("--output-schema") + 1]))
elif "--json-schema" in args:
    schema = json.loads(args[args.index("--json-schema") + 1])
with open(os.path.join(D, "capture.jsonl"), "a") as handle:
    handle.write(json.dumps({{"kind": KIND, "role": role, "stdin": stdin, "argv": args,
                              "schema": schema}}) + "\n")
scenario = json.load(open(os.path.join(D, "scenario.json")))
specs = scenario.get(role) or scenario.get("default") or [{{"error": "no scenario", "code": 9}}]
counter = os.path.join(D, "count-" + role)
n = int(open(counter).read()) if os.path.exists(counter) else 0
open(counter, "w").write(str(n + 1))
spec = specs[min(n, len(specs) - 1)]

def judge_auto(params):
    positions = json.loads(stdin.split("<COMPARE_JSON untrusted=\"true\">")[1].split("</COMPARE_JSON>")[0])["positions"]
    labels = sorted(k for k in positions if k != "S")
    clusters = [labels] if params.get("clusters", "one") == "one" else [[l] for l in labels]
    rel = "agree" if len(clusters) == 1 else "conflict"
    pairs = [{{"a": a, "b": b, "relation": rel, "crux": "c"}} for i, a in enumerate(labels) for b in labels[i + 1:]]
    sealed = None
    if "S" in positions:
        s = params.get("sealed", "agree")
        pairs += [{{"a": l, "b": "S", "relation": s, "crux": "c"}} for l in labels]
        sealed = {{"relation": params.get("overall_sealed", s), "crux": "c"}}
    if params.get("drop_pair"):
        pairs = pairs[1:]
    if params.get("drop_sealed_pair"):
        pairs = pairs[:-1]
    if params.get("dup_pair"):
        pairs = pairs + pairs[:1]
    contradictions = [{{"claim": "x", "labels": [labels[0], labels[0]]}}] if params.get("repeat_label") else []
    positions_out = [{{"label": l, "stance": "stance " + l}} for l in labels]
    if params.get("drop_position"):
        positions_out = positions_out[1:]
    if "S" in positions and not params.get("no_s_position"):
        positions_out.append({{"label": "S", "stance": "the sealed answer"}})
    if params.get("dup_s_position") and "S" in positions:
        positions_out.append({{"label": "S", "stance": "again"}})
    return {{"positions": positions_out,
            "clusters": clusters, "pairwise": pairs, "sealed": sealed,
            "contradictions": contradictions, "material": False}}

if "sleep" in spec:
    time.sleep(spec["sleep"])
    spec = spec.get("then") or {{"error": "slept", "code": 3}}
if "flood" in spec:
    sys.stdout.write("x" * spec["flood"]); sys.stdout.flush()
    time.sleep(spec.get("then_sleep", 0))
    sys.exit(0)
if "real_shape" in spec:
    # What codex-cli prints for a failed run: banner, the whole prompt echoed after a
    # `user` line, a warning, then the ERROR line (twice), and a non-zero exit.
    out = sys.stdout if spec.get("stream") == "stdout" else sys.stderr
    out.write("OpenAI Codex v0.170.0 (research preview)\n--------\n")
    out.write("workdir: /tmp/x\nmodel: gpt\nsandbox: read-only\n--------\n")
    out.write("user\n" + stdin + "\n")
    out.write("warning: bubblewrap is not available; using a weaker sandbox\n")
    for _ in range(2):
        out.write(spec["real_shape"] + "\n")
    out.flush()
    sys.exit(spec.get("code", 1))
if "error" in spec:
    sys.stderr.write(spec["error"]); sys.exit(spec.get("code", 1))
if "claude_error" in spec and KIND == "claude":
    print(json.dumps({{"is_error": True, "result": spec["claude_error"]}})); sys.exit(1)
if spec.get("garbage"):
    out = "this is not json"
else:
    data = judge_auto(spec["judge_auto"]) if "judge_auto" in spec else spec.get("json")
    out = json.dumps(data) if data is not None else spec.get("stdout", "")
if KIND == "claude":
    print(json.dumps({{"is_error": False, "result": "", "structured_output": json.loads(out)}}) if not spec.get("garbage") else out)
else:
    path = args[args.index("-o") + 1]
    open(path, "w").write(out)
"""


class FakeCLI:
    """An executable that records every stdin and replays a scenario."""

    def __init__(self, directory: Path, kind: str):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self.kind = kind
        self.path = self.dir / kind
        self.path.write_text(
            FAKE_SCRIPT.format(python=sys.executable, dir=str(self.dir), kind=kind)
        )
        self.path.chmod(self.path.stat().st_mode | stat.S_IXUSR)
        self.script(default=[{"error": "unscripted fake", "code": 9}])

    def script(self, **roles: list[dict[str, Any]]) -> None:
        for counter in self.dir.glob("count-*"):
            counter.unlink()
        (self.dir / "scenario.json").write_text(json.dumps(roles))

    def calls(self, role: str | None = None) -> list[dict[str, Any]]:
        capture = self.dir / "capture.jsonl"
        if not capture.exists():
            return []
        rows = [json.loads(line) for line in capture.read_text().splitlines()]
        return [r for r in rows if role is None or r["role"] == role]


class FakeRouter:
    """httpx MockTransport standing in for OpenRouter. Replays `responses`; an entry may
    be a Response, an Exception to raise, or a dict returned as a 200 completion."""

    def __init__(self, *responses: Any):
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        item = self.responses[min(len(self.requests) - 1, len(self.responses) - 1)]
        if isinstance(item, Exception):
            raise item
        if isinstance(item, httpx.Response):
            return item
        return completion(item)


def completion(data: Any, cost: float | None = 0.01, finish: str = "stop") -> httpx.Response:
    body = {
        "model": "fake/model",
        "choices": [{"finish_reason": finish, "message": {"content": json.dumps(data)}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "cost": cost},
    }
    return httpx.Response(200, json=body)


def memo(recommendation: str = "Do A", **overrides: Any) -> dict[str, Any]:
    base = {
        "framing": "framing",
        "recommendation": recommendation,
        "best_alternative": "Do B",
        "evidence": ["fact one"],
        "assumptions": ["costs stay stable"],
        "value_tradeoffs": ["speed versus cost"],
        "forecasts": ["demand grows"],
        "feasibility": ["two weeks"],
        "missing_information": [],
        "failure_modes": ["delay"],
        "reversal_evidence": ["B becomes cheaper"],
        "confidence": 0.7,
    }
    return {**base, **overrides}


def choice_memo(choice: str, reason: str = "because", **overrides: Any) -> dict[str, Any]:
    return memo(f"Pick {choice}", choice=choice, choice_reason=reason, **overrides)
