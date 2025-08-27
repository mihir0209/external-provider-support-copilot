import time
import requests
from flask import Flask, request, jsonify, Response, stream_with_context
import json
import re
import os
import subprocess
import tempfile
app = Flask(__name__)
import dotenv

dotenv.load_dotenv()

A4F_API_KEY = os.getenv("A4F_API_KEY") # Configure the API key in .env directly or just paste the key in quotes here.
MODEL_LIST_ENDPOINT = "https://api.a4f.co/v1/models"
CHAT_COMPLETION_ENDPOINT = "https://api.a4f.co/v1/chat/completions"

class AIEngine:
    MAX_RETRIES = 3
    BASE_BACKOFF = 5
    COOLDOWN_SECONDS = 10
    _api_call_count = 0
    _last_cooldown_time = 0

    def __init__(self, api_key, chat_endpoint):
        self.api_key = api_key
        self.chat_endpoint = chat_endpoint

    def _maybe_cooldown(self):
        self.__class__._api_call_count += 1
        if self.__class__._api_call_count % 5 == 0:
            now = time.time()
            if now - self.__class__._last_cooldown_time > 1:
                print(f"[AIEngine] ⏳ cooldown: {self.COOLDOWN_SECONDS}s after 5 requests...")
                for i in range(self.COOLDOWN_SECONDS, 0, -1):
                    print(f"[AIEngine]   ...{i}s remaining", end='\r', flush=True)
                    time.sleep(1)
                print("[AIEngine]   ...0s remaining          ")
                self.__class__._last_cooldown_time = time.time()

    def list_models(self):
        try:
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            r = requests.get(MODEL_LIST_ENDPOINT, headers=headers, timeout=15)
            print(f"[AIEngine] Model list: {r.status_code} {r.text[:200]}")
            if r.status_code != 200:
                return []
            model_data = r.json()
            return model_data.get("data", [])
        except Exception as e:
            print(f"[AIEngine] Model listing exception: {e}")
            return []

    def relay_completion(self, payload, stream=False):
        self._maybe_cooldown()
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        try:
            if stream:
                with requests.post(self.chat_endpoint, headers=headers, json=payload, stream=True, timeout=80) as r:
                    def gen():
                        for line in r.iter_lines():
                            if line:
                                # Proper SSE chunk relay.
                                text = line.decode()
                                if not text.startswith("data: "):
                                    text = "data: " + text
                                yield text + "\n\n"
                    return gen, r.status_code
            else:
                r = requests.post(self.chat_endpoint, headers=headers, json=payload, timeout=60)
                print(f"[AIEngine][relay_completion] status: {r.status_code} body: {r.text[:200]}")
                return r.json(), r.status_code
        except Exception as e:
            print(f"[AIEngine] relay_completion exception: {e}")
            if stream:
                def gen_error():
                    import json
                    err = {
                        "error": {"message": str(e)},
                        "object": "error"
                    }
                    yield f"data: {json.dumps(err)}\n\n"
                    yield "data: [DONE]\n\n"
                return gen_error, 500
            else:
                return {"error": str(e)}, 500

