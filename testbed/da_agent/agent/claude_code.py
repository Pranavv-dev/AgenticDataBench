"""
Claude Code Agent Module - Agent implementation driving the Claude Code CLI.

This module provides an agent that:
- Starts the `claude` CLI inside the agent's Docker container exactly as
  Harbor's claude-code agent does (harbor_claude_code.py), with a timeout and
  Anthropic API credentials from this process's environment
- Parses Claude Code's stream-json (or json) output into a normalized trajectory,
  capturing per-step timing, tool uses, and token usage
"""

import json
import logging
import os
import subprocess
import threading
import time
from da_agent.envs.da_agent import DA_Agent_Env
from da_agent.agent.base import BaseAgent
from da_agent.agent import harbor_claude_code
from utils.config import ANTHROPIC_API_KEY, ANTHROPIC_BASE_URL

logger = logging.getLogger("da_agent")

DEFAULT_TIME_OUT = 3600  # 60 minutes


class PromptAgent(BaseAgent):
    # Claude Code drives the `claude` CLI; only model is used from the shared
    # config (the timeout uses DEFAULT_TIME_OUT instead of max_steps). No
    # cross-task state, so __init__ is inherited from BaseAgent.

    def set_env_and_task(self, env: DA_Agent_Env):
        self.env = env
        self.instruction = self.env.task_config['question']
        self.trajectory = []
        self.raw_output = ""
        self.event_timestamps = []

    def _build_task_prompt(self):
        task = self.instruction
        task += f"\n\nYou are working in the directory: {self.work_dir}."
        task += " All required data files are available in this directory."
        task += " Complete the task and ensure all output files are saved in this directory."

        image_file_names = self._get_image_file_names()
        if image_file_names:
            task += self._build_plotting_instructions(image_file_names)

        return task

    def _get_image_file_names(self):
        image_file_names = []
        for post_process_f in self.env.post_process_func:
            def image_post_process(output_file_name):
                if output_file_name in self.env.task_config.get('output_file_name', []):
                    return output_file_name
                return None
            output_file_name = eval(post_process_f)
            if output_file_name:
                image_file_names.append(output_file_name)
        return image_file_names

    def _build_plotting_instructions(self, image_file_names):
        return f"""
### Plotting (REQUIRED)

If you create a matplotlib plot, you MUST call:

    from image import Plotprocess
    Plotprocess.plot_process(fig, "<image_file_name>")

Use ONLY these file names:
{", ".join(image_file_names)}

Rules:
- Call AFTER plotting is complete
- Call BEFORE saving the figure
- Use: fig = plt.gcf()
- Replace <image_file_name> with one from the list above

Example:
```python
from image import Plotprocess
import matplotlib.pyplot as plt

# plotting code ...

fig = plt.gcf()
Plotprocess.plot_process(fig, "{image_file_names[0]}")
```"""

    def _exec_step(self, container_name, step, deadline, on_line=None):
        """Run one Harbor exec (harbor_claude_code.launch_plan step) in the container.

        Returns (exit code or None on timeout, output lines). On timeout every
        process the agent started in the container is killed, as Harbor's
        agent timeout ends the agent phase.
        """
        process = subprocess.Popen(
            harbor_claude_code.docker_exec_argv(container_name, step),
            env=harbor_claude_code.docker_exec_env(os.environ, step),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        timed_out = threading.Event()

        def on_timeout():
            timed_out.set()
            subprocess.run(["docker", "exec", str(container_name), "bash", "-c", "kill -KILL -1"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            process.kill()

        timer = threading.Timer(max(0.0, deadline - time.time()), on_timeout)
        timer.daemon = True
        timer.start()
        lines = []
        try:
            for line in iter(process.stdout.readline, b""):
                decoded = line.decode("utf-8", errors="ignore")
                lines.append(decoded)
                if on_line:
                    on_line(decoded)
            process.wait()
        finally:
            timer.cancel()
        return (None if timed_out.is_set() else process.returncode), lines

    def run(self):
        """Start Claude Code exactly as Harbor's claude-code agent does.

        Harbor's setup exec, then its launch exec (prompt piped on stdin, logs
        and CLAUDE_CONFIG_DIR under /logs/agent), as the container's default
        user (root), with credentials from this process's environment. See
        harbor_claude_code.py. Upstream ran ``claude -p <prompt> --model ...
        --dangerously-skip-permissions`` as a non-root user instead.
        """
        assert self.env is not None, "Environment is not set."

        task_prompt = self._build_task_prompt()
        container_name = self.env.container.name
        source = harbor_claude_code.credentials_source(
            os.environ, {"ANTHROPIC_API_KEY": ANTHROPIC_API_KEY, "ANTHROPIC_BASE_URL": ANTHROPIC_BASE_URL})
        plan = harbor_claude_code.launch_plan(prompt=task_prompt, model=self.model, source=source)
        deadline = time.time() + DEFAULT_TIME_OUT

        self.event_timestamps = []

        def on_line(decoded):
            self.event_timestamps.append(time.time())
            logger.debug("Claude Code: %s", decoded.strip())

        result = (True, "Task completed")
        output_lines = []
        try:
            for step in plan:
                code, lines = self._exec_step(container_name, step, deadline,
                                              on_line=on_line if step["name"] == "run" else None)
                if step["name"] == "run":
                    output_lines = lines
                if code is None:
                    result = (False, f"Timeout after {DEFAULT_TIME_OUT}s")
                    break
                if code != 0:
                    result = (False, f"Agent exited with code {code}" if step["name"] == "run"
                              else f"Setup exited with code {code}: {''.join(lines)[:2000]}")
                    break
        except Exception as e:
            logger.error("Error running Claude Code: %s", e)
            result = (False, f"Error: {e}")

        self.raw_output = "".join(output_lines)
        self._parse_trajectory()
        return result

    def _parse_trajectory(self):
        self.trajectory = []

        # Try parsing as a single JSON array (--output-format json)
        try:
            entries = json.loads(self.raw_output.strip())
            if isinstance(entries, list):
                for entry in entries:
                    normalized = self._normalize_entry(entry)
                    self.trajectory.append(normalized)
                return
        except json.JSONDecodeError:
            pass

        # Fall back to JSONL parsing (--output-format stream-json)
        lines = self.raw_output.strip().split("\n")
        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                normalized = self._normalize_entry(entry)
                # Add timing info from recorded timestamps (stream-json only)
                if i < len(self.event_timestamps):
                    ts = self.event_timestamps[i]
                    prev_ts = self.event_timestamps[i - 1] if i > 0 else ts
                    normalized["timing"] = {
                        "start_time": prev_ts,
                        "end_time": ts,
                        "duration": ts - prev_ts,
                    }
                self.trajectory.append(normalized)
            except json.JSONDecodeError:
                step = {"type": "raw", "content": line}
                if i < len(self.event_timestamps):
                    ts = self.event_timestamps[i]
                    prev_ts = self.event_timestamps[i - 1] if i > 0 else ts
                    step["timing"] = {
                        "start_time": prev_ts,
                        "end_time": ts,
                        "duration": ts - prev_ts,
                    }
                self.trajectory.append(step)

    def _normalize_entry(self, entry):
        entry_type = entry.get("type", "unknown")

        if entry_type == "system":
            subtype = entry.get("subtype", "")
            if subtype == "init":
                return {
                    "type": "system_init",
                    "session_id": entry.get("session_id", ""),
                    "model": entry.get("model", ""),
                    "cwd": entry.get("cwd", ""),
                }
            return {"type": "system", "subtype": subtype}

        if entry_type == "assistant":
            message = entry.get("message", {})
            msg_id = message.get("id", "")
            content = message.get("content", [])
            text_parts = []
            code_action = None
            tool_uses = []

            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    tool_name = block.get("name", "")
                    tool_input = block.get("input", {})
                    tool_uses.append({"name": tool_name, "input": tool_input})
                    if tool_name == "Bash" and "command" in tool_input:
                        code_action = tool_input["command"]

            result = {"type": "assistant", "content": "\n".join(text_parts)}
            if msg_id:
                result["msg_id"] = msg_id
            if tool_uses:
                result["tool_uses"] = tool_uses
            if code_action:
                result["code_action"] = code_action
            # Extract per-message usage if available
            usage = message.get("usage", {})
            if usage:
                result["usage"] = usage
            return result

        elif entry_type == "tool_result":
            content = entry.get("content", "")
            if isinstance(content, list):
                text_parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                content = "\n".join(text_parts)
            result = {"type": "tool_result", "observations": f"Execution logs:\n{content}"}
            return result

        elif entry_type == "result":
            result_entry = {
                "type": "result",
                "subtype": entry.get("subtype", ""),
                "is_error": entry.get("is_error", False),
                "result": entry.get("result", ""),
                "stop_reason": entry.get("stop_reason", ""),
                "duration_ms": entry.get("duration_ms", 0),
                "num_turns": entry.get("num_turns", 0),
            }
            # Extract aggregate usage and Claude Code's own cost figure
            usage = entry.get("usage", {})
            if usage:
                result_entry["usage"] = usage
            if entry.get("total_cost_usd") is not None:
                result_entry["total_cost_usd"] = entry["total_cost_usd"]
            return result_entry

        return entry

    def get_trajectory(self):
        return {
            "task": self.instruction,
            "trajectory": self.trajectory
        }