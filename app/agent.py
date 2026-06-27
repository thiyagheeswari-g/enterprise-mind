import os
from typing import Any
from pydantic import BaseModel, Field

from google.adk.workflow import Workflow, node
from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.apps import App

from .config import config

# ==============================================================================
# SCHEMAS
# ==============================================================================

class Finding(BaseModel):
    finding_id: str
    description: str
    metric: str
    value: str
    comparison: str
    direction: str
    magnitude: str
    supporting_rows: str
    confidence_basis: str

class FindingsReport(BaseModel):
    findings: list[Finding]

class InvestigationPlan(BaseModel):
    business_question: str
    hypotheses: list[str]
    investigation_steps: list[str]
    key_metrics: list[str]

class Recommendation(BaseModel):
    action: str
    target: str
    expected_impact: str
    priority: str
    effort: str
    timeframe: str

class ConsultingReport(BaseModel):
    root_cause: str
    recommendations: list[Recommendation]
    confidence_score: int
    evidence: list[str]

class QualityReview(BaseModel):
    validation_status: str
    checks_passed: list[str]
    checks_failed: list[str]
    issues: list[str]
    confidence_assessment: str
    final_report: str
    disclaimer: str

import re
import json
import hashlib
from datetime import datetime

# ==============================================================================
# SECURITY & RULES
# ==============================================================================

PII_PATTERNS = {
    "email":       r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b',
    "phone":       r'\b(\+\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b',
    "ssn":         r'\b\d{3}-\d{2}-\d{4}\b',
    "credit_card": r'\b(?:\d{4}[-\s]?){3}\d{4}\b',
    "ip_address":  r'\b(?:\d{1,3}\.){3}\d{1,3}\b',
    "aadhaar":     r'\b\d{4}\s\d{4}\s\d{4}\b',
}

INJECTION_KEYWORDS = [
    "ignore instructions", "ignore previous", "jailbreak", "bypass",
    "override", "forget previous", "new instructions", "act as",
    "disregard", "pretend you are", "system prompt", "you are now",
    "do anything now", "dan mode", "developer mode"
]

def log_audit(agent: str, action: str, severity: str, input_text: str, pii_scrubbed: bool, injection_detected: bool, confidence: int | None = None):
    log_entry = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "agent": agent,
        "action": action,
        "severity": severity,
        "input_hash": hashlib.sha256(input_text.encode('utf-8')).hexdigest() if input_text else "",
        "pii_scrubbed": pii_scrubbed,
        "injection_detected": injection_detected,
        "confidence": confidence
    }
    print(json.dumps(log_entry))
    return log_entry

# ==============================================================================
# FUNCTION NODES
# ==============================================================================

@node
def security_checkpoint(ctx: Context, node_input: Any) -> Event:
    """PII scrub + injection detection + audit log."""
    input_str = ""
    
    # Extract text and attachments from ADK Content payload
    if hasattr(node_input, 'parts'):
        for part in node_input.parts:
            if hasattr(part, 'text') and part.text:
                input_str += part.text + " "
            elif hasattr(part, 'inline_data') and part.inline_data:
                os.makedirs(".data", exist_ok=True)
                file_path = os.path.abspath(".data/uploaded.csv")
                with open(file_path, "wb") as f:
                    data = part.inline_data.data
                    if isinstance(data, str):
                        f.write(data.encode('utf-8'))
                    else:
                        f.write(data)
                ctx.state['last_uploaded_filename'] = file_path
    else:
        input_str = str(node_input)
        
    if not input_str.strip():
        input_str = str(node_input)
        
    # Fallback: if no file was uploaded via UI, check if the user provided a file path in the prompt
    if 'last_uploaded_filename' not in ctx.state:
        csv_match = re.search(r'([a-zA-Z]:\\[^\s"]+\.csv|/[^\s"]+\.csv)', input_str)
        if csv_match:
            potential_path = csv_match.group(1).strip()
            if os.path.exists(potential_path):
                ctx.state['last_uploaded_filename'] = potential_path
        
    pii_scrubbed = False
    injection_detected = False
    
    # Injection Detection
    lower_input = input_str.lower()
    for kw in INJECTION_KEYWORDS:
        if kw in lower_input:
            injection_detected = True
            log_audit("security_checkpoint", f"Prompt injection detected: {kw}", "CRITICAL", input_str, pii_scrubbed, injection_detected)
            return Event(output="SECURITY_EVENT: Prompt injection detected. Request blocked.", route="SECURITY_EVENT")
            
    # PII Scrubbing
    scrubbed_input = input_str
    for pii_type, pattern in PII_PATTERNS.items():
        if re.search(pattern, scrubbed_input):
            pii_scrubbed = True
            scrubbed_input = re.sub(pattern, f"[REDACTED {pii_type.upper()}]", scrubbed_input)
            
    # Audit log
    log_audit("security_checkpoint", "Passed security checks", "INFO", input_str, pii_scrubbed, injection_detected)
    
    return Event(output=scrubbed_input, route="SAFE")



