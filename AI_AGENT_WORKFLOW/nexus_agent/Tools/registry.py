#this file deals with the registry of tools that are available for the agent to use. It contains the ToolRegistry class which is used to register the tools and get the tool by name when it is being executed by the agent. The registry is a dictionary that maps the name of the tool to the tool object. This allows us to easily get the tool by name when it is being executed by the agent and also to display the list of available tools in the tool box when we are using openai api to display the tools in the tool box.
import logging
from pathlib import Path
from typing import Any
from nexus_agent.Tools.base import Tool, ToolInvokation, ToolResult
from nexus_agent.Tools.builtin import ReadFileTool, get_all_builtin_tools
from nexus_agent.Tools.subagents import SubAgentTool, get_default_subagents_definitions
from nexus_agent.config.config import Config
from nexus_agent.Tools.builtin.rag import RAGTool 
from nexus_agent.client.llm_client import LLMClient
from nexus_agent.hooks.hook_system import HookSystem
from nexus_agent.safety.approval import ApprovalContext, ApprovalDecision, ApprovalManager


logger = logging.getLogger(__name__) #logging is important for debugging and also for understanding the flow of the program. We will use logging to log the registration of tools and also to log any errors that occur during the registration of tools.
class ToolRegistry:
    def __init__(self,config:Config):
        self._tools: dict[str, Tool] = {}#this creates a dictionary that maps the name of the tool to the tool object. This allows us to easily get the tool by name when it is being executed by the agent and also to display the list of available tools in the tool box when we are using openai api to display the tools in the tool box.
        self._mcp_tools: dict[str, Tool] = {}
        self.config = config

    @property
    def connected_mcp_servers(self) -> list[Tool]:
        return self._mcp_tools.values()

    def register(self, tool: Tool):
        if tool.name in self._tools:
            logger.warning(f"Tool with name {tool.name} is already registered. Overwriting the existing tool.")
        self._tools[tool.name] = tool
        logger.debug(f"Registered tool: {tool.name}")
    
    def register_mcp_tool(self, tool: Tool):
        
        self._mcp_tools[tool.name] = tool
        logger.debug(f"Registered MCP tool: {tool.name}")
    
    def unregister(self, name: str):
        if name in self._tools:
            del self._tools[name]
            logger.debug(f"Unregistered tool: {name}")
            return True
        else:
            logger.warning(f"Tool with name {name} is not registered. Cannot unregister.")
            return False
    def get(self, name: str) -> Tool | None:
        if name in self._tools:
            return self._tools[name]
        elif name in self._mcp_tools:
            return self._mcp_tools[name]
        return None

    def get_tools(self) -> list[Tool]:
        tools: list[Tool] = []
        for tool in self._tools.values(): # this will iterate over the values of the dictionary which are the tool objects and append them to the tools list and then return the tools list.
            tools.append(tool)
        for mcp_tool in self._mcp_tools.values(): # this will iterate over the values of the dictionary which are the tool objects and append them to the tools list and then return the tools list.
            tools.append(mcp_tool)
        if self.config.allowed_tools:
            allowed_set = set(self.config.allowed_tools)
            tools = [t for t in tools if t.name in allowed_set]
        return tools
    def get_schema(self) -> list[dict[str, Any]]:
        return [tool.to_openai_schema() for tool in self.get_tools()]
    async def invoke(self, name: str, params:dict[str, Any], cwd:Path,hook_system:HookSystem,approval_manager:ApprovalManager | None = None) -> ToolResult:
        tool = self.get(name)
        if tool is None:
            result = ToolResult.error_result(
                f"Unknown tool {name}",
                metadata={"tool_name: " :name}
            )
            await hook_system.trigger_after_tool(name, params, result)
            return result
            
        validate_errors = tool.validate_params(params)
        if validate_errors:
            result = ToolResult.error_result(
                f"Invalid Parameter :{' '.join(validate_errors)}",
            )
            await hook_system.trigger_after_tool(name, params, result)
            return result
        
        await hook_system.trigger_before_tool(name, params)
        Invokation = ToolInvokation(
            cwd=cwd,
            params=params
        )
        if approval_manager:
            confirmation = await tool.get_confirmation(Invokation)
            if confirmation:
                context = ApprovalContext(
                    tool_name=name,params=params,is_mutating=tool.is_mutating(params),
                    affected_paths=confirmation.affected_paths,
                    command=confirmation.command,
                    is_dangerous=confirmation.is_dangerous,
                )
                decision = await approval_manager.check_approval(context)
                if decision == ApprovalDecision.REJECTED:
                    result = ToolResult.error_result(f"Operation was rejected by safety policy")
                    await hook_system.trigger_after_tool(name, params, result)
                    return result
                elif decision == ApprovalDecision.NEEDS_CONFIRMATION:
                    approval = approval_manager.request_confirmation(confirmation)
                    if not approval:
                        result = ToolResult.error_result("User rejected the operation")
                        await hook_system.trigger_after_tool(name, params, result)
                        return result
        try:
           result = await tool.execute(Invokation)
        except Exception as e:
            logger.exception(f"Tool {name} raised an unexpected error")
            result = ToolResult.error_result(
                f"internal error {e}",metadata={f"Tool_name": name}
            )
        await hook_system.trigger_after_tool(name, params, result)
        return result




def create_default_registry(config:Config) -> ToolRegistry:
    registry = ToolRegistry(config)
    llm_client = LLMClient(config)
    for tool_class in get_all_builtin_tools():
        if tool_class == RAGTool:
            registry.register(tool_class(config, llm_client))  
        else:
            registry.register(tool_class(config))
    for subagent_def in get_default_subagents_definitions():
        registry.register(SubAgentTool(config, subagent_def))
    return registry


