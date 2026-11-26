"""Identical-launch test: Harbor 0.27.0's claude-code agent vs da_agent/agent/harbor_claude_code.py.

Run from testbed/ (fake credentials only; nothing is executed):
  uvx --from harbor==0.27.0 python harbor_parity/test_identical_launch.py

It instantiates Harbor's own ClaudeCode agent (claude-haiku-4-5, Claude Code
2.1.311) with a recording environment that answers like the Harbor adapter's
task image before the agent is installed, runs its setup() and run(), and
checks, for each way of passing credentials:
  1. install: the commands Harbor runs to install Claude Code are the RUN lines
     of da_agent/images/claude-code/Dockerfile, byte for byte and in order, run
     as the same user;
  2. the setup and run commands are identical (the instruction variable's
     random suffix aside);
  3. the env of both execs, names and values, is identical, including the
     trial's agent env overlay;
  4. both execs run as the container's default user in the image's WORKDIR
     (no -u, no -w), and the instruction reaches the run exec unchanged;
  5. docker_exec_argv() wraps each command as Harbor's docker environment
     does: bash -c "<pipefail prefix><command>".
Any difference fails.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import importlib.util
import json
import os
import re
import sys
import tempfile
from pathlib import Path

# Harbor reads credentials and options from os.environ; start from nothing.
for _k in list(os.environ):
    if _k.startswith(("ANTHROPIC_", "CLAUDE_", "AWS_")) or _k in {"MAX_THINKING_TOKENS", "DISABLE_PROMPT_CACHING"}:
        del os.environ[_k]

from harbor.agents.installed.claude_code import ClaudeCode  # noqa: E402
from harbor.models.agent.context import AgentContext  # noqa: E402

TESTBED = Path(__file__).resolve().parents[1]
MODULE = TESTBED / "da_agent" / "agent" / "harbor_claude_code.py"
DOCKERFILE = TESTBED / "da_agent" / "images" / "claude-code" / "Dockerfile"

VERSION = "2.1.311"
INSTRUCTION = (
    "Analyse the data and save output.csv.\n\nquotes \" ' `backticks` $HOME ${X} \\ back\\slash\n"
    "unicode: 中文 ✓\n\n### Plotting (REQUIRED)\n\nlast line"
)
UUID_RE = re.compile(r"(claude_code_instruction_|CLAUDE_CODE_INSTRUCTION_)[0-9a-fA-F]{32}")

KEY = "sk-ant-api03-FAKEKEY0123456789abcdef"
TOKEN = "sk-ant-oat01-FAKETOKEN0123456789abcdef"
SCENARIOS = [
    {"name": "parity key + proxy URL", "model": "claude-haiku-4-5",
     "env": {"ANTHROPIC_API_KEY": KEY, "ANTHROPIC_BASE_URL": "https://proxy.example.com/anthropic/"}},
    {"name": "parity key + proxy URL, provider prefix", "model": "anthropic/claude-haiku-4-5",
     "env": {"ANTHROPIC_API_KEY": KEY, "ANTHROPIC_BASE_URL": "https://proxy.example.com"}},
    {"name": "fake key + local recording server", "model": "claude-haiku-4-5",
     "env": {"ANTHROPIC_API_KEY": KEY, "ANTHROPIC_BASE_URL": "http://host.docker.internal:8471"}},
    {"name": "API key, Anthropic's endpoint", "model": "claude-haiku-4-5",
     "env": {"ANTHROPIC_API_KEY": KEY}},
    {"name": "gateway token in ANTHROPIC_AUTH_TOKEN", "model": "claude-haiku-4-5",
     "env": {"ANTHROPIC_AUTH_TOKEN": KEY, "ANTHROPIC_BASE_URL": "https://proxy.example.com"}},
    {"name": "Claude plan token (CLAUDE_FORCE_OAUTH)", "model": "claude-haiku-4-5",
     "env": {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN, "CLAUDE_FORCE_OAUTH": "1"}},
]


class Result:
    def __init__(self, code: int, stdout: str = "", stderr: str = ""):
        self.return_code, self.stdout, self.stderr = code, stdout, stderr


class RecordingEnvironment:
    """Answers like the adapter's task image (python:3.10-bullseye + curl) before the agent install."""

    default_user = None

    def __init__(self):
        self.calls: list[dict] = []

    async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None):
        self.calls.append({"command": command, "cwd": cwd, "env": dict(env or {}), "user": user})
        if "bootstrap.sh" in command:
            return Result(0, f"{VERSION} (Claude Code)\n")
        if command.endswith("claude --version"):  # version probe before install
            return Result(127, "", "claude: command not found")
        if command.startswith("command -v"):
            return Result(1 if re.search(r"\b(node|nodejs|npm)\b", command) else 0)  # checked in the image
        if command.startswith("for manager in"):
            return Result(0, "apt-get")
        return Result(0)

    async def upload_file(self, *a, **k):
        raise AssertionError("unexpected upload")


