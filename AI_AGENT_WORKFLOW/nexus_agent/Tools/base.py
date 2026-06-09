from __future__ import annotations
import abc #for using abstract class
from enum import Enum
from pydantic import BaseModel, ValidationError # helps in validation 
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

from nexus_agent.config.config import Config
#from pydantic import model_json_schema #to convert pydantic model to json schema which is required by openai api to display the tool in the tool box


#this file contains the base class for all the tools that we will be using in our agent. This class will be inherited by all the tools that we will be creating and it will contain the common functionality that all the tools will have like validation of parameters, getting confirmation for mutating tools and converting the tool to openai schema format.
#and also the ToolResult class which will be used to return the result of the tool execution and the ToolInvokation class which will be used to pass the parameters and the current working directory to the tool when it is being executed. The ToolConfirmation class will be used to display a confirmation dialog to the user before executing a mutating tool.
#The ToolKind enum will be used to define the type of the tool whether it is a read only tool, a write tool, a shell tool, a network tool, a memory tool or an mcp tool. This will be used to determine whether the tool is mutating or not and whether we need to ask for confirmation before executing the tool or not.

class ToolKind(str, Enum):
    READ = "read"
    WRITE = "write"
    SHELL = "shell"
    NETWORK = "network"
    MEMORY = "memory"
    MCP = "mcp"
    
@dataclass
class FileDiff:
    path: Path
    old_content: str  
    new_content: str
    
    is_new_file: bool = False
    is_deletion:bool = False
    
    def create_diff(self) -> str:
        import difflib
        old_lines = self.old_content.splitlines(keepends=True)
        new_lines = self.new_content.splitlines(keepends=True)
        
        if old_lines and not  old_lines[-1].endswith('\n'):
            old_lines[-1] += '\n'
        if new_lines and not  new_lines[-1].endswith('\n'):
            new_lines[-1] += '\n'
            
        old_name = '/dev/null' if self.is_new_file else str(self.path)
        new_name = '/dev/null' if self.is_deletion else str(self.path)
        
        diff = difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=old_name,
            tofile=new_name,
        )
        
        return "".join(diff)
    
@dataclass
class ToolResult:
    success:bool
    output:str
    error:str | None = None
    metadata: dict[str,Any]  = field(default_factory=dict) 
    
    truncated: bool = False
    diff: FileDiff | None = None
    exit_code: int | None = None
    
    @classmethod#classmethod is used to define a method that is bound to the class and not the instance of the class. This means
    def error_result(#this function will be used to create a ToolResult object with success set to False and the error message set to the error parameter that is passed to the function. This will be used to return an error result when there is an error in executing the tool.
        cls,
        error: str,
        output: str = "",
        metadata=None,
        **kwargs: Any,
    ):
        return cls(success=False, output=output, error=error, metadata={},**kwargs)
    
    @classmethod#classmethod is used to define a method that is bound to the class and not the instance of the class. This means
    def success_result(#this function will be used to create a ToolResult object with success set to True and the output message set to the output parameter that is passed to the function. This will be used to return a success result when the tool is executed successfully.
        cls,
        output: str,
        **kwargs: Any,
    ):
        return cls(success=True, output=output, error=None, **kwargs)
    
    
    def to_model_output(self) -> str:
        if self.success:
            return self.output
        return f"Error : {self.error}\n\nOutput:\n{self.output}"
@dataclass 
class ToolInvokation:
    cwd: Path
    params: dict[str, Any] = field(default_factory=dict)

@dataclass
class ToolConfirmation:
    tool_name: str
    params: dict[str,Any]
    description: str
    diff: FileDiff | None = None
    affected_paths: list[Path] = field(default_factory=list)
    command: str | None = None
    is_dangerous: bool = False
    
class Tool(abc.ABC): #for using abstract class
    name:str = "base_tool"
    description: str = "Base tool"
    kind: ToolKind = ToolKind.READ
    def __init__(self,config:Config) -> None:
        self.config = config
    
    @property    #it will return dict for mcp tools and basemodel for others like our own tools as they will all have basemodels
    def schema(self) -> dict[str, Any] | type['BaseModel']:
        raise NotADirectoryError("tool must define schema property or class attribute")
    
    @abc.abstractmethod
    async def execute(self, invokation: ToolInvokation) -> ToolResult:
        pass
    #checks if parameters are correct like if get_wheather("paris") but if we do get_wheather("paris 23") integer it will valitade an error
    def validate_params(self, params: dict[str, Any]) -> list[str]:
        schema = self.schema
        #to check for pydantic model checks (x, (A,B,...)) as it automatically does validation
        if isinstance(schema, type) and issubclass(schema, BaseModel):
            try:
               schema(**params)# passing all the data like we could do BaseModel(**params) and params tells path of the file to read,write,edit and more info that is declared in schema
            except ValidationError as e:#Now if there are any error we will now give it to the model so that the model can understand and improve
                errors = []
                for error in e.errors():
                    field = ".".join(str(x) for x in error.get("loc",[]))#extracting location from error field to find where the error came from
                    msg = error.get("msg","ValidationError")
                    errors.append(f"params '{field}' : {msg}")
                return errors
            except Exception as e:
                return [str(e)]
            
        #if there is no baseModel and is a dict then
        return []
    #this function will check if the tool is mutating or not like if it is read only tool then it will return false and if it is write tool then it will return true
    def is_mutating(self, params:dict[str, Any]) -> bool:
        #so if its either one of these then it is mutating tool otherwise it is not
        return self.kind in {
            ToolKind.WRITE, ToolKind.SHELL, ToolKind.NETWORK, ToolKind.MCP, ToolKind.MEMORY
            }
    # displays a tool box that ask for conformation if there is something mutating and does not ask if there sis nothing mutating
    async def get_confirmation(self, invokation: ToolInvokation) -> ToolInvokation | None:
        if not self.is_mutating(invokation.params):
            return None
        #if it is mutating then we will ask for confirmation
        #this returns a ToolConfirmation object which contains the name of the tool, the parameters that are being passed to the tool and the description of the tool. This can be used to display a confirmation dialog to the user before executing the tool.
        return ToolConfirmation(tool_name=self.name, params=invokation.params, description=f"Execute {self.name}")
        #tool_name is the name of the tool that is being executed, params are the parameters that are being passed to the tool and description is the description of the tool that can be used to display in the confirmation dialog.
        #this turns pydantic model to openai schema which is a dict that contains the type of the tool and the properties of the tool. This is used to display the tool in the openai schema format which is required by the openai api to display the tool in the tool box.
        
    def to_openai_schema(self) -> dict[str, Any]:
        schema = self.schema
        if isinstance(schema, type) and issubclass(schema, BaseModel):
            json_schema = schema.model_json_schema( mode='serialization')
            return {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": json_schema.get("properties", {}),
                    "required": list(json_schema.get("required", [])),
                    }
                }
        #if the schema is a dict then we will assume that it is already in the openai schema format and we will just return it as it is
        if isinstance(schema, dict):
            result = {
                "name": self.name,
                "description": self.description,
            }
            
            if 'parameters' in schema:
                result['parameters'] = schema['parameters'] #this is for the case when we have a custom tool that does not use pydantic model and we want to display it in the openai schema format then we can just return the parameters as it is without any modification
            else:
                result['parameters'] = schema
            return result
        #if there is no schema then we will just raise an error
        raise ValueError(f"Invalid schema type for tool. {self.name}: {self.schema}.")
