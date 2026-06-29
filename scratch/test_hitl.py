import asyncio
from google.adk.workflow import Workflow, Edge, START, node
from google.adk.agents.context import Context
from google.adk.events import RequestInput
from google.adk.apps import App

@node
async def hitl_node(ctx: Context, node_input: Any):
    print("hitl_node entered")
    val = yield RequestInput(
        interrupt_id="my_interrupt",
        message="Please enter something"
    )
    print(f"DEBUG: Type of yielded val: {type(val)}, Value: {val}")
    ctx.output = f"Received: {val}"
    return

workflow = Workflow(
    name="test_workflow",
    edges=[
        Edge(from_node=START, to_node=hitl_node),
    ]
)

async def main():
    app = App(root_agent=workflow, name="test_app")
    
    # First run: should suspend
    print("--- First Run ---")
    run_id = "test_run_1"
    generator = workflow.run(ctx=Context(user_id="user", session_id="session"), node_input="start")
    async for event in generator:
        print(f"Event: {event}")
        
    # Now simulate resuming with a response
    print("\n--- Resume Run ---")
    # We resume the workflow
    # In ADK 2.0, we can resume by running with the same session and providing the resume input
    # Let's see how the runner resumes it.
    # Usually, we pass the resume inputs to the context or session.
    # Let's check how the session is resumed in the API server.
    
if __name__ == "__main__":
    # Just import and inspect RequestInput or run a quick test
    pass
