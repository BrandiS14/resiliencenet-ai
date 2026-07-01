import os
import re
import json
import logging
from typing import Any
from pydantic import BaseModel, Field

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.tools import AgentTool, McpToolset
from google.adk.workflow import Workflow, Edge, START, node
from google.adk.agents.context import Context
from google.adk.events import RequestInput
from mcp import StdioServerParameters
from google.genai.types import HttpRetryOptions

from app.config import config

# Set up structured security audit logging
logging.basicConfig(level=logging.INFO)
audit_logger = logging.getLogger("security_audit")

# Initialize Gemini model with native retry options for 429 rate limiting
model = Gemini(
    model=config.model,
    retry_options=HttpRetryOptions(
        attempts=3,
        initial_delay=2.0,
        max_delay=10.0,
        http_status_codes=[429]
    )
)

# Define the connection parameters for our local MCP server
mcp_connection = StdioServerParameters(
    command="uv",
    args=["run", "python", "-m", "app.mcp_server"],
)

# Initialize MCP Toolsets with domain filters (least-privilege access)
hazard_mcp = McpToolset(
    connection_params=mcp_connection,
    tool_filter=["get_weather_and_hazards"]
)

shelter_mcp = McpToolset(
    connection_params=mcp_connection,
    tool_filter=["get_evacuation_routes", "get_resource_shelters"]
)

healthcare_mcp = McpToolset(
    connection_params=mcp_connection,
    tool_filter=["get_healthcare_facilities"]
)

resource_mcp = McpToolset(
    connection_params=mcp_connection,
    tool_filter=["get_resource_shelters"]
)

infrastructure_mcp = McpToolset(
    connection_params=mcp_connection,
    tool_filter=["get_evacuation_routes"]
)

government_mcp = McpToolset(
    connection_params=mcp_connection,
    tool_filter=["get_government_advisories"]
)

# 1. Define the specialized sub-agents with their respective MCP tools
hazard_agent = Agent(
    name="hazard_agent",
    model=model,
    tools=[hazard_mcp],
    instruction=(
        "You are the Hazard Intelligence Agent. Your role is to assess natural disaster risks "
        "and provide forecasts for events like floods, cyclones, earthquakes, wildfires, and heatwaves. "
        "Use your weather and hazard tool to check active hazard levels and safety warnings for the user's location."
    )
)

shelter_agent = Agent(
    name="shelter_agent",
    model=model,
    tools=[shelter_mcp],
    instruction=(
        "You are the Shelter & Evacuation Agent. Your role is to identify safe evacuation routes "
        "and recommend nearby emergency shelters. Use your tools to find routes and shelters. "
        "Consider any user constraints (e.g., mobility issues, pets, elderly family members) to provide the safest options."
    )
)

healthcare_agent = Agent(
    name="healthcare_agent",
    model=model,
    tools=[healthcare_mcp],
    instruction=(
        "You are the Healthcare Agent. Your role is to provide medical safety guidance, "
        "first-aid instructions, and locate active medical facilities or emergency care centers. "
        "Use your tool to locate hospitals and clinics near the user during a disaster."
    )
)

resource_agent = Agent(
    name="resource_agent",
    model=model,
    tools=[resource_mcp],
    instruction=(
        "You are the Resource Agent. Your role is to help users secure essential supplies, "
        "including clean water, non-perishable food, power backups, fuel, and emergency kits. "
        "Use your tool to find resource distribution centers near the user."
    )
)

infrastructure_agent = Agent(
    name="infrastructure_agent",
    model=model,
    tools=[infrastructure_mcp],
    instruction=(
        "You are the Infrastructure Agent. Your role is to monitor and report the status of "
        "critical infrastructure, including road closures, public transit availability, utility outages, "
        "and communication network status. Use your tool to query evacuation and routing safety."
    )
)

government_agent = Agent(
    name="government_agent",
    model=model,
    tools=[government_mcp],
    instruction=(
        "You are the Government & Emergency Services Agent. Your role is to provide official "
        "government advisories, emergency helplines, contacts, and relief program information. "
        "Use your tool to fetch the latest official advisories for the user's location."
    )
)

# Wrap specialized agents as tools for the Coordinator
hazard_tool = AgentTool(hazard_agent)
shelter_tool = AgentTool(shelter_agent)
healthcare_tool = AgentTool(healthcare_agent)
resource_tool = AgentTool(resource_agent)
infrastructure_tool = AgentTool(infrastructure_agent)
government_tool = AgentTool(government_agent)

