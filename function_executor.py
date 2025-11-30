import re
import json
import subprocess
import os
import tempfile
import time

def parse_function_calls_from_text(content):
    """Parse function calls from text content for models that don't support native function calling"""
    function_calls = []
    
    try:
        # Remove <think> blocks to ignore reasoning
        content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
        
        # Check if this is a reasoning model response with <think> tags
        is_reasoning_model = '<tool_call>' in content
        
        if is_reasoning_model:
            print("[PARSER] Detected reasoning model response")
            # Handle reasoning model format
            function_calls.extend(parse_reasoning_model_calls(content))
        
        # Standard XML format: <invoke name="tool_name">...<parameter>...
        invoke_pattern = r'<invoke name="([^"]+)">(.*?)</invoke>'
        matches = re.finditer(invoke_pattern, content, re.DOTALL)
        
        for match in matches:
            func_name = match.group(1)
            params_text = match.group(2)
            
            # Parse parameters
            params = {}
            param_pattern = r'<parameter name="([^"]+)">([^<]*)</parameter>'
            param_matches = re.finditer(param_pattern, params_text)
            
            for param_match in param_matches:
                param_name = param_match.group(1)
                param_value = param_match.group(2).strip()
                params[param_name] = param_value
            
            function_calls.append({
                "name": func_name,
                "parameters": params
            })
            
    except Exception as e:
        print(f"[PARSER] Error parsing function calls: {e}")
    
    return function_calls

def parse_reasoning_model_calls(content):
    """Parse function calls from reasoning models that use different formats"""
    function_calls = []
    
    try:
        # Pattern 1: {"name": "tool_name", "arguments": {...}} - handle complex nested JSON
        tool_call_pattern = r'\{"name":\s*"([^"]+)",\s*"arguments":\s*(\{.*?\})\}'
        matches = re.finditer(tool_call_pattern, content, re.DOTALL)
        
        for match in matches:
            func_name = match.group(1)
            try:
                args_text = match.group(2)
                
                # Handle escaped quotes and newlines in the JSON
                args_text = args_text.replace('\\"', '"').replace('\\n', '\n').replace('\\\\', '\\')
                
                # Try to parse as JSON
                params = json.loads(args_text)
                
                # Map non-existent tools to real ones
                func_name = map_tool_name(func_name)
                
                function_calls.append({
                    "name": func_name,
                    "parameters": params
                })
                print(f"[PARSER] Successfully parsed {func_name} with {len(params)} parameters")
                
            except json.JSONDecodeError as e:
                print(f"[PARSER] Failed to parse JSON arguments for {func_name}: {e}")
                # Try manual parameter extraction
                params = extract_parameters_manually(args_text)
                if params:
                    func_name = map_tool_name(func_name)
                    function_calls.append({
                        "name": func_name,
                        "parameters": params
                    })
                    print(f"[PARSER] Manually extracted parameters for {func_name}")
        
        # Pattern 2: Look for code blocks that suggest file operations
        if not function_calls:
            function_calls.extend(extract_implicit_function_calls(content))
        
        # Pattern 3: JSON tool calls in code blocks
        code_block_pattern = r'```json\s*\n(.*?)\n```'
        code_matches = re.finditer(code_block_pattern, content, re.DOTALL)
        
        for match in code_matches:
            json_text = match.group(1).strip()
            try:
                tool_call = json.loads(json_text)
                if "tool" in tool_call:
                    func_name = tool_call["tool"]
                    params = {k: v for k, v in tool_call.items() if k != "tool"}
                    func_name = map_tool_name(func_name)
                    function_calls.append({
                        "name": func_name,
                        "parameters": params
                    })
                    print(f"[PARSER] Parsed tool call from code block: {func_name}")
            except json.JSONDecodeError as e:
                print(f"[PARSER] Failed to parse JSON from code block: {e}")
            
    except Exception as e:
        print(f"[PARSER] Error parsing reasoning model calls: {e}")
    
    return function_calls

