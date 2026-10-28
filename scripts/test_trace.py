"""Record a few demo steps for incident 1 and print the trace (no LLM involved).

Run from the project root:  python -m scripts.test_trace
Needs an open incident (run the detector on a faulty run first). The demo rows are removed at the end.
"""

from app.db import get_conn
from app.trace import format_trace, record_step, run_tool

INCIDENT_ID = 1
AGENT = "demo"


def delete_demo_rows():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM agent_steps WHERE agent_name = %s", (AGENT,))
    conn.commit()
    conn.close()


if __name__ == "__main__":
    delete_demo_rows()

    # an LLM step, as the Investigator will record it (tokens and timing are made up here)
    record_step(INCIDENT_ID, AGENT, "llm_call", tool_input="Failed checks: value_distribution ...",
                tool_output="Hypothesis: amounts are on a different scale. Next: compare_runs.",
                tokens=412, duration_ms=1530)

    # a real tool call, timed and recorded automatically
    run_tool(INCIDENT_ID, AGENT, "get_recent_runs", {"n": 2})

    # a tool call with a mistake: the error is returned and also recorded
    run_tool(INCIDENT_ID, AGENT, "no_such_tool", {})

    print(format_trace(INCIDENT_ID))
    delete_demo_rows()
