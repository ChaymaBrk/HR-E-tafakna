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
import tiktoken  
from azure.storage.blob import BlobServiceClient
import uuid
import dotenv
from langdetect import detect
dotenv.load_dotenv()
app = Flask(__name__)
CORS(app)

# Configuration (unchanged)
ai_studio_endpoint = os.environ.get('AI_STUDIO_ENDPOINT')
ai_studio_subscription_id = os.environ.get('AI_STUDIO_SUBSCRIPTION_ID')
ai_studio_resource_group = os.environ.get('AI_STUDIO_RESOURCE_GROUP')
ai_studio_project_name = os.environ.get('AI_STUDIO_PROJECT_NAME')
ai_studio_agent_id = "asst_SMDn2DfoX4DmKRVXtTDNPEUJ"
# Azure Blob Storage Configuration
STORAGE_CONN_STRING = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
CONTAINER_NAME = "chat-conversations"

# Initialize Blob Service Client
blob_service_client = BlobServiceClient.from_connection_string(STORAGE_CONN_STRING)
container_client = blob_service_client.get_container_client(CONTAINER_NAME)

# Create container if it doesn't exist
try:
    container_client.create_container()
except Exception as e:
    if "ContainerAlreadyExists" not in str(e):
        raise
# Initialize tokenizer for token counting
try:
    tokenizer = tiktoken.get_encoding("cl100k_base")
except:
    # Fallback if tiktoken not available (very rough estimation)
    tokenizer = None

# Constants
MAX_TOKENS = 5000  # Maximum tokens per conversation
TOKEN_WARNING = 4500  # Warn when approaching limit

# Initialize Azure client (unchanged)
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

# Enhanced thread storage with token tracking
employee_threads = {}

def count_tokens(text: str) -> int:
    """Estimate the number of tokens in a text string"""
    if tokenizer:
        return len(tokenizer.encode(text))
    # Fallback: very rough estimation (~4 chars per token)
    return len(text) // 4

def get_or_create_thread(employee_id: str) -> dict:
    """Get or create thread for employee with token tracking"""
    if employee_id not in employee_threads:
        thread = agent_client.agents.threads.create()
        employee_threads[employee_id] = {
            "thread_id": thread.id,
            "context_set": False,
            "last_activity": datetime.datetime.now(pytz.UTC),
            "token_count": 0,  # Track total tokens in conversation
            "warned": False    # Track if user has been warned about token limit
        }
        print(f"Created new thread {thread.id} for employee {employee_id}")
    return employee_threads[employee_id]

def create_employee_context(employee_data: dict, language: str) -> str:
    """Format comprehensive employee context with language instruction"""
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
        "4. Any other relevant factors from their profile\n"
        f"5. Always reply in the same language as the user's question. Detected language: {language}\n"
    )
def save_conversation(employee_id: str, messages: list):
    """Save conversation to blob storage as JSON"""
    blob_name = f"{employee_id}/{uuid.uuid4()}.json"
    blob_client = container_client.get_blob_client(blob_name)
    
    conversation = {
        "timestamp": datetime.datetime.now(pytz.UTC).isoformat(),
        "messages": messages
    }
    
    blob_client.upload_blob(json.dumps(conversation), overwrite=True)

