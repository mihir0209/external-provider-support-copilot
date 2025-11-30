import requests
import json
import time
import os
import sys

BASE_URL = "http://localhost:11434"

def print_pass(message):
    print(f"✅ PASS: {message}")

def print_fail(message, details=""):
    print(f"❌ FAIL: {message}")
    if details:
        print(f"   Details: {details}")

def test_version():
    print("\n--- Testing Version Endpoint ---")
    try:
        response = requests.get(f"{BASE_URL}/api/version")
        if response.status_code == 200:
            data = response.json()
            if "version" in data:
                print_pass(f"Version: {data['version']}")
            else:
                print_fail("Version key missing in response")
        else:
            print_fail(f"Status code: {response.status_code}")
    except Exception as e:
        print_fail(f"Exception: {e}")

def test_list_models():
    print("\n--- Testing List Models Endpoint ---")
    try:
        response = requests.get(f"{BASE_URL}/api/tags")
        if response.status_code == 200:
            data = response.json()
            if "models" in data:
                models = data["models"]
                print_pass(f"Found {len(models)} models")
                if len(models) > 0:
                    print(f"   Sample model: {models[0]['name']}")
                return models
            else:
                print_fail("Models key missing")
        else:
            print_fail(f"Status code: {response.status_code}")
    except Exception as e:
        print_fail(f"Exception: {e}")
    return []

def test_show_model(model_name):
    print(f"\n--- Testing Show Model Endpoint ({model_name}) ---")
    try:
        response = requests.post(f"{BASE_URL}/api/show", json={"model": model_name})
        if response.status_code == 200:
            data = response.json()
            if "model_info" in data:
                print_pass("Model info received")
            else:
                print_fail("model_info missing")
        else:
            print_fail(f"Status code: {response.status_code}")
    except Exception as e:
        print_fail(f"Exception: {e}")

def test_chat_basic():
    print("\n--- Testing Basic Chat Completion ---")
    payload = {
        "messages": [{"role": "user", "content": "Say 'Hello World' and nothing else."}],
        "stream": True # Copilot usually requests stream
    }
    
    try:
        print("   Sending request...")
        response = requests.post(f"{BASE_URL}/v1/chat/completions", json=payload, stream=True)
        
        if response.status_code == 200:
            full_content = ""
            raw_lines = []
            for line in response.iter_lines():
                if line:
                    line = line.decode('utf-8')
                    raw_lines.append(line)
                    if line.startswith("data: ") and line != "data: [DONE]":
                        try:
                            json_str = line[6:]
                            chunk = json.loads(json_str)
                            if "choices" in chunk and len(chunk["choices"]) > 0:
                                delta = chunk["choices"][0].get("delta", {})
                                if "content" in delta:
                                    full_content += delta["content"]
                        except:
                            pass
            
            # Log raw response for debugging
            with open("test_chat_basic_response.log", "w", encoding="utf-8") as f:
                f.write("\n".join(raw_lines))
            
            print_pass(f"Response received: {full_content.strip()}")
            if "Hello World" in full_content:
                print_pass("Content verification successful")
            else:
                print(f"   Note: Content was '{full_content.strip()}'")
                print(f"   Raw response saved to test_chat_basic_response.log")
        else:
            print_fail(f"Status code: {response.status_code}")
            print(response.text)
    except Exception as e:
        print_fail(f"Exception: {e}")

def test_function_calling():
    print("\n--- Testing Function Calling (Tool Execution) ---")
    # We will ask it to create a file.
    filename = "test_tool_output.txt"
    content_to_write = f"Timestamp: {time.time()}"
    
    # Clean up if exists
    if os.path.exists(filename):
        try:
            os.remove(filename)
        except:
            pass
        
    payload = {
        "messages": [
            {"role": "system", "content": "You are a helpful assistant that can execute tools. When asked to create a file, use the create_file tool."},
            {"role": "user", "content": f"Please create a file named '{filename}' with the content '{content_to_write}'."}
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "create_file",
                    "description": "Create a new file with the specified content",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "filePath": {
                                "type": "string",
                                "description": "The absolute or relative path to the file"
                            },
                            "content": {
                                "type": "string",
                                "description": "The content to write to the file"
                            }
                        },
                        "required": ["filePath", "content"]
                    }
                }
            }
        ],
        "stream": True
    }
    
    try:
        print(f"   Requesting file creation: {filename}")
        response = requests.post(f"{BASE_URL}/v1/chat/completions", json=payload, stream=True)
        
        if response.status_code == 200:
            # Consume the stream
            full_response = ""
            raw_lines = []
            for line in response.iter_lines():
                if line:
                    line = line.decode('utf-8')
                    raw_lines.append(line)
                    if line.startswith("data: ") and line != "data: [DONE]":
                        full_response += line
            
            # Log raw response for debugging
            with open("test_function_calling_response.log", "w", encoding="utf-8") as f:
                f.write("\n".join(raw_lines))
            
            print("   Request completed.")
            
            # Check if file exists
            if os.path.exists(filename):
                with open(filename, 'r') as f:
                    content = f.read()
                if content == content_to_write:
                    print_pass(f"File created with correct content: {content}")
                else:
                    print_fail(f"File created but content mismatch. Expected '{content_to_write}', got '{content}'")
            else:
                print_fail(f"File '{filename}' was not created.")
                print("   Note: This might depend on the model's ability to call the tool correctly.")
                print(f"   Raw response saved to test_function_calling_response.log")
        else:
            print_fail(f"Status code: {response.status_code}")
            print(response.text)
            
    except Exception as e:
        print_fail(f"Exception: {e}")

if __name__ == "__main__":
    print("Starting Server Tests...")
    print(f"Target: {BASE_URL}")
    
    # Check if server is up
    try:
        requests.get(f"{BASE_URL}/api/version", timeout=2)
    except requests.exceptions.ConnectionError:
        print_fail("Server is not running. Please start server.py first.")
        sys.exit(1)

    test_version()
    models = test_list_models()
    
    if models:
        test_show_model(models[0]['name'])
    else:
        test_show_model("gpt-4")
        
    test_chat_basic()
    test_function_calling()
    
    print("\nTests Completed.")