# 2. Define the Coordinator Agent (Orchestrator) using native AgentTools
coordinator_agent = Agent(
    name="coordinator_agent",
    model=model,
    tools=[
        hazard_tool,
        shelter_tool,
        healthcare_tool,
        resource_tool,
        infrastructure_tool,
        government_tool,
    ],
    instruction=(
        "You are the Coordinator Agent for ResilienceNet AI. Your job is to orchestrate the "
        "disaster resilience response. When a user describes an emergency, you must:\n"
        "1. Analyze their query and decide which specialized agents (tools) to consult. "
        "Only call the sub-agents that are absolutely necessary for the user's specific query to conserve API quota.\n"
        "2. Call the relevant specialized agents to gather safety and resource information.\n"
        "3. Synthesize the findings into a clear, actionable, and compassionate response.\n"
        "4. If you need critical missing information (e.g., specific medical needs, pets, or exact location) "
        "to give safe advice, output the keyword 'HUMAN_INPUT_REQUIRED' followed by your question. "
        "Otherwise, provide the final response directly."
    )
)

# 3. Define the Workflow State Schema
class ResilienceState(BaseModel):
    user_query: str = ""
    sanitized_query: str = ""
    risk_assessment: str = ""
    shelter_info: str = ""
    healthcare_info: str = ""
    resource_info: str = ""
    infrastructure_status: str = ""
    government_info: str = ""
    requires_human_input: bool = False
    human_input_prompt: str = ""
    human_input_received: str = ""
    consent_given: bool = False
    requires_consent: bool = False

# 4. Define the Workflow Nodes
@node
async def security_checkpoint(ctx: Context, node_input: Any):
    """Checks the input for safety, prompt injections, PII, and user consent."""
    text = str(node_input)
    
    # 1. Prompt Injection Detection
    injection_keywords = [
        "ignore previous instructions", "override", "system prompt", 
        "bypass", "you are now a", "do not follow", "reveal your instruction"
    ]
    if any(kw in text.lower() for kw in injection_keywords):
        audit_logger.warning(json.dumps({
            "event": "PROMPT_INJECTION_DETECTED",
            "severity": "CRITICAL",
            "user_id": ctx.user_id,
            "session_id": ctx.session.id if ctx.session else None
        }))
        ctx.route = "unsafe"
        return "Security Event: Potential prompt injection detected. Request blocked."

    # 2. PII Redaction
    sanitized_text = text
    email_pattern = r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"
    phone_pattern = r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b"
    ssn_pattern = r"\b\d{3}-\d{2}-\d{4}\b"
    
    scrubbed = []
    if re.search(email_pattern, sanitized_text):
        sanitized_text = re.sub(email_pattern, "[EMAIL_REDACTED]", sanitized_text)
        scrubbed.append("email")
    if re.search(phone_pattern, sanitized_text):
        sanitized_text = re.sub(phone_pattern, "[PHONE_REDACTED]", sanitized_text)
        scrubbed.append("phone")
    if re.search(ssn_pattern, sanitized_text):
        sanitized_text = re.sub(ssn_pattern, "[ID_REDACTED]", sanitized_text)
        scrubbed.append("national_id")
        
    if scrubbed:
        audit_logger.info(json.dumps({
            "event": "PII_REDACTED",
            "severity": "INFO",
            "types_redacted": scrubbed,
            "user_id": ctx.user_id,
            "session_id": ctx.session.id if ctx.session else None
        }))
        
    ctx.state["sanitized_query"] = sanitized_text
    
    # 3. Domain-Specific Rule: Consent Check for Location/Medical Data
    sensitive_keywords = ["location", "address", "gps", "medical", "health", "symptoms", "illness", "injury"]
    consent_given = ctx.state.get("consent_given", False)
    if any(kw in text.lower() for kw in sensitive_keywords) and not consent_given:
        ctx.state["requires_consent"] = True
        audit_logger.warning(json.dumps({
            "event": "CONSENT_REQUIRED",
            "severity": "WARNING",
            "reason": "Sensitive data (location or medical) detected without prior consent.",
            "user_id": ctx.user_id,
            "session_id": ctx.session.id if ctx.session else None
        }))
        ctx.route = "needs_hitl"
        return "ResilienceNet AI requires your consent to process location or health-related data. Do you grant permission to proceed? (Yes/No)"

    ctx.route = "safe"
    return sanitized_text

