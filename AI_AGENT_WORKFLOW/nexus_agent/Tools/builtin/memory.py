#this file is used for memory for llm to remember a users prefrence by creating a log file in the users system and fetching it and append it to  system log
#1153


import json
import os
from pathlib import Path
import re
from pydantic import BaseModel, Field
from nexus_agent.Tools.base import Tool, ToolInvokation, ToolKind, ToolResult
from nexus_agent.config.loader import get_data_dir
from nexus_agent.utils.path import is_binary_file, resolve_path


class MemoryParams(BaseModel):
    action: str = Field(
        ..., 
        description="Action: 'set','get','delete','list','clear'"
    )
    key: str = Field('.', description="Memory key (required for `set`,`get`,`delete`")
    value: str | None = Field(False,description='Value to store (required for `set`)')

class MemoryTool(Tool):
    name = "memory"
    description="Store and retrive presistent memory. Use this to remember user prefrences, important context or notes."
    kind = ToolKind.READ
    schema = MemoryParams
    
    def _load_memory(self) -> dict:
        data_dir = get_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        path = data_dir / "user_memory.json"
        
        if not path.exists():
            return {'entries': {}}
        try:
            content = path.read_text(encoding='utf-8')
            return json.loads(content)
        except Exception:
            return {'entries': {}}
    def _save_memory(self, memory: dict) -> None:
        data_dir = get_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        path = data_dir / "user_memory.json"
        
        path.write_text(json.dumps(memory,indent=2, ensure_ascii=False))
    
    async def execute(self, invocation:ToolInvokation) -> ToolResult:
        params = MemoryParams(**invocation.params)
        
       
        if params.action.lower() == 'set':
            if not params.key or not params.value:
                return ToolResult.error_result("`key` and `value` required for `set` action")
            memory = self._load_memory()
            memory['entries'][params.key] = params.value
            self._save_memory(memory)
            return ToolResult.success_result(f"set memory: {params.key}")
        elif params.action.lower() == 'get':
            if not params.key:
                return ToolResult.error_result("'key' required for 'get' action")
            
            memory = self._load_memory()
            if params.key not in memory.get('entries',{}):
                return ToolResult.success_result(f"Memory not found: {params.key}",metadata={'found':False,})
            return ToolResult.success_result(f"Memory found: {params.key}: {memory['entries'][params.key]}",metadata={'found':True,})
        elif params.action.lower() == 'delete':
            if not params.key:
                return ToolResult.error_result("'key' required for 'delete' action")
            memory = self._load_memory()
            if params.key not in memory.get('entries',{}):
                return ToolResult.success_result(f"Memory not found: {params.key}",metadata={'found':False,})
            del memory['entries'][params.key]
            self._save_memory(memory)
            return ToolResult.success_result(f"Deleted memory: {params.key}")
        elif params.action.lower() == 'list':
            memory = self._load_memory()
            entries = memory.get('entries', {})
            if not entries:
                return ToolResult.success_result(f"Not memories stored",metadata={'found':False,})
            lines = [f"Stored memories:"]
            for key,value in sorted(entries.items()):
                lines.append(f" {key}: {value}")
            
            return ToolResult.error_result("\n".join(lines),metadata={'found':True,})
        elif params.action.lower() == 'clear':
            memory = self._load_memory()
            count = len(memory.get('entries', {}))
            memory['entries'] = {}
            self._save_memory(memory)
            
            return ToolResult.success_result(f"Cleared {count} memory entries")
        else:
            return ToolResult.error_result(f"Unknown action: {params.action}")
            
            
            
            