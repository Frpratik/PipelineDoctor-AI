"""Pydantic models for structured agent outputs (the LLM must fill these in)."""

from typing import Literal

from pydantic import BaseModel, Field


class Evidence(BaseModel):
    claim: str = Field(description="One fact that supports the root cause, e.g. 'amount_inr is about 100x larger than yesterday'")
    sql_or_tool: str = Field(description="The EXACT SQL query or tool call that produced the result, e.g. 'compare_runs(8, 7)', so someone else can re-run it")
    result: str = Field(description="The key numbers or text that the SQL or tool returned")


class Judgement(BaseModel):
    verdict: Literal["correct", "partial", "wrong"]
    reason: str = Field(description="One or two sentences explaining the verdict against the rubric")


class FixProposal(BaseModel):
    fix_type: Literal["update_rows", "delete_rows", "manual"] = Field(
        description="update_rows / delete_rows = SQL on the orders table; manual = something SQL cannot fix (e.g. ask the vendor to resend)")
    description: str = Field(description="Plain-English summary of the fix and why it repairs the root cause")
    sql_or_steps: str = Field(description="For SQL fixes: the UPDATE/DELETE statement(s) on orders, separated by semicolons. For manual: numbered steps")
    risk_notes: str = Field(description="What could go wrong, and what to double-check before approving")


class Verdict(BaseModel):
    accepted: bool = Field(description="True only if the evidence was reproduced, supports the root cause, and the root cause explains all failed checks")
    problems: list[str] = Field(description="Specific problems found with the diagnosis; empty if accepted")
    feedback: str = Field(description="Concrete instructions for the investigator on what to check or change; empty if accepted")


class Diagnosis(BaseModel):
    hypotheses_considered: list[str] = Field(description="Every hypothesis you thought about, including the ones you rejected")
    root_cause: str = Field(description="One or two specific sentences: what went wrong and where")
    evidence: list[Evidence] = Field(description="2 to 4 pieces of evidence, each backed by a tool call or SQL")
    affected_tables: list[str]
    affected_columns: list[str]
    confidence: float = Field(ge=0, le=1, description="0 = guess, 1 = certain")