def extract_parameters_manually(args_text):
    """Manually extract parameters when JSON parsing fails"""
    params = {}
    
    try:
        # Extract filePath
        file_path_match = re.search(r'"filePath":\s*"([^"]+)"', args_text)
        if file_path_match:
            params["filePath"] = file_path_match.group(1)
        
        # Extract explanation
        explanation_match = re.search(r'"explanation":\s*"([^"]+)"', args_text)
        if explanation_match:
            params["explanation"] = explanation_match.group(1)
        
        # Extract code (this is trickier due to escaping)
        code_match = re.search(r'"code":\s*"([^"]+(?:\\.[^"]*)*)"', args_text)
        if code_match:
            code = code_match.group(1)
            # Unescape the code
            code = code.replace('\\"', '"').replace('\\n', '\n').replace('\\\\', '\\')
            params["code"] = code
        
    except Exception as e:
        print(f"[PARSER] Error in manual parameter extraction: {e}")
    
    return params

def map_tool_name(tool_name):
    """Map non-existent tool names to real ones"""
    tool_mapping = {
        "edit_file": "replace_string_in_file",
        "write_file": "create_file",
        "save_file": "create_file",
        "execute_command": "run_in_terminal",
        "run_command": "run_in_terminal",
        "terminal": "run_in_terminal"
    }
    
    mapped_name = tool_mapping.get(tool_name, tool_name)
    if mapped_name != tool_name:
        print(f"[PARSER] Mapped {tool_name} -> {mapped_name}")
    
    return mapped_name

def extract_implicit_function_calls(content):
    """Extract implicit function calls from reasoning model responses"""
    function_calls = []
    
    # Look for file creation patterns
    file_creation_pattern = r'create.*?file.*?["\']([^"\']+)["\']'
    matches = re.finditer(file_creation_pattern, content, re.IGNORECASE)
    
    for match in matches:
        file_path = match.group(1)
        # Look for code blocks near this pattern
        code_blocks = re.findall(r'```(?:python|py)?\n(.*?)```', content, re.DOTALL)
        if code_blocks:
            function_calls.append({
                "name": "create_file",
                "parameters": {
                    "filePath": file_path,
                    "content": code_blocks[0].strip()
                }
            })
    
    return function_calls

def clean_content_for_display(content):
    """Clean reasoning model content for display to user"""
    # Remove <think> tags and their content
    content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL)
    
    # Remove tool call JSON
    content = re.sub(r'\{"name":\s*"[^"]+",\s*"arguments":\s*\{[^}]*\}\}', '', content)
    
    # Remove code blocks with JSON
    content = re.sub(r'```json\s*\n.*?\n```', '', content, flags=re.DOTALL)
    
    # Remove XML tool calls
    content = re.sub(r'<invoke name="[^"]+">.*?</invoke>', '', content, flags=re.DOTALL)
    
    # Clean up extra whitespace
    content = re.sub(r'\n\s*\n', '\n\n', content)
    content = content.strip()
    
    return content

def execute_function_call(func_name, params):
    """Execute the actual function call and return results"""
    try:
        if func_name == "create_file":
            return execute_create_file(params)
        elif func_name == "run_in_terminal":
            return execute_run_in_terminal(params)
        elif func_name == "read_file":
            return execute_read_file(params)
        elif func_name == "replace_string_in_file":
            return execute_replace_string_in_file(params)
        elif func_name == "insert_edit_into_file":
            return execute_insert_edit_into_file(params)
        elif func_name == "list_dir":
            return execute_list_dir(params)
        else:
            return {"error": f"Function {func_name} not supported"}
    except Exception as e:
        return {"error": f"Error executing {func_name}: {str(e)}"}

