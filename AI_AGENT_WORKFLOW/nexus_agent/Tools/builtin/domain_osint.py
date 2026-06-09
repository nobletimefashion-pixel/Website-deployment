# Tools/builtin/osint_domain.py
from pydantic import BaseModel, Field
import asyncio

from nexus_agent.Tools.base import Tool, ToolInvokation, ToolKind, ToolResult


class DomainOSINTParams(BaseModel):
    domain: str = Field(
        ...,
        description="Target domain to enumerate subdomains for (e.g., example.com)"
    )
    timeout: int = Field(
        120,
        ge=30,
        le=600,
        description="Command timeout in seconds (default: 120)"
    )


class DomainOSINTTool(Tool):
    name = "domain_osint"
    description = (
        "Enumerate subdomains of a target domain using sublist3r. "
        "Discovers all publicly known subdomains. "
        "Requires sublist3r to be installed: pip install sublist3r"
    )
    kind = ToolKind.READ
    schema = DomainOSINTParams
    
    async def execute(self, invocation: ToolInvokation) -> ToolResult:
        params = DomainOSINTParams(**invocation.params)
        
        try:
            process = await asyncio.create_subprocess_exec(
                "sublist3r",
                "-d", params.domain,
                "-n",  # No bruteforce
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout_data, stderr_data = await asyncio.wait_for(
                process.communicate(),
                timeout=params.timeout
            )
            
            stdout = stdout_data.decode('utf-8', errors='replace')
            
            if not stdout:
                return ToolResult.error_result(
                    f"sublist3r produced no output for '{params.domain}'.",
                    metadata={"domain": params.domain}
                )
            
            # Extract subdomains from output
            lines = [
                line.strip()
                for line in stdout.splitlines()
                if line.strip() and params.domain in line and not line.startswith("[")
            ]
            
            if not lines:
                return ToolResult.success_result(
                    f"No subdomains found for '{params.domain}'.",
                    metadata={"domain": params.domain, "subdomain_count": 0}
                )
            
            output = f"Subdomains found for '{params.domain}':\n\n"
            output += "\n".join(f"[+] {subdomain}" for subdomain in lines)
            
            return ToolResult.success_result(
                output=output,
                metadata={
                    "domain": params.domain,
                    "subdomain_count": len(lines),
                    "subdomains": lines
                }
            )
        
        except asyncio.TimeoutError:
            return ToolResult.error_result(
                f"Command timed out after {params.timeout} seconds",
                metadata={"domain": params.domain}
            )
        except FileNotFoundError:
            return ToolResult.error_result(
                "sublist3r is not installed. Install it with: pip install sublist3r",
                metadata={"domain": params.domain}
            )
        except Exception as e:
            return ToolResult.error_result(
                f"Unexpected error: {str(e)}",
                metadata={"domain": params.domain}
            )