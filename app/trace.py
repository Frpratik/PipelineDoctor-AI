"""Agent trace: every step an agent takes is saved to agent_steps, so we can replay how it reasoned.

All agents use these same functions. step_no counts up per incident across all agents, so the table reads
like one story: investigator steps, then verifier steps, then fixer steps.
"""

import json
import time

from app.db import get_conn, query_df
from app.tools import TOOLS

# A background job can listen to the trace: it sets this to a function that receives a one-line progress message.
_on_step = None


def set_progress_callback(callback) -> None:
    global _on_step
    _on_step = callback


def _progress_message(agent_name, action, tool_name) -> str:
    who = agent_name.capitalize()
    if action == "tool_call":
        return f"{who}: called {tool_name}"
    if action == "llm_call":
        return f"{who}: thinking"
    return f"{who}: {action.replace('_', ' ')}"


def record_step(incident_id, agent_name, action, tool_name=None, tool_input=None, tool_output=None,
                tokens=0, duration_ms=0) -> int:
    """Save one step and return its step_no.

    action says what kind of step it was, e.g. "tool_call", "llm_call", "memory_lookup".
    tool_input / tool_output hold the step's input and output as text (for an LLM call: the prompt and the answer).
    """
    conn = get_conn()
    cur = conn.cursor()
    # step_no is picked inside the INSERT itself, so it is always the next number for this incident.
    cur.execute(
        "INSERT INTO agent_steps (incident_id, agent_name, step_no, action, tool_name, tool_input, tool_output, tokens, duration_ms) "
        "SELECT %s, %s, COALESCE(MAX(step_no), 0) + 1, %s, %s, %s, %s, %s, %s FROM agent_steps WHERE incident_id = %s "
        "RETURNING step_no",
        (incident_id, agent_name, action, tool_name, tool_input, tool_output, tokens, duration_ms, incident_id),
    )
    step_no = cur.fetchone()[0]
    conn.commit()
    conn.close()
    if _on_step:
        _on_step(_progress_message(agent_name, action, tool_name))
    return step_no


def run_tool(incident_id, agent_name, tool_name, tool_args: dict) -> str:
    """Call one of our tools, time it, record it in the trace, and return its output to the agent."""
    started = time.perf_counter()
    if tool_name not in TOOLS:
        output = f"ERROR: unknown tool '{tool_name}'. Available: {list(TOOLS)}"
    else:
        try:
            output = TOOLS[tool_name](**tool_args)
        except Exception as e:  # bad arguments from the LLM must not crash the agent
            output = f"ERROR: {e}"
    duration_ms = int((time.perf_counter() - started) * 1000)
    record_step(incident_id, agent_name, "tool_call", tool_name, json.dumps(tool_args), output, duration_ms=duration_ms)
    return output


def get_trace(incident_id) -> list:
    """All steps of an incident in order, as a list of dicts (the API in Step 10 returns this)."""
    steps = query_df(
        "SELECT step_no, agent_name, action, tool_name, tool_input, tool_output, tokens, duration_ms "
        "FROM agent_steps WHERE incident_id = %s ORDER BY step_no", (incident_id,))
    steps = steps.astype(object).where(steps.notna(), None)  # pandas turns NULL into NaN; keep it None
    return steps.to_dict("records")


def format_trace(incident_id, width=90) -> str:
    """Human-readable version of the trace for the terminal."""
    lines = []
    for s in get_trace(incident_id):
        what = s["tool_name"] or ""
        lines.append(f"#{s['step_no']:<3} [{s['agent_name']}] {s['action']} {what}  ({s['duration_ms']} ms, {s['tokens']} tokens)")
        if s["tool_input"]:
            lines.append(f"      in : {s['tool_input'][:width]}")
        if s["tool_output"]:
            lines.append(f"      out: {s['tool_output'][:width]}")
    return "\n".join(lines)
