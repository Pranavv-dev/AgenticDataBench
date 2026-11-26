"""Claude Code started exactly as Harbor 0.27.0's ``claude-code`` agent starts it.

Used for the Harbor parity experiment. The original side keeps upstream's own
pipeline (run.py: the task container, the work dir, the prompt and the output
collection; evaluate.py: the grading). Only how the Claude Code CLI is installed
and launched comes from Harbor, so both sides send the model the same requests.

Source of truth (harbor==0.27.0):
  - agents/installed/claude_code.py ``ClaudeCode.run()``: the env, the setup
    command and the run command; ``_resolve_auth_env()`` and
    ``_model_alias_env()``: credentials and the ANTHROPIC_DEFAULT_*_MODEL /
    CLAUDE_CODE_SUBAGENT_MODEL aliases.
  - agents/installed/base.py ``BaseInstalledAgent._exec()``: the
    ``set -o pipefail`` prefix.
  - environments/docker/docker.py ``_compose_exec()``: one ``-e`` per variable,
    no ``-u`` (the image's default user, root) and no ``-w`` (the image's
    WORKDIR, /workspace), then ``bash -c <command>``.
  - ``ClaudeCode.install()`` is baked into images/claude-code/Dockerfile.
harbor_parity/test_identical_launch.py runs Harbor's own ClaudeCode class
against a recording environment and fails on any difference from
``launch_plan()`` below or from the Dockerfile's install steps.

Credentials are read from the given environment the way Harbor reads them from
the host's: ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN), ANTHROPIC_BASE_URL,
CLAUDE_CODE_OAUTH_TOKEN and CLAUDE_FORCE_OAUTH. Harbor options that would change
the launch (an effort level, a turn or token limit, Bedrock) are refused rather
than copied: the parity runs use none of them.
"""

from __future__ import annotations

import uuid
from typing import Dict, List, Mapping, Optional

HARBOR_VERSION = "0.27.0"
CLAUDE_CODE_VERSION = "2.1.311"

# Harbor's EnvironmentPaths.agent_dir in a Linux container.
LOGS_DIR = "/logs/agent"

# base.py BaseInstalledAgent._exec: every command is prefixed with this.
EXEC_PREFIX = "(set -o pipefail) 2>/dev/null && set -o pipefail; "

# claude_code.py ClaudeCode.run(): the setup command with no settings file, no
# skills, no memory dir and no MCP servers (the adapter uses none of them).
SETUP_COMMAND = (
    "mkdir -p $CLAUDE_CONFIG_DIR/debug $CLAUDE_CONFIG_DIR/projects/-app "
    "$CLAUDE_CONFIG_DIR/shell-snapshots $CLAUDE_CONFIG_DIR/statsig "
    "$CLAUDE_CONFIG_DIR/todos $CLAUDE_CONFIG_DIR/skills && "
    "if [ -d ~/.claude/skills ]; then "
    "cp -r ~/.claude/skills/. $CLAUDE_CONFIG_DIR/skills/ 2>/dev/null || true; "
    "fi"
)

# ClaudeCodeOptions defaults turned into CLI flags (build_cli_flags()): only
# permission_mode has a default.
CLI_FLAGS = "--permission-mode=bypassPermissions"

# claude_code.py _model_alias_env(all_aliases=True).
MODEL_ALIAS_NAMES = (
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
)

# Settings that would make Harbor add flags or variables this module does not
# reproduce. The parity runs leave them unset on both sides.
UNSUPPORTED = (
    "CLAUDE_CODE_EFFORT_LEVEL",
    "CLAUDE_CODE_MAX_TURNS",
    "MAX_THINKING_TOKENS",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
    "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING",
    "CLAUDE_CODE_USE_BEDROCK",
    "AWS_BEARER_TOKEN_BEDROCK",
)


def _flag(source: Mapping[str, str], name: str) -> bool:
    """harbor.utils parse_bool_env_value for an optional flag (default False)."""
    raw = source.get(name)
    if raw is None or not raw.strip():
        return False
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean, not {raw!r}")


def model_id(model: str) -> str:
    """The ANTHROPIC_MODEL value Harbor sets for -m <model> ("anthropic/x" -> "x")."""
    provider, _, name = model.partition("/")
    if not name:
        return model
    if provider != "anthropic":
        raise ValueError(f"Only Anthropic models are supported, not {model!r}")
    return name


