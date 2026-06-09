# Tools/builtin/osint_username.py
from pydantic import BaseModel, Field
import asyncio

from nexus_agent.Tools.base import Tool, ToolInvokation, ToolKind, ToolResult


class UsernameOSINTParams(BaseModel):
    username: str = Field(
        ...,
        description="Username to search across social networks"
    )
    timeout: int = Field(
        180,
        ge=30,
        le=600,
        description="Command timeout in seconds (default: 180)"
    )
    per_site_timeout: int = Field(
        3,
        ge=1,
        le=10,
        description="Timeout per site in seconds (default: 3)"
    )


class UsernameOSINTTool(Tool):
    name = "username_osint"
    description = (
        "Search for a username across 300+ social networks and platforms using sherlock. "
        "Discovers where a username is registered across the internet. "
        "Requires sherlock to be installed: pip install sherlock-project"
    )
    kind = ToolKind.READ
    schema = UsernameOSINTParams
    
    async def execute(self, invocation: ToolInvokation) -> ToolResult:
        params = UsernameOSINTParams(**invocation.params)
        
        try:
            process = await asyncio.create_subprocess_exec(
                "sherlock",
                params.username,
                "--print-found",
                "--timeout",
                str(params.per_site_timeout),
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
                    f"sherlock produced no output. stderr: {stderr}",
                    metadata={"username": params.username}
                )
            
            if not stdout or "No accounts found" in stdout:
                return ToolResult.success_result(
                    f"No accounts found for username '{params.username}'.",
                    metadata={"username": params.username, "accounts_found": 0}
                )
            
            output = f"OSINT results for username '{params.username}':\n\n{stdout}"
            
            # Count found accounts
            account_count = stdout.count("[+]")
            
            return ToolResult.success_result(
                output=output,
                metadata={
                    "username": params.username,
                    "accounts_found": account_count
                }
            )
        
        except asyncio.TimeoutError:
            return ToolResult.error_result(
                f"Command timed out after {params.timeout} seconds",
                metadata={"username": params.username}
            )
        except FileNotFoundError:
            return ToolResult.error_result(
                "sherlock is not installed. Install it with: pip install sherlock-project",
                metadata={"username": params.username}
            )
        except Exception as e:
            return ToolResult.error_result(
                f"Unexpected error: {str(e)}",
                metadata={"username": params.username}
            )