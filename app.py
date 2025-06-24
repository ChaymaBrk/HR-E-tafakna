import datetime
import os
import pytz
import json
from flask import Flask, Response, request
from azure.identity import DefaultAzureCredential
from azure.core.pipeline.policies import BearerTokenCredentialPolicy
from azure.core.pipeline.transport import RequestsTransport
from azure.ai.projects import AIProjectClient
from azure.ai.agents.models import ListSortOrder
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# Configuration
ai_studio_endpoint = os.environ.get('AI_STUDIO_ENDPOINT')
ai_studio_subscription_id = os.environ.get('AI_STUDIO_SUBSCRIPTION_ID')
ai_studio_resource_group = os.environ.get('AI_STUDIO_RESOURCE_GROUP')
ai_studio_project_name = os.environ.get('AI_STUDIO_PROJECT_NAME')
ai_studio_agent_id = os.environ.get('AI_STUDIO_AGENT_ID')

# Initialize Azure client
if all([ai_studio_endpoint, ai_studio_subscription_id, ai_studio_resource_group, 
        ai_studio_project_name, ai_studio_agent_id]):
    credential = DefaultAzureCredential()
    agent_client = AIProjectClient(
        credential=credential,
        endpoint=ai_studio_endpoint,
        subscription_id=ai_studio_subscription_id,
        resource_group_name=ai_studio_resource_group,
        project_name=ai_studio_project_name,
        transport=RequestsTransport(),
        client_options={"api_version": "2024-05-01-preview"},
        policies=[BearerTokenCredentialPolicy(credential, "https://ai.azure.com/.default")]
    )
else:
    print("Warning: AI Studio Agent environment variables not fully configured")
    agent_client = None

# Thread storage {employee_id: {"thread_id": str, "context_set": bool}}
employee_threads = {}

def get_or_create_thread(employee_id: str) -> dict:
    """Get or create thread for employee"""
    if employee_id not in employee_threads:
        thread = agent_client.agents.threads.create()
        employee_threads[employee_id] = {
            "thread_id": thread.id,
            "context_set": False,
            "last_activity": datetime.datetime.now(pytz.UTC)
        }
        print(f"Created new thread {thread.id} for employee {employee_id}")
    return employee_threads[employee_id]

def create_employee_context(employee_data: dict) -> str:
    """Format comprehensive employee context for the AI"""
    return (
        "EMPLOYEE LEGAL CONTEXT (use for all responses):\n"
        f"- ID: {employee_data.get('id')}\n"
        f"- Full Name: {employee_data.get('full_name')}\n"
        f"- CIN: {employee_data.get('cin')} (issued {employee_data.get('cin_date')} in {employee_data.get('cin_place')})\n"
        f"- Contract: {employee_data.get('contract_type')} ({employee_data.get('employment_type')})\n"
        f"- Salary: {employee_data.get('net_salary')} TND (Brut: {employee_data.get('brut_salary')} TND)\n"
        f"- Seniority: {employee_data.get('seniority_in_months')} months (since {employee_data.get('date_of_start')})\n"
        f"- Profession: {employee_data.get('profession')}\n"
        f"- CNSS: {employee_data.get('cnss_number')}\n"
        f"- Status: {employee_data.get('marital_status')}, {employee_data.get('nationality')}\n"
        "When answering, always consider:\n"
        "1. Tunisian Labor Code provisions\n"
        "2. The employee's specific contract terms\n"
        "3. Their seniority and salary level\n"
        "4. Any other relevant factors from their profile"
    )

@app.route('/api/hr-legal-assistant', methods=['POST'])
def hr_legal_assistant():
    if not agent_client:
        return Response("data: Service not configured\n\n", 
                       content_type="text/event-stream")

    data = request.json
    if not data or 'employee_data' not in data or 'question' not in data:
        return Response("data: Invalid request - employee_data and question are required\n\n",
                      content_type="text/event-stream",
                      status=400)

    employee_data = data['employee_data']
    question = data['question']
    
    # Validate employee data contains required ID
    if 'id' not in employee_data:
        return Response("data: Employee data must contain an 'id' field\n\n",
                       content_type="text/event-stream",
                       status=400)

    employee_id = str(employee_data['id'])

    def generate():
        try:
            # Get or create thread
            thread_info = get_or_create_thread(employee_id)
            thread_id = thread_info["thread_id"]
            thread_info["last_activity"] = datetime.datetime.now(pytz.UTC)

            # Set employee context if first time
            if not thread_info["context_set"]:
                context = create_employee_context(employee_data)
                agent_client.agents.messages.create(
                    thread_id=thread_id,
                    role="user",
                    content=context
                )
                thread_info["context_set"] = True

            # Add user question
            agent_client.agents.messages.create(
                thread_id=thread_id,
                role="user",
                content=question
            )

            # Run agent
            run = agent_client.agents.runs.create_and_process(
                thread_id=thread_id,
                agent_id=ai_studio_agent_id
            )

            if run.status == "failed":
                yield f"data: {json.dumps({'error': run.last_error})}\n\n"
                return

            # Get and stream response
            messages = agent_client.agents.messages.list(
                thread_id=thread_id,
                order=ListSortOrder.ASCENDING
            )

            assistant_response = next(
                (msg.text_messages[-1].text.value 
                 for msg in messages 
                 if msg.role == "assistant" and msg.text_messages),
                "No response from assistant"
            )

            # Stream response chunks
            for chunk in [assistant_response[i:i+200] for i in range(0, len(assistant_response), 200)]:
                yield f"data: {json.dumps({'response': chunk})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(generate(), content_type="text/event-stream")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)