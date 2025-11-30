import time
import requests
from flask import Flask, request, jsonify, Response, stream_with_context
import json
import sys
import os
import dotenv

# Add AI_Engine to path
# Use insert(0) to ensure local modules (like config.py) take precedence over installed packages
sys.path.insert(0, os.path.join(os.getcwd(), "AI_Engine"))

try:
    from ai_engine import AI_engine
    from model_cache import shared_model_cache
except ImportError as e:
    print(f"Failed to import AI Engine: {e}")
    sys.exit(1)

app = Flask(__name__)

# Import our function executor
from function_executor import parse_function_calls_from_text, execute_function_call, clean_content_for_display

dotenv.load_dotenv()

# Initialize AI Engine
engine = AI_engine(verbose=True)
shared_model_cache.load_cache()

# AIEngine class removed as we are using the imported AI_engine module

def strip_text_values(data):
    """Recursively replaces all string values in a JSON object with an empty string."""
    if isinstance(data, dict):
        return {key: strip_text_values(value) for key, value in data.items()}
    elif isinstance(data, list):
        return [strip_text_values(item) for item in data]
    elif isinstance(data, str):
        return ""  # Replace string with empty
    else:
        return data  # Keep numbers, booleans, etc.

def process_function_calls_in_response(content):
    """Process any function calls found in the response content and execute them"""
    function_calls = parse_function_calls_from_text(content)
    function_results = []
    cleaned_content = content
    
    if function_calls:
        print(f"[API] Found {len(function_calls)} function calls to execute")
        
        # Clean the content for display (remove thinking and tool calls)
        cleaned_content = clean_content_for_display(content)
        
        for func_call in function_calls:
            func_name = func_call["name"]
            params = func_call["parameters"]
            print(f"[API] Executing function: {func_name} with params: {list(params.keys())}")
            
            result = execute_function_call(func_name, params)
            function_results.append({
                "name": func_name,
                "result": result
            })
            print(f"[API] Function {func_name} result: {result}")
    
    return function_results, cleaned_content

# engine = AIEngine(A4F_API_KEY, CHAT_COMPLETION_ENDPOINT) # Removed

@app.after_request
def add_headers(response):
    response.headers['Content-Type'] = 'application/json; charset=utf-8'
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response

@app.route('/api/version', methods=['GET'])
def version():
    print("[API] GET /api/version")
    return jsonify({"version": "1.0.0"})

@app.route('/api/tags', methods=['GET'])
def tags():
    print("[API] GET /api/tags")
    # Use shared_model_cache to get models
    if shared_model_cache.is_cache_valid():
        models = shared_model_cache.get_models()
    else:
        # If cache is invalid, we might want to trigger a refresh or return empty
        # For now, let's try to return what we have or empty
        models = shared_model_cache.get_models()
        
    formatted = []
    for m in models:
        # Handle both string (new cache) and dict (old cache/fallback) formats
        if isinstance(m, str):
            model_id = m
            modified_at = "2025-08-01T00:00:00Z"
        else:
            model_id = m.get("id", "unknown")
            modified_at = str(m.get("created", "2025-08-01T00:00:00Z"))
            
        formatted.append({
            "name": model_id,
            "model": model_id,
            "modified_at": modified_at,
            "size": 0
        })
    return jsonify({"models": formatted})

@app.route('/api/show', methods=['POST'])
def show():
    req = request.get_json(force=True)
    model = req.get("model", "")
    print(f"[API] POST /api/show: {model}")
    return jsonify({
        "template": model,
        "capabilities": ["tools", "function_call"],
        "details": {"family": "gpt"},
        "model_info": {
            "general.basename": model,
            "general.architecture": "gpt",
            "gpt.context_length": 32768
        }
    })

@app.route('/v1/chat/completions', methods=['POST'])
def chat_completions():
    req = request.get_json(force=True)
    
    # Log request structure
    with open('logs/last_copilot_request.json', 'w') as f:
        json.dump(req, f, indent=2)
    
    messages = req.get("messages", [])
    model = req.get("model")
    
    print(f"[API] Requesting completion for model: {model}")

    # Call AI Engine
    # We use autodecide=True to let the engine pick the best provider if needed, 
    # or it will use the specific model if found.
    result = engine.chat_completion(
        messages=messages,
        model=model,
        autodecide=True
    )
    
    if not result.success:
        print(f"[API] AI Engine failed: {result.error_message}")
        return jsonify({"error": result.error_message}), 500
        
    print(f"[API] AI Engine success. Provider: {result.provider_used}, Model: {result.model_used}")
    
    # Save response for debugging
    with open('logs/last_model_response.json', 'w') as f:
        # Create a dict representation of the result
        response_debug = {
            "content": result.content,
            "provider": result.provider_used,
            "model": result.model_used,
            "raw_response": result.raw_response
        }
        json.dump(response_debug, f, indent=2)

    # Process content for function calls
    # This handles parsing <think> tags (stripping them) and finding tool calls
    function_results, cleaned_content = process_function_calls_in_response(result.content)
    
    # Generate fake streaming response
    def fake_stream_response():
        completion_id = f"chatcmpl-{int(time.time())}"
        created = int(time.time())
        
        # 1. Send Role
        yield f"data: {json.dumps({
            'id': completion_id,
            'object': 'chat.completion.chunk',
            'created': created,
            'model': result.model_used,
            'choices': [{'index': 0, 'delta': {'role': 'assistant'}, 'finish_reason': None}]
        })}\n\n"
        
        # 2. Send Content (if any)
        if cleaned_content:
            yield f"data: {json.dumps({
                'id': completion_id,
                'object': 'chat.completion.chunk',
                'created': created,
                'model': result.model_used,
                'choices': [{'index': 0, 'delta': {'content': cleaned_content}, 'finish_reason': None}]
            })}\n\n"
            
        # 3. Send Tool Calls (if any)
        if function_results:
            tool_calls = []
            for i, func_result in enumerate(function_results):
                tool_calls.append({
                    "id": f"call_{int(time.time())}_{i}",
                    "type": "function",
                    "function": {
                        "name": func_result["name"],
                        "arguments": json.dumps(func_result["result"])
                    }
                })
            
            yield f"data: {json.dumps({
                'id': completion_id,
                'object': 'chat.completion.chunk',
                'created': created,
                'model': result.model_used,
                'choices': [{'index': 0, 'delta': {'tool_calls': tool_calls}, 'finish_reason': None}]
            })}\n\n"
            
        # 4. Finish
        yield f"data: {json.dumps({
            'id': completion_id,
            'object': 'chat.completion.chunk',
            'created': created,
            'model': result.model_used,
            'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]
        })}\n\n"
        
        yield "data: [DONE]\n\n"

    return Response(stream_with_context(fake_stream_response()), mimetype="text/event-stream")


@app.route("/", defaults={"path": ""}, methods=["GET", "POST"])
@app.route("/<path:path>", methods=["GET", "POST"])
def catch_all(path):
    print(f"[API] WARNING: Hit unknown endpoint: {request.method} /{path}")
    return jsonify({"error": "Not implemented"}), 404

if __name__ == '__main__':
    print("== Custom AI Backend Server for Copilot BYOK with Function Execution starting on localhost:11434 ==")
    app.run(host="localhost", port=11434)