@node(rerun_on_resume=True)
async def coordinator_node(ctx: Context, node_input: Any):
    """Orchestrates the response using the Coordinator Agent and sub-agent tools."""
    ctx.state["user_query"] = str(node_input)
    
    # Use the sanitized query from the security checkpoint
    query = ctx.state.get("sanitized_query", str(node_input))
    human_input_received = ctx.state.get("human_input_received", "")
    if human_input_received:
        query += f"\n[Additional User Info]: {human_input_received}"
        ctx.state["human_input_received"] = ""  # Reset after consuming
        
    response = await ctx.run_node(coordinator_agent, query)
    
    # Check if the coordinator requested human input
    if "HUMAN_INPUT_REQUIRED" in response:
        ctx.state["requires_human_input"] = True
        ctx.state["human_input_prompt"] = response.replace("HUMAN_INPUT_REQUIRED", "").strip()
        ctx.route = "needs_hitl"
        return ctx.state["human_input_prompt"]
        
    ctx.route = "complete"
    return response

@node
async def human_review(ctx: Context, node_input: Any):
    """Pauses the workflow to request input or consent from the user."""
    requires_consent = ctx.state.get("requires_consent", False)
    
    # Handle Consent Request
    if requires_consent:
        yield RequestInput(
            interrupt_id="user_consent_request",
            message="ResilienceNet AI requires your consent to process location or health-related data. Do you grant permission to proceed? (Yes/No)"
        )
        user_response = ctx.resume_inputs.get("user_consent_request")
        response_text = str(user_response).strip().lower() if user_response else ""
        if response_text in ["yes", "y", "grant", "approve"]:
            ctx.state["consent_given"] = True
            ctx.state["requires_consent"] = False
            audit_logger.info(json.dumps({
                "event": "CONSENT_GRANTED",
                "severity": "INFO",
                "user_id": ctx.user_id,
                "session_id": ctx.session.id if ctx.session else None
            }))
            # Route back to coordinator
            ctx.route = "default"
            ctx.output = "Consent granted. Processing your request."
            return
        else:
            ctx.state["requires_consent"] = False
            audit_logger.warning(json.dumps({
                "event": "CONSENT_DENIED",
                "severity": "WARNING",
                "user_id": ctx.user_id,
                "session_id": ctx.session.id if ctx.session else None
            }))
            ctx.route = "unsafe"
            ctx.output = "Request cancelled. ResilienceNet AI cannot process location or health-related data without your consent."
            return
            
    # Handle standard Details Request
    human_input_prompt = ctx.state.get("human_input_prompt", "Please provide the requested details.")
    yield RequestInput(
        interrupt_id="user_details_request",
        message=human_input_prompt
    )
    
    user_response = ctx.resume_inputs.get("user_details_request")
    ctx.state["human_input_received"] = str(user_response) if user_response else ""
    ctx.state["requires_human_input"] = False
    ctx.output = f"User details received: {user_response}"
    return

@node
async def security_event(ctx: Context, node_input: Any):
    """Terminal node for handling security violations."""
    return str(node_input)

@node
async def final_output(ctx: Context, node_input: Any):
    """Terminal node for delivering the final synthesized guidance."""
    return str(node_input)

# 6. Build the Workflow Graph
workflow = Workflow(
    name="resilience_workflow",
    description="Orchestrates the ResilienceNet AI multi-agent disaster resilience flow.",
    state_schema=ResilienceState,
    edges=[
        Edge(from_node=START, to_node=security_checkpoint),
        Edge(from_node=security_checkpoint, to_node=coordinator_node, route="safe"),
        Edge(from_node=security_checkpoint, to_node=security_event, route="unsafe"),
        Edge(from_node=security_checkpoint, to_node=human_review, route="needs_hitl"),
        Edge(from_node=coordinator_node, to_node=human_review, route="needs_hitl"),
        Edge(from_node=coordinator_node, to_node=final_output, route="complete"),
        Edge(from_node=human_review, to_node=coordinator_node),
        Edge(from_node=human_review, to_node=security_event, route="unsafe"),
    ]
)

# Export app and root_agent for fast_api_app.py
root_agent = workflow

app = App(
    root_agent=root_agent,
    name="app",
)