def load_module():
    spec = importlib.util.spec_from_file_location("harbor_claude_code", MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def norm(s: str) -> str:
    return UUID_RE.sub(lambda m: m.group(1) + "<uuid>", s)


def norm_env(env: dict) -> dict:
    return {norm(k): norm(v) for k, v in env.items()}


def harbor_calls(model: str, env: dict):
    """Harbor's install calls and run() calls, with credentials in os.environ as on the host."""
    saved = dict(os.environ)
    os.environ.update(env)
    try:
        logs = Path(tempfile.mkdtemp(prefix="harbor-test-"))
        agent = ClaudeCode(logs_dir=logs, model_name=model, version=VERSION)
        rec = RecordingEnvironment()

        async def go():
            await agent.setup(rec)
            n_install = len(rec.calls)
            await agent.run(INSTRUCTION, rec, AgentContext())
            return n_install

        n_install = asyncio.run(go())
        # Trial wraps agent setup and run in environment.scoped_exec_env(agent.extra_env)
        # (trial.py), and _merge_env applies persistent < per-exec < scoped.
        overlay = agent.extra_env
        for c in rec.calls:
            c["effective_env"] = {**c["env"], **overlay}
        return rec.calls[:n_install], rec.calls[n_install:]
    finally:
        os.environ.clear()
        os.environ.update(saved)


def dockerfile_runs() -> list[str]:
    out = []
    for line in DOCKERFILE.read_text().splitlines():
        if line.startswith("RUN ["):
            argv = json.loads(line[len("RUN "):])
            assert argv[:2] == ["bash", "-c"], argv
            out.append(argv[2])
    return out


def main() -> int:
    assert importlib.metadata.version("harbor") == "0.27.0", importlib.metadata.version("harbor")
    mod = load_module()
    assert mod.HARBOR_VERSION == "0.27.0" and mod.CLAUDE_CODE_VERSION == VERSION
    failures: list[str] = []

    def check(ok: bool, what: str):
        print(("PASS " if ok else "FAIL ") + what)
        if not ok:
            failures.append(what)

    # 1. install (the same for every scenario)
    install, _ = harbor_calls(SCENARIOS[0]["model"], SCENARIOS[0]["env"])
    probes = [c for c in install
              if c["command"].startswith(("command -v", "for manager in"))
              or (c["command"].endswith("claude --version") and "bootstrap" not in c["command"])]
    changes = [c for c in install if c not in probes]
    print("Harbor install probes (read-only):", [c["command"] for c in probes])
    print("Harbor install commands that change the container:")
    for c in changes:
        print(f"  user={c['user']!r} env={c['env']} :: {c['command']}")
    check([c["command"] for c in changes] == dockerfile_runs(),
          "install: Dockerfile RUN lines == Harbor's install commands, byte for byte, same order")
    check([c["user"] for c in changes] == ["root", "root", "root", None],
          "install: mkdir, apt-get and the musl check as root, Claude Code as the default user (root in both images)")
    check(all(not c["env"] or c["env"] == {"DEBIAN_FRONTEND": "noninteractive"} for c in changes),
          "install: no exec env beyond DEBIAN_FRONTEND=noninteractive (the image ENV sets the same)")

    for sc in SCENARIOS:
        name = sc["name"]
        print(f"\n== scenario: {name}")
        _, calls = harbor_calls(sc["model"], sc["env"])
        check(len(calls) == 2, f"[{name}] Harbor run() makes exactly 2 execs (setup, run)")
        plan = mod.launch_plan(prompt=INSTRUCTION, model=sc["model"], source=dict(sc["env"]))
        for hc, step in zip(calls, plan):
            sname = step["name"]
            h_cmd = norm(hc["command"])
            o_cmd = norm(mod.EXEC_PREFIX + step["command"])
            check(h_cmd == o_cmd, f"[{name}] {sname}: command identical")
            if h_cmd != o_cmd:
                print("   harbor:", h_cmd, "\n   ours:  ", o_cmd)
            h_env, o_env = norm_env(hc["effective_env"]), norm_env(step["env"])
            diff = sorted(k for k in set(h_env) | set(o_env) if h_env.get(k) != o_env.get(k))
            check(not diff, f"[{name}] {sname}: env names and values identical ({len(h_env)} variables)")
            for k in diff:
                print(f"   {k}: harbor={h_env.get(k)!r} ours={o_env.get(k)!r}")
            check(hc["cwd"] is None and hc["user"] is None,
                  f"[{name}] {sname}: default user and WORKDIR (Harbor passes no -u, no -w)")
            argv = mod.docker_exec_argv("c", step)
            check(argv[-3:] == ["bash", "-c", mod.EXEC_PREFIX + step["command"]]
                  and argv[2:-4:2] == ["-e"] * len(step["env"]) and argv[3:-4:2] == list(step["env"])
                  and argv[-4] == "c" and "-u" not in argv and "-w" not in argv,
                  f"[{name}] {sname}: docker exec -e <each name> c bash -c <prefix + command>")
            if sname == "run":
                var = [k for k in hc["effective_env"] if k.startswith("CLAUDE_CODE_INSTRUCTION_")]
                ovar = [k for k in step["env"] if k.startswith("CLAUDE_CODE_INSTRUCTION_")]
                check(len(var) == len(ovar) == 1
                      and hc["effective_env"][var[0]] == step["env"][ovar[0]] == INSTRUCTION,
                      f"[{name}] run: instruction passed unchanged in CLAUDE_CODE_INSTRUCTION_<uuid>")
        print("   env names:", sorted(norm_env(calls[-1]["effective_env"])))

    print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'ALL PASS'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