engine = AIEngine(A4F_API_KEY, CHAT_COMPLETION_ENDPOINT)

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
    models = engine.list_models()
    formatted = []
    for m in models:
        formatted.append({
            "name": m.get("id", "unknown"),
            "model": m.get("id", "unknown"),
            "modified_at": m.get("created", "2025-08-01T00:00:00Z"),
            "size": m.get("size", 0)
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
    
@app.route('/v1/chat/completions', methods=['POST'])
def chat_completions():
    req = request.get_json(force=True)
    
    # Create a text-free version of the request for structural analysis
    # req_structure = strip_text_values(req)
    
    # print("[API] POST /v1/chat/completions (Structure Only):")
    # print(json.dumps(req_structure))
    
    # Validate request
    if not req.get("model"):
        req["model"] = "gpt-4" # Fallback model if none provided
    
    # Make sure messages have valid roles
    for msg in req.get("messages", []):
        if not msg.get("role"):
            msg["role"] = "user"
    
    headers = {
        "Authorization": f"Bearer {A4F_API_KEY}",
        "Content-Type": "application/json"
    }

    # Check if tools are present - if so, disable streaming in the outgoing request
    has_tools = bool(req.get("tools"))
    if has_tools:
        print("[API] Tools detected - disabling streaming for provider request")
        # Create a copy of the request without streaming
        provider_req = req.copy()
        provider_req["stream"] = False
        if "stream_options" in provider_req:
            del provider_req["stream_options"]
        use_streaming = False
    else:
        provider_req = req
        use_streaming = req.get("stream", False)

    # Make request to provider:
    try:
        if use_streaming:
            # True streaming mode (no tools)
            with requests.post(
                CHAT_COMPLETION_ENDPOINT,
                headers=headers,
                json=provider_req,
                timeout=60,
                stream=True
            ) as r:
                if r.status_code != 200:
                    print(f"[API] Provider API Error: {r.status_code} - {r.text}")
                    def error_stream():
                        err = {"error": f"Provider API Error: {r.status_code}", "details": r.text}
                        yield f"data: {json.dumps(err)}\n\n"
                        yield "data: [DONE]\n\n"
                    return Response(stream_with_context(error_stream()), mimetype="text/event-stream"), r.status_code
                
                def stream_response():
                    print("[API] Starting to stream response from provider")
                    for line in r.iter_lines():
                        if not line:
                            continue
                            
                        text = line.decode()
                        if text.strip() == "data: [DONE]":
                            yield text + "\n\n"
                            print("[API] Completed streaming response")
                            break
                            
                        # Ensure proper SSE formatting
                        if not text.startswith("data:"):
                            text = "data: " + text
                        
                        # Process the chunk to match expected format
                        try:
                            if text.startswith("data:"):
                                data = text[5:].strip()
                                chunk = json.loads(data)
                                
                                # Add any missing required fields
                                if "id" not in chunk:
                                    chunk["id"] = f"chatcmpl-{int(time.time())}"
                                if "object" not in chunk:
                                    chunk["object"] = "chat.completion.chunk"
                                if "created" not in chunk:
                                    chunk["created"] = int(time.time())
                                if "model" not in chunk:
                                    chunk["model"] = req.get("model") or "provider-model"
                                    
                                # Make sure it has proper choices format
                                if "choices" in chunk and chunk["choices"]:
                                    # Ensure it has required fields
                                    if "finish_reason" not in chunk["choices"][0]:
                                        chunk["choices"][0]["finish_reason"] = None
                                    if "index" not in chunk["choices"][0]:
                                        chunk["choices"][0]["index"] = 0
                                
                                # Reconstruct the formatted chunk
                                text = f"data: {json.dumps(chunk)}\n\n"
                        except Exception as e:
                            print(f"[API] Warning: Error processing chunk: {e}")
                        
                        yield text
                    
                    # Send any usage information at the end if requested
                    if req.get("stream_options", {}).get("include_usage", False):
                        usage_chunk = {
                            "id": f"chatcmpl-usage-{int(time.time())}",
                            "object": "chat.completion.chunk.usage",
                            "created": int(time.time()),
                            "model": req.get("model") or "provider-model",
                            "usage": {
                                "prompt_tokens": 0,  # These would ideally be real values
                                "completion_tokens": 0,
                                "total_tokens": 0
                            }
                        }
                        yield f"data: {json.dumps(usage_chunk)}\n\n"
                    
                    yield "data: [DONE]\n\n"

                print("[API] Returning SSE streaming to Copilot.")
                return Response(stream_with_context(stream_response()), mimetype="text/event-stream")
        
        else:
            # Non-streaming mode or tools present - get complete response and fake stream it
            print("[API] Using non-streaming mode (tools present or streaming disabled)")
            r = requests.post(
                CHAT_COMPLETION_ENDPOINT,
                headers=headers,
                json=provider_req,
                timeout=60
            )
            
            if r.status_code != 200:
                print(f"[API] Provider API Error: {r.status_code} - {r.text}")
                def error_stream():
                    error_response = {
                        "id": f"error-{int(time.time())}",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": req.get("model", "provider-model"),
                        "choices": [{
                            "index": 0,
                            "delta": {
                                "content": f"Error: Provider API Error {r.status_code}"
                            },
                            "finish_reason": "error"
                        }]
                    }
                    yield f"data: {json.dumps(error_response)}\n\n"
                    yield "data: [DONE]\n\n"
                return Response(stream_with_context(error_stream()), mimetype="text/event-stream"), r.status_code
            
            # Process the complete response
            try:
                response_data = r.json()
                print(f"[API] Received complete response: {json.dumps(response_data, indent=2)[:500]}...")
            except:
                print(f"[API] Failed to parse JSON response: {r.text[:200]}")
                response_data = {"choices": []}
                
            # Convert complete response to streaming format
            def fake_stream_response():
                # Check if we have valid choices
                if not response_data.get("choices") or len(response_data["choices"]) == 0:
                    print("[API] No choices in response - creating default response")
                    # Create a default response if no choices
                    default_response = {
                        "id": f"chatcmpl-{int(time.time())}",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": req.get("model", "provider-model"),
                        "choices": [{
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "content": "I apologize, but I'm unable to provide a response at this moment. Please try again."
                            },
                            "finish_reason": "stop"
                        }]
                    }
                    yield f"data: {json.dumps(default_response)}\n\n"
                else:
                    choice = response_data["choices"][0]
                    content = ""
                    
                    # Extract content from the response
                    if "message" in choice and choice["message"].get("content"):
                        content = choice["message"]["content"]
                    elif "text" in choice:
                        content = choice["text"]
                    
                    # Handle tool calls if present
                    tool_calls = None
                    if "message" in choice and choice["message"].get("tool_calls"):
                        tool_calls = choice["message"]["tool_calls"]
                    
                    # Send role first if we have content or tool calls
                    if content or tool_calls:
                        role_chunk = {
                            "id": response_data.get("id", f"chatcmpl-{int(time.time())}"),
                            "object": "chat.completion.chunk",
                            "created": response_data.get("created", int(time.time())),
                            "model": response_data.get("model", req.get("model", "provider-model")),
                            "choices": [{
                                "index": 0,
                                "delta": {"role": "assistant"},
                                "finish_reason": None
                            }]
                        }
                        yield f"data: {json.dumps(role_chunk)}\n\n"
                    
                    # Send content if available
                    if content:
                        content_chunk = {
                            "id": response_data.get("id", f"chatcmpl-{int(time.time())}"),
                            "object": "chat.completion.chunk",
                            "created": response_data.get("created", int(time.time())),
                            "model": response_data.get("model", req.get("model", "provider-model")),
                            "choices": [{
                                "index": 0,
                                "delta": {"content": content},
                                "finish_reason": None
                            }]
                        }
                        yield f"data: {json.dumps(content_chunk)}\n\n"
                    
                    # Send tool calls if available
                    if tool_calls:
                        tool_chunk = {
                            "id": response_data.get("id", f"chatcmpl-{int(time.time())}"),
                            "object": "chat.completion.chunk",
                            "created": response_data.get("created", int(time.time())),
                            "model": response_data.get("model", req.get("model", "provider-model")),
                            "choices": [{
                                "index": 0,
                                "delta": {"tool_calls": tool_calls},
                                "finish_reason": None
                            }]
                        }
                        yield f"data: {json.dumps(tool_chunk)}\n\n"
                
                # Final chunk with finish_reason
                final_chunk = {
                    "id": response_data.get("id", f"chatcmpl-{int(time.time())}"),
                    "object": "chat.completion.chunk",
                    "created": response_data.get("created", int(time.time())),
                    "model": response_data.get("model", req.get("model", "provider-model")),
                    "choices": [{
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop"
                    }]
                }
                yield f"data: {json.dumps(final_chunk)}\n\n"
                
                # Send usage information if requested
                if req.get("stream_options", {}).get("include_usage", False):
                    usage_data = response_data.get("usage", {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0
                    })
                    usage_chunk = {
                        "id": f"chatcmpl-usage-{int(time.time())}",
                        "object": "chat.completion.chunk.usage",
                        "created": int(time.time()),
                        "model": req.get("model") or "provider-model",
                        "usage": usage_data
                    }
                    yield f"data: {json.dumps(usage_chunk)}\n\n"
                
                yield "data: [DONE]\n\n"
            
            print("[API] Returning fake SSE streaming to Copilot.")
            return Response(stream_with_context(fake_stream_response()), mimetype="text/event-stream")

    except Exception as e:
        print(f"[API] Exception forwarding request: {e}")
        def error_stream():
            # Format error as a proper OpenAI-compatible error response
            error_response = {
                "id": f"error-{int(time.time())}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": req.get("model", "provider-model"),
                "choices": [{
                    "index": 0,
                    "delta": {
                        "content": f"Error: {str(e)}"
                    },
                    "finish_reason": "error"
                }]
            }
            yield f"data: {json.dumps(error_response)}\n\n"
            yield "data: [DONE]\n\n"
        return Response(stream_with_context(error_stream()), mimetype="text/event-stream"), 500




@app.route("/", defaults={"path": ""}, methods=["GET", "POST"])
@app.route("/<path:path>", methods=["GET", "POST"])
def catch_all(path):
    print(f"[API] WARNING: Hit unknown endpoint: {request.method} /{path}")
    return jsonify({"error": "Not implemented"}), 404

if __name__ == '__main__':
    print("== Custom AI Backend Server for Copilot BYOK starting on localhost:11434 ==")
    app.run(host="localhost", port=11434)