@node
async def final_output_node(ctx: Context, node_input: dict):
    """Handles final HITL (Human-in-the-loop) approval."""
    if not ctx.resume_inputs:
        yield RequestInput(
            interrupt_id="executive_approval",
            message="Please review the final Validated Executive Report. Type 'approve' to finalize or provide feedback."
        )
        return
    
    yield Event(output=node_input)

# ==============================================================================
# LLM AGENTS
# ==============================================================================

from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters

mcp_toolset = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command="python",
            args=["-m", "app.mcp_server"]
        )
    )
)

business_analyst_agent = LlmAgent(
    name="business_analyst_agent",
    model=config.model,
    instruction="""You are a Business Analyst.
Analyze the user's business question and generate a structured Investigation Plan.
You MUST output the InvestigationPlan schema. NEVER do analysis yourself.""",
    output_schema=InvestigationPlan,
    output_key="last_investigation_plan"
)

data_investigator_agent = LlmAgent(
    name="data_investigator_agent",
    model=config.model,
    instruction="""You are the Data Investigator Agent.
Your job is to execute the Business Analyst's Investigation Plan on the dataset.
Read the dataset from the file path provided in ctx.state['last_uploaded_filename'].
You MUST use the provided MCP tools to analyze the data.
MANDATORY SEQUENCE:
1. ALWAYS call clean_dataset first.
2. ALWAYS call engineer_features second.
3. Then call analyze_trends, detect_anomalies, and segment_and_compare as dictated by the Investigation Plan.
You MUST output the FindingsReport schema with a minimum of 3 findings.""",
    output_schema=FindingsReport,
    output_key="last_analysis_findings",
    tools=[mcp_toolset]
)

consulting_agent = LlmAgent(
    name="consulting_agent",
    model=config.model,
    instruction="""You are a Consulting Agent (Tone: McKinsey Engagement Manager).
Transform raw findings from the Data Investigator into business intelligence.
Answer: WHAT happened, WHY it happened (root cause), and WHAT SHOULD BE DONE.
IMPORTANT: You MUST generate a valid ConsultingReport using the provided data.
DO NOT hallucinate tool calls like 'SetModelResponseRecommendations'. ONLY use the built-in 'set_model_response' tool to return your final JSON matching the ConsultingReport schema.""",
    output_schema=ConsultingReport,
    output_key="last_consulting_report"
)

quality_reviewer_agent = LlmAgent(
    name="quality_reviewer_agent",
    model=config.model,
    instruction="""You are a Quality Reviewer Agent.
Validate the Consulting Agent's report. Check evidence trace, confidence justification,
recommendation specificity, fabrication scan, and consultant tone.
CRITICAL DOMAIN RULE: If the Consulting Agent finding has confidence_score < 60, you MUST mark it "INSUFFICIENT_EVIDENCE" and request revision.
You MUST output the QualityReview schema.""",
    output_schema=QualityReview,
    output_key="last_quality_review"
)

# ==============================================================================
# WORKFLOW GRAPH
# ==============================================================================

root_agent = Workflow(
    name="enterprise_mind",
    edges=[
        ('START', security_checkpoint),
        (security_checkpoint, {
            "SAFE": business_analyst_agent,
            "SECURITY_EVENT": final_output_node
        }),
        (business_analyst_agent, data_investigator_agent),
        (data_investigator_agent, consulting_agent),
        (consulting_agent, quality_reviewer_agent),
        (quality_reviewer_agent, final_output_node)
    ]
)

app = App(
    name="app",
    root_agent=root_agent
)
