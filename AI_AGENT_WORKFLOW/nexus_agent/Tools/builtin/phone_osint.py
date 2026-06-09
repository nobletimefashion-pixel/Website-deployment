# Tools/builtin/osint_phone.py
from pydantic import BaseModel, Field
import asyncio

from nexus_agent.Tools.base import Tool, ToolInvokation, ToolKind, ToolResult


class PhoneOSINTParams(BaseModel):
    phone: str = Field(
        ...,
        description="Phone number in E.164 format (e.g., +14155552671)"
    )
    timeout: int = Field(
        60,
        ge=10,
        le=180,
        description="Command timeout in seconds (default: 60)"
    )


class PhoneOSINTTool(Tool):
    name = "phone_osint"
    description = (
        "Gather carrier, country, and line type intelligence for a phone number using phoneinfoga. "
        "Phone number should be in E.164 format. "
        "Download from: https://github.com/sundowndev/phoneinfoga/releases"
    )
    kind = ToolKind.READ
    schema = PhoneOSINTParams
    
    async def execute(self, invocation: ToolInvokation) -> ToolResult:
        params = PhoneOSINTParams(**invocation.params)
        
        try:
            process = await asyncio.create_subprocess_exec(
                "phoneinfoga",
                "scan",
                "-n", params.phone,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout_data, stderr_data = await asyncio.wait_for(
                process.communicate(),
                timeout=params.timeout
            )
            
            stdout = stdout_data.decode('utf-8', errors='replace').strip()
            stderr = stderr_data.decode('utf-8', errors='replace').strip()
            
            if not stdout:
                return ToolResult.error_result(
                    f"phoneinfoga produced no output for '{params.phone}'. stderr: {stderr}",
                    metadata={"phone": params.phone}
                )
            
            output = f"Phone intelligence for '{params.phone}':\n\n{stdout}"
            
            return ToolResult.success_result(
                output=output,
                metadata={"phone": params.phone}
            )
        
        except asyncio.TimeoutError:
            return ToolResult.error_result(
                f"Command timed out after {params.timeout} seconds",
                metadata={"phone": params.phone}
            )
        except FileNotFoundError:
            return ToolResult.error_result(
                "phoneinfoga is not installed. Download from: https://github.com/sundowndev/phoneinfoga/releases",
                metadata={"phone": params.phone}
            )
        except Exception as e:
            return ToolResult.error_result(
                f"Unexpected error: {str(e)}",
                metadata={"phone": params.phone}
            )