def harbor_env(*, model: str, source: Mapping[str, str]) -> Dict[str, str]:
    """The ``env`` Harbor passes to the setup and run execs (run() + _resolve_auth_env())."""
    set_unsupported = [n for n in UNSUPPORTED if source.get(n)]
    if set_unsupported:
        raise ValueError(f"Unset {', '.join(set_unsupported)}: the Harbor launch copied here does not use them")

    force_oauth = _flag(source, "CLAUDE_FORCE_OAUTH")
    oauth_token = (source.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip()
    if force_oauth and not oauth_token:
        raise RuntimeError("CLAUDE_FORCE_OAUTH is set but CLAUDE_CODE_OAUTH_TOKEN is not")

    # Harbor's model connection: the key comes from ANTHROPIC_API_KEY, else
    # ANTHROPIC_AUTH_TOKEN, and goes back under the same name; a base URL given
    # by the user is passed verbatim. CLAUDE_FORCE_OAUTH drops the key.
    api_key = auth_token = ""
    if not force_oauth:
        if "ANTHROPIC_API_KEY" in source:
            api_key = source["ANTHROPIC_API_KEY"]
        elif "ANTHROPIC_AUTH_TOKEN" in source:
            auth_token = source["ANTHROPIC_AUTH_TOKEN"]
    base_url = source.get("ANTHROPIC_BASE_URL") or ""

    auth_env = {
        "ANTHROPIC_API_KEY": api_key,
        "ANTHROPIC_BASE_URL": base_url,
        "CLAUDE_CODE_OAUTH_TOKEN": oauth_token,
    }
    env = {k: v for k, v in auth_env.items() if v}
    env["ANTHROPIC_AUTH_TOKEN"] = auth_token

    # run()
    resolved_model = model_id(model) if model else (source.get("ANTHROPIC_MODEL") or "")
    if resolved_model:
        env["ANTHROPIC_MODEL"] = resolved_model
    if "ANTHROPIC_BASE_URL" in env and "ANTHROPIC_MODEL" in env:
        for name in MODEL_ALIAS_NAMES:
            env[name] = source[name] if name in source else env["ANTHROPIC_MODEL"]
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    env["IS_SANDBOX"] = "1"
    env["CLAUDE_CONFIG_DIR"] = f"{LOGS_DIR}/sessions"
    return env


def run_command(instruction_var: str) -> str:
    """claude_code.py ClaudeCode.run(): the launch (no --settings, no --resume)."""
    env_var = instruction_var.upper()
    return (
        'export PATH="$HOME/.local/bin:$PATH"; '
        f'{instruction_var}="${env_var}"; '
        f"unset {env_var}; "
        f'printf "%s" "${instruction_var}" | '
        f"claude --verbose --output-format=stream-json "
        f"{CLI_FLAGS} "
        f"--print 2>&1 | tee "
        f"{LOGS_DIR}/claude-code.txt"
    )


def launch_plan(*, prompt: str, model: str, source: Mapping[str, str]) -> List[Dict[str, object]]:
    """The two execs Harbor's run() makes, without running them.

    Each step: ``command`` (to run as ``bash -c EXEC_PREFIX + command``) and
    ``env`` (the variables passed with ``-e``).
    """
    env = harbor_env(model=model, source=source)
    instruction_var = f"claude_code_instruction_{uuid.uuid4().hex}"
    return [
        {"name": "setup", "command": SETUP_COMMAND, "env": env},
        {"name": "run", "command": run_command(instruction_var), "env": {**env, instruction_var.upper(): prompt}},
    ]


def docker_exec_argv(container: str, step: Mapping[str, object]) -> List[str]:
    """``docker exec`` for one step, like Harbor's ``docker compose exec``.

    Each variable is passed as ``-e NAME`` with its value in the docker client's
    own environment (see ``docker_exec_env``), so keys and the prompt never
    appear on a command line. The container receives NAME=value either way.
    """
    argv = ["docker", "exec"]
    for name in step["env"]:
        argv += ["-e", name]
    return argv + [container, "bash", "-c", EXEC_PREFIX + str(step["command"])]


def docker_exec_env(base: Mapping[str, str], step: Mapping[str, object]) -> Dict[str, str]:
    """The docker client's environment for ``docker_exec_argv``: its own plus the step's values."""
    return {**base, **step["env"]}


def credentials_source(environ: Mapping[str, str], fallback: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Where credentials come from: the environment, as on Harbor.

    If it holds no credential at all, upstream's defaults from utils.config
    (``fallback``) are used instead, as the runner did before.
    """
    source = dict(environ)
    if fallback and not any(source.get(n) for n in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")):
        source.update({k: v for k, v in fallback.items() if v})
    return source
