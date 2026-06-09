# Tools/builtin/osint_email.py
from pydantic import BaseModel, Field
import asyncio

from nexus_agent.Tools.base import Tool, ToolInvokation, ToolKind, ToolResult


class EmailOSINTParams(BaseModel):
    email: str = Field(
        ...,
        description="Email address to check across online services"
    )
    timeout: int = Field(
        120,
        ge=10,
        le=300,
        description="Command timeout in seconds (default: 120)"
    )


class EmailOSINTTool(Tool):
    name = "email_osint"
    description = (
        "Enumerate online services registered with a target email using holehe. "
        "Discovers which platforms (social media, shopping, etc.) an email is registered on. "
        "Requires holehe to be installed: pip install holehe"
    )
    kind = ToolKind.READ
    schema = EmailOSINTParams
    
    async def execute(self, invocation: ToolInvokation) -> ToolResult:
        params = EmailOSINTParams(**invocation.params)
        
        try:
            process = await asyncio.create_subprocess_exec(
                "holehe",
                params.email,
                "--only-used",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout_data, stderr_data = await asyncio.wait_for(
                process.communicate(),
                timeout=params.timeout
            )
            
            stdout = stdout_data.decode('utf-8', errors='replace').strip()
            stderr = stderr_data.decode('utf-8', errors='replace').strip()
            exit_code = process.returncode
            
            if exit_code != 0:
                return ToolResult.error_result(
                    f"holehe exited with code {exit_code}: {stderr}",
                    metadata={"email": params.email, "exit_code": exit_code}
                )
            
            if not stdout:
                return ToolResult.success_result(
                    f"No registered services found for {params.email}.",
                    metadata={"email": params.email, "services_found": 0}
                )
            
            output = f"OSINT results for '{params.email}':\n\n{stdout}"
            
            return ToolResult.success_result(
                output=output,
                metadata={"email": params.email}
            )
        
        except asyncio.TimeoutError:
            return ToolResult.error_result(
                f"Command timed out after {params.timeout} seconds",
                metadata={"email": params.email}
            )
        except FileNotFoundError:
            return ToolResult.error_result(
                "holehe is not installed. Install it with: pip install holehe",
                metadata={"email": params.email}
            )
        except Exception as e:
            return ToolResult.error_result(
                f"Unexpected error: {str(e)}",
                metadata={"email": params.email}
            )