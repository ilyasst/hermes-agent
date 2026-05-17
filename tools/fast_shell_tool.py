#!/usr/bin/env python3
"""
fast_shell tool — delegate multi-step shell work to a cheaper planner.

[LOCAL MOD] Added to reduce thinking-model turn count on procedural shell
tasks. Instead of the thinking agent chaining many terminal calls (each
burning ~4K reasoning tokens), it invokes fast_shell once with a goal,
and a cheap planner (adequate tier) drives bash execution autonomously,
returning only a summary.

Typical win: "install vscode" goes from 5-7 thinking-agent turns (~8 min)
to 1 thinking-agent turn + 3-5 adequate planner calls (~45s).
"""

import json
import logging
from typing import Optional

from tools.terminal_tool import terminal_tool
from agent.auxiliary_client import call_llm

logger = logging.getLogger(__name__)


_PLANNER_SYSTEM = """You are a shell task executor. Complete the user's task with bash commands.

Respond ONLY with a JSON object, no other text, no markdown fences.

To run a command:
{"command": "<bash command>", "done": false, "reason": "<1-line why>"}

To finish successfully:
{"command": null, "done": true, "reason": "<what was accomplished, citing concrete evidence from command output>"}

To give up:
{"command": null, "done": true, "error": "<why you can't finish>"}

Rules:
- Prefer compound commands (&&, pipes, one-liners) over chaining many calls.
- Use sudo only when required (passwordless sudo is available).
- Keep commands reasonable (< 1 min each).
- If a command fails, try ONE alternative approach before giving up.
- VERIFY before marking done: cite concrete evidence from actual command
  output. Do not claim success unless the output explicitly confirms it.
  Do not interpret absence of output as success.
- String matching is case-sensitive: "Z" is NOT "z". Check character case
  literally when asked about character presence.
- When a task is ambiguous (e.g., "in /dir/" could mean top-level or
  recursive), default to the simplest interpretation (top-level)."""


def fast_shell_tool(
    task: str,
    max_steps: int = 5,
    task_id: Optional[str] = None,
    verify: bool = False,
) -> str:
    """Execute a multi-step shell task via the adequate-tier planner.

    Args:
        task: Goal in plain English.
        max_steps: Max commands the planner may try (default 5).
        task_id: Task identifier for terminal environment isolation.
        verify: If True, force a verification command after the planner
            claims done (runs one extra check step confirming the result).

    Returns:
        JSON string with {success, result/error, steps, history}.
    """
    messages = [
        {"role": "system", "content": _PLANNER_SYSTEM},
        {"role": "user", "content": f"Task: {task}"},
    ]

    history = []
    verify_requested = verify
    verify_ran = False
    effective_task_id = task_id or "default"

    # Give verify mode 2 extra iterations: one for the "continue" nudge
    # and one for the planner to run + finalize the verification.
    budget = max_steps + (2 if verify else 0)
    for step in range(budget):
        # Ask the planner for the next command
        try:
            response = call_llm(
                provider="custom",
                base_url="http://localhost:8800/v1",
                api_key="no-key-required",
                model="adequate",
                messages=messages,
                max_tokens=512,
                temperature=0.2,
                timeout=60,
            )
            content = (response.choices[0].message.content or "").strip()
        except Exception as e:
            logger.warning("fast_shell planner call failed: %s", e)
            return json.dumps({
                "success": False,
                "error": f"Planner call failed: {e}",
                "steps": len(history),
                "history": history,
            })

        # Strip optional markdown fences
        if content.startswith("```"):
            parts = content.split("```")
            if len(parts) >= 2:
                content = parts[1]
                if content.startswith("json"):
                    content = content[4:]
            content = content.strip()

        try:
            plan = json.loads(content)
        except json.JSONDecodeError:
            logger.warning("fast_shell got non-JSON plan: %s", content[:200])
            return json.dumps({
                "success": False,
                "error": f"Planner returned invalid JSON: {content[:200]}",
                "steps": len(history),
                "history": history,
            })

        if plan.get("done"):
            # If verify mode is enabled AND the planner claims success,
            # force one extra verification command to ground-truth the claim.
            # Only fires once per invocation.
            if verify and "error" not in plan:
                verify = False  # prevent loop / one-shot
                verify_ran = True
                prompt = (
                    "You marked done without running any command. "
                    "Run a concrete verification command that proves the "
                    "task is complete. Respond with JSON containing "
                    'command (not null), done:false.'
                    if not history else
                    "Before finalizing, run ONE concrete verification "
                    "command that proves the task result. Respond with "
                    'JSON containing command (not null), done:false.'
                )
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user", "content": prompt})
                continue
            return json.dumps({
                "success": "error" not in plan,
                "result": plan.get("reason") or plan.get("error") or "completed",
                "steps": len(history),
                "history": history,
                "verified": verify_ran if verify_requested else None,
            })

        cmd = plan.get("command", "")
        if not cmd:
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": "Empty command. Provide a valid JSON plan."})
            continue

        # Execute the command via terminal_tool (force=True: planner already vetted)
        exec_result = terminal_tool(
            command=cmd,
            task_id=effective_task_id,
            force=True,
        )

        # Parse terminal_tool result (JSON string)
        try:
            exec_data = json.loads(exec_result)
            output = (exec_data.get("output") or "")[:600]
            exit_code = exec_data.get("exit_code", -1)
            err = exec_data.get("error")
        except (json.JSONDecodeError, TypeError):
            output = str(exec_result)[:600]
            exit_code = -1
            err = None

        history.append({
            "step": step + 1,
            "command": cmd[:800],
            "exit": exit_code,
            "output": output,
            **({"error": err} if err else {}),
        })

        # Feed result back to planner
        messages.append({"role": "assistant", "content": content})
        messages.append({
            "role": "user",
            "content": (
                f"Exit code: {exit_code}\n"
                f"Output:\n{output}\n"
                + (f"Error: {err}\n" if err else "")
                + "\nWhat's next? (JSON only)"
            ),
        })

    return json.dumps({
        "success": False,
        "error": f"Max steps ({max_steps}) reached without completion",
        "steps": len(history),
        "history": history,
        "verified": verify_ran if verify_requested else None,
    })


FAST_SHELL_SCHEMA = {
    "name": "fast_shell",
    "description": (
        "Delegate a procedural shell task to a fast planner that runs bash "
        "autonomously (up to 5 steps) and returns a summary. Use this for "
        "multi-command sequences (install software, configure services, "
        "multi-step file ops) instead of chaining many 'terminal' calls. "
        "Prefer fast_shell when the task needs 2+ commands with feedback."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "Goal in plain English, e.g. 'install VS Code on Debian via the Microsoft apt repo'."
            },
            "max_steps": {
                "type": "integer",
                "description": "Max commands to run (default 5)."
            },
            "verify": {
                "type": "boolean",
                "description": "If true, forces one extra verification command after the planner claims done. Use for tasks where correctness matters (verifying an install, checking a fact)."
            }
        },
        "required": ["task"]
    }
}


# --- Registry ---
from tools.registry import registry

registry.register(
    name="fast_shell",
    toolset="terminal",
    schema=FAST_SHELL_SCHEMA,
    handler=lambda args, **kw: fast_shell_tool(
        task=args.get("task", ""),
        max_steps=args.get("max_steps", 5),
        verify=bool(args.get("verify", False)),
        task_id=kw.get("task_id"),
    ),
    emoji="⚡",
)
