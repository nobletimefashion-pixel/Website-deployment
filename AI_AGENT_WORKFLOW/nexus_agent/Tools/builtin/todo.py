import uuid
from pydantic import BaseModel, Field
from nexus_agent.Tools.base import Tool, ToolInvokation, ToolKind, ToolResult
from nexus_agent.config.config import Config


class TodosParams(BaseModel):
    action: str = Field(
        ..., 
        description="Action: 'add','complete','list','clear'"
    )
    id: str | None = Field(None, description="Todo ID (for complete)")
    content: str | None = Field(None, description="Todo content (for add)")

class TodosTool(Tool):
    name = "todo"
    description = "Manage a task list for a current session. Use this to track progress on multi-step tasks."
    kind = ToolKind.MEMORY
    schema = TodosParams
    
    def __init__(self,config:Config) -> None:
        super().__init__(config)
        self._todos: dict[str,str] = {}
    
    async def execute(self, invocation:ToolInvokation) -> ToolResult:
        params = TodosParams(**invocation.params)
        
       
        if params.action.lower() == 'add':
            if not params.content:
                return ToolResult.error_result("'Content' required for 'add' action")
            todos_id = str(uuid.uuid4())[:8]
            self._todos[todos_id] = params.content
            return ToolResult.success_result(f"Added Todo [{todos_id}]: {params.content}")
        elif params.action.lower() == 'complete':
            if not params.id:
                return ToolResult.error_result("'id' required for 'complete' action")
            if params.id not in self._todos:
                return ToolResult.error_result(f"Todo not found: {params.id}")
            
            
            content = self._todos.pop(params.id)
            
            return ToolResult.success_result(f"Completed todo [{params.id}]:{params.content}")
        elif params.action.lower() == 'list':
            if not self._todos:
                return ToolResult.success_result("No todos")
            lines = ['Todos:']
            for todos_id,content in self._todos.items():
                lines.append(f"  [{todos_id}] {content}")
            return ToolResult.success_result("\n".join(lines))
        elif params.action.lower() == 'clear':
            count = len(self._todos)
            self._todos.clear()
            return ToolResult.success_result(f"Cleared {count} todos")
        else:
            return ToolResult.error_result(f"Unknown action: {params.action}")
            
            
            
            