def get_recent_conversations(employee_id: str, max_count=3):
    """Get recent conversations for an employee"""
    blobs = container_client.list_blobs(name_starts_with=f"{employee_id}/")
    conversations = []
    
    for blob in sorted(blobs, key=lambda x: x.creation_time, reverse=True)[:max_count]:
        blob_client = container_client.get_blob_client(blob.name)
        data = blob_client.download_blob().readall()
        conversations.append(json.loads(data))
    
    return conversations

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
    
    if 'id' not in employee_data:
        return Response("data: Employee data must contain an 'id' field\n\n",
                       content_type="text/event-stream",
                       status=400)

    employee_id = str(employee_data['id'])
    try:
        language = detect(question)
    except Exception:
        language = "FR"
    def generate():
        try:
            # Get/create thread
            thread_info = get_or_create_thread(employee_id)
            thread_id = thread_info["thread_id"]
            
            # Check token limit before processing
            if thread_info["token_count"] >= MAX_TOKENS:
                yield f"data: {json.dumps({'error': 'You have reached the maximum conversation limit. Please start a new conversation.'})}\n\n"
                return
            
            # Update activity time
            thread_info["last_activity"] = datetime.datetime.now(pytz.UTC)
            
            # Calculate tokens for new messages
            question_tokens = count_tokens(question)
            estimated_response_tokens = 1000  # Our max_completion_tokens setting
            
            # Check if this would exceed the limit
            if thread_info["token_count"] + question_tokens + estimated_response_tokens > MAX_TOKENS:
                yield f"data: {json.dumps({'error': 'This question would exceed the conversation token limit. Please ask a shorter question or start a new conversation.'})}\n\n"
                return
            
            # Warn if approaching limit
            if not thread_info["warned"] and thread_info["token_count"] + question_tokens + estimated_response_tokens > TOKEN_WARNING:
                thread_info["warned"] = True
                warning_msg = (
                    f"Warning: You are approaching the conversation limit "
                    f"({thread_info['token_count'] + question_tokens + estimated_response_tokens}/{MAX_TOKENS} tokens). "
                    "Consider starting a new conversation soon."
                )
                yield f"data: {json.dumps({'warning': warning_msg})}\n\n"

            # Get recent conversation history (last 2 exchanges)
            recent_conversations = []
            try:
                recent_conversations = get_recent_conversations(employee_id, max_count=2)
            except Exception as e:
                print(f"Error loading conversation history: {str(e)}")

            # Build context with history
            context = create_employee_context(employee_data, language)
            if recent_conversations:
                context += "\n\nPREVIOUS CONVERSATION CONTEXT:\n"
                for conv in recent_conversations:
                    for msg in conv["messages"][-2:]:  # Get last 2 messages from each conversation
                        context += f"{msg['role'].upper()}: {msg['content']}\n"

            # Set employee context if first time or if we have new history
            if not thread_info["context_set"] or recent_conversations:
                agent_client.agents.messages.create(
                    thread_id=thread_id,
                    role="user",
                    content=context
                )
                thread_info["context_set"] = True
                thread_info["token_count"] += count_tokens(context)

            # Add user question
            agent_client.agents.messages.create(
                thread_id=thread_id,
                role="user",
                content=question
            )
            thread_info["token_count"] += question_tokens

            # Run agent
            run = agent_client.agents.runs.create_and_process(
                thread_id=thread_id,
                agent_id=ai_studio_agent_id,
                max_completion_tokens=1000
            )

            if run.status == "failed":
                yield f"data: {json.dumps({'error': run.last_error})}\n\n"
                return

            # Get messages in DESCENDING order (newest first)
            messages = agent_client.agents.messages.list(
                thread_id=thread_id,
                order=ListSortOrder.DESCENDING
            )

            # Find the LATEST assistant message
            assistant_response = None
            for msg in messages:
                if msg.role == "assistant" and msg.text_messages:
                    assistant_response = msg.text_messages[-1].text.value
                    break  # Stop at first found assistant message (newest)

            if not assistant_response:
                assistant_response = "No response from assistant"
            
            # Update token count with response
            response_tokens = count_tokens(assistant_response)
            thread_info["token_count"] += response_tokens

            # Save conversation to blob storage
            try:
                save_conversation(
                    employee_id=employee_id,
                    messages=[
                        {"role": "user", "content": question},
                        {"role": "assistant", "content": assistant_response}
                    ]
                )
            except Exception as e:
                print(f"Error saving conversation: {str(e)}")

            # Stream response chunks
            for chunk in [assistant_response[i:i+200] for i in range(0, len(assistant_response), 200)]:
                yield f"data: {json.dumps({'response': chunk})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(generate(), content_type="text/event-stream")

# Add cleanup for old threads
def cleanup_old_threads():
    """Clean up threads older than 24 hours"""
    now = datetime.datetime.now(pytz.UTC)
    for employee_id, thread_info in list(employee_threads.items()):
        if (now - thread_info["last_activity"]).total_seconds() > 86400:  # 24 hours
            del employee_threads[employee_id]
            print(f"Cleaned up thread for employee {employee_id}")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=True)