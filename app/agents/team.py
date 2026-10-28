"""Investigator <-> Verifier loop (LangGraph).

  investigator -> verifier -> (accepted) END
                           -> (rejected, retries left) investigator, with the Verifier's feedback
                           -> (rejected, no retries left) END, marked unverified
"""

from typing import Callable, TypedDict

from langgraph.graph import END, START, StateGraph

from app.agents.investigator import investigate
from app.agents.verifier import verify
from app.db import set_incident_status
from app.schemas import Diagnosis, Verdict

MAX_RETRIES = 2  # first attempt + at most 2 retries = 3 attempts


class TeamState(TypedDict):
    incident_id: int
    retries: int
    feedback: str
    diagnosis: Diagnosis
    verdict: Verdict


def _feedback_text(diagnosis: Diagnosis, verdict: Verdict) -> str:
    """What we tell the Investigator after a rejection: its old answer, the problems and the advice."""
    problems = "\n".join(f"- {p}" for p in verdict.problems)
    return (f"Your previous root cause was: {diagnosis.root_cause}\n"
            f"Problems found by the reviewer:\n{problems}\nReviewer advice: {verdict.feedback}")


def build_team_graph(investigate_fn: Callable = investigate, verify_fn: Callable = verify):
    """The two agent functions are parameters so tests can replace them."""

    def investigator_node(state: TeamState) -> dict:
        return {"diagnosis": investigate_fn(state["incident_id"], state["feedback"])}

    def verifier_node(state: TeamState) -> dict:
        verdict = verify_fn(state["incident_id"], state["diagnosis"])
        if verdict.accepted:
            return {"verdict": verdict}
        return {"verdict": verdict, "retries": state["retries"] + 1,
                "feedback": _feedback_text(state["diagnosis"], verdict)}

    def route(state: TeamState) -> str:
        if state["verdict"].accepted or state["retries"] > MAX_RETRIES:
            return "done"
        return "retry"

    graph = StateGraph(TeamState)
    graph.add_node("investigator", investigator_node)
    graph.add_node("verifier", verifier_node)
    graph.add_edge(START, "investigator")
    graph.add_edge("investigator", "verifier")
    graph.add_conditional_edges("verifier", route, {"retry": "investigator", "done": END})
    return graph.compile()


def diagnose_incident(incident_id: int, graph=None) -> dict:
    """Run the full loop. Returns the last diagnosis, its verdict and how many attempts it took."""
    graph = graph or build_team_graph()
    set_incident_status(incident_id, "investigating")
    final = graph.invoke({"incident_id": incident_id, "retries": 0, "feedback": ""})
    accepted = final["verdict"].accepted
    set_incident_status(incident_id, "verified" if accepted else "unverified")
    attempts = min(final["retries"] + 1, MAX_RETRIES + 1)  # one attempt, plus one per rejection that led to a retry
    return {"diagnosis": final["diagnosis"], "verdict": final["verdict"], "attempts": attempts}