def execute_insert_edit_into_file(params):
    """Handle insert_edit_into_file by converting to replace_string_in_file"""
    file_path = params.get("filePath")
    code = params.get("code", "")
    explanation = params.get("explanation", "")
    
    if not file_path:
        return {"error": "filePath parameter required"}
    
    try:
        # Read the existing file
        if os.path.exists(file_path):
            with open(file_path, 'r', encoding='utf-8') as f:
                existing_content = f.read()
            
            # Find the function to replace based on the code pattern
            # This is a smart replacement - look for the function signature
            lines = code.strip().split('\n')
            if lines:
                first_line = lines[0].strip()
                if 'def ' in first_line:
                    # Extract function name
                    func_match = re.search(r'def\s+(\w+)', first_line)
                    if func_match:
                        func_name = func_match.group(1)
                        
                        # Find existing function in file
                        pattern = rf'def\s+{func_name}\s*\([^)]*\):\s*.*?(?=\n\s*def|\n\s*class|\n\s*$|\Z)'
                        match = re.search(pattern, existing_content, re.DOTALL)
                        
                        if match:
                            # Replace existing function
                            new_content = existing_content.replace(match.group(0), code.strip())
                            with open(file_path, 'w', encoding='utf-8') as f:
                                f.write(new_content)
                            return {"success": f"Function {func_name} updated in {file_path}"}
            
            # If no function found, append the code
            with open(file_path, 'a', encoding='utf-8') as f:
                f.write('\n\n' + code)
            return {"success": f"Code appended to {file_path}"}
        else:
            # Create new file
            return execute_create_file({"filePath": file_path, "content": code})
            
    except Exception as e:
        return {"error": f"Failed to edit file: {str(e)}"}

def execute_create_file(params):
    """Create a file with given content"""
    file_path = params.get("filePath") or params.get("filename")
    content = params.get("content", "")
    
    if not file_path:
        return {"error": "filePath (or filename) parameter required"}
    
    try:
        # Create directory if it doesn't exist (only if file is in a subdirectory)
        dir_path = os.path.dirname(file_path)
        if dir_path and dir_path != "":
            os.makedirs(dir_path, exist_ok=True)
        
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        
        return {"success": f"File {file_path} created successfully"}
    except Exception as e:
        return {"error": f"Failed to create file: {str(e)}"}

def execute_run_in_terminal(params):
    """Run a command in terminal"""
    command = params.get("command")
    explanation = params.get("explanation", "")
    is_background = params.get("isBackground", "false").lower() == "true"
    
    if not command:
        return {"error": "command parameter required"}
    
    try:
        if is_background:
            # Start background process
            process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, 
                                     stderr=subprocess.PIPE, text=True)
            return {"success": f"Background command started: {command}", "pid": process.pid}
        else:
            # Run and wait for completion
            result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
            return {
                "success": f"Command executed: {command}",
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode
            }
    except subprocess.TimeoutExpired:
        return {"error": "Command timed out"}
    except Exception as e:
        return {"error": f"Failed to run command: {str(e)}"}

def execute_read_file(params):
    """Read file contents"""
    file_path = params.get("filePath") or params.get("filename")
    
    if not file_path:
        return {"error": "filePath (or filename) parameter required"}
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        return {"success": f"File {file_path} read successfully", "content": content}
    except Exception as e:
        return {"error": f"Failed to read file: {str(e)}"}

def execute_replace_string_in_file(params):
    """Replace string in file"""
    file_path = params.get("filePath") or params.get("filename")
    old_string = params.get("oldString")
    new_string = params.get("newString")
    
    if not all([file_path, old_string is not None, new_string is not None]):
        return {"error": "filePath (or filename), oldString, and newString parameters required"}
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        if old_string not in content:
            return {"error": "oldString not found in file"}
        
        new_content = content.replace(old_string, new_string)
        
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        
        return {"success": f"String replaced in {file_path}"}
    except Exception as e:
        return {"error": f"Failed to replace string: {str(e)}"}

def execute_list_dir(params):
    """List directory contents"""
    path = params.get("path", ".")
    
    try:
        items = os.listdir(path)
        return {"success": f"Directory {path} listed", "items": items}
    except Exception as e:
        return {"error": f"Failed to list directory: {str(e)}"}