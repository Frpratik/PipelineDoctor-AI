"""The tool-using agent loop shared by the Investigator and the Verifier (LangGraph).

  think -> (model asked for tools) tools -> think -> ...
        -> (done, or tool limit reached) conclude -> END

think: the LLM decides what to check next.   tools: we run the tools it asked for.
conclude: a final LLM call turns everything it learned into a structured object (Diagnosis, Verdict, ...).
"""

import json
import time
from typing import Annotated, Callable, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import BaseModel

from app import llm
from app.db import query_df
from app.detector import get_healthy_runs
from app.tools import TOOLS
from app.trace import record_step, run_tool

MAX_RESULT_CHARS = 1500  # long tool results are shortened when we write the final summary prompt


class AgentState(TypedDict):
    incident_id: int
    messages: Annotated[list[BaseMessage], add_messages]   # the agent's conversation with the tools
    tool_calls_made: int
    output: BaseModel


def build_context(incident_id: int) -> str:
    """The incident as text: which run, which checks failed, and which earlier runs were healthy."""
    inc = query_df(
        "SELECT i.run_id, r.run_date, r.rows_in, r.rows_loaded, r.status FROM incidents i "
        "JOIN pipeline_runs r USING (run_id) WHERE i.incident_id = %s", (incident_id,)).iloc[0]
    checks = query_df("SELECT check_name, passed, details FROM check_results WHERE run_id = %s ORDER BY check_id",
                      (int(inc["run_id"]),))
    healthy = get_healthy_runs(inc["run_date"])
    lines = [f"Incident {incident_id}: pipeline run_id={inc['run_id']} for {inc['run_date']}, status={inc['status']}, "
             f"rows_in={inc['rows_in']}, rows_loaded={inc['rows_loaded']}.",
             "Check results:"]
    for _, c in checks.iterrows():
        lines.append(f"  [{'PASS' if c['passed'] else 'FAIL'}] {c['check_name']}: {c['details']}")
    ids = [int(r) for r in healthy["run_id"]]
    lines.append(f"Earlier healthy run_ids (oldest first): {ids}. The most recent healthy run is {ids[-1]}.")
    return "\n".join(lines)


def _short(text: str) -> str:
    return text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + " ...[shortened]"


def _transcript(messages: list[BaseMessage]) -> str:
    """Flatten the tool conversation into plain text for the final structured call."""
    parts = []
    for m in messages:
        if isinstance(m, AIMessage):
            if m.content:
                parts.append(f"Agent thought: {m.content}")
            for call in m.tool_calls:
                parts.append(f"Agent called: {call['name']}({json.dumps(call['args'])})")
        elif isinstance(m, ToolMessage):
            parts.append(f"Tool result: {_short(str(m.content))}")
        elif m.type == "human":
            parts.append(f"Task:\n{m.content}")
    return "\n\n".join(parts)


def build_agent_graph(agent_name: str, schema: type[BaseModel], conclude_system: str, final_action: str,
                      max_tool_calls: int, tool_llm: Callable = llm.call_with_tools,
                      structured_call: Callable = llm.call_structured):
    """Build the loop for one agent. The two LLM functions are parameters so tests can replace them with fakes."""

    def think(state: AgentState) -> dict:
        tokens_before, started = llm.total_tokens(), time.perf_counter()
        reply = tool_llm(state["messages"], list(TOOLS.values()))
        asked = "; ".join(f"{c['name']}({json.dumps(c['args'])})" for c in reply.tool_calls)
        record_step(state["incident_id"], agent_name, "llm_call", tool_input=str(state["messages"][-1].content)[:500],
                    tool_output=(str(reply.content) + (f" -> wants: {asked}" if asked else "")).strip(),
                    tokens=llm.total_tokens() - tokens_before, duration_ms=int((time.perf_counter() - started) * 1000))
        return {"messages": [reply]}

    def tools(state: AgentState) -> dict:
        made, results = state["tool_calls_made"], []
        for call in state["messages"][-1].tool_calls:
            if made >= max_tool_calls:
                output = "ERROR: tool-call limit reached. Stop and summarize what you found."
            else:
                output = run_tool(state["incident_id"], agent_name, call["name"], call["args"])
                made += 1
            results.append(ToolMessage(content=output, tool_call_id=call["id"]))
        return {"messages": results, "tool_calls_made": made}

    def route(state: AgentState) -> str:
        wants_tools = bool(state["messages"][-1].tool_calls)
        return "tools" if wants_tools and state["tool_calls_made"] < max_tool_calls else "conclude"

    def conclude(state: AgentState) -> dict:
        tokens_before, started = llm.total_tokens(), time.perf_counter()
        output = structured_call(schema, conclude_system, _transcript(state["messages"]))
        record_step(state["incident_id"], agent_name, final_action, tool_output=output.model_dump_json(),
                    tokens=llm.total_tokens() - tokens_before, duration_ms=int((time.perf_counter() - started) * 1000))
        return {"output": output}

    graph = StateGraph(AgentState)
    graph.add_node("think", think)
    graph.add_node("tools", tools)
    graph.add_node("conclude", conclude)
    graph.add_edge(START, "think")
    graph.add_conditional_edges("think", route, {"tools": "tools", "conclude": "conclude"})
    graph.add_edge("tools", "think")
    graph.add_edge("conclude", END)
    return graph.compile()


def run_agent(graph, incident_id: int, system: str, first_message: str) -> BaseModel:
    """Start the loop with a system prompt and a first message; return the final structured output."""
    final = graph.invoke({
        "incident_id": incident_id,
        "messages": [("system", system), ("human", first_message)],
        "tool_calls_made": 0,
    })
    return final["output"]
