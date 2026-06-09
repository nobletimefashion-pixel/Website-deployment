# Tools/builtin/osint_paste.py
from pydantic import BaseModel, Field
import requests

from nexus_agent.Tools.base import Tool, ToolInvokation, ToolKind, ToolResult


class PasteOSINTParams(BaseModel):
    query: str = Field(
        ...,
        description="Email address or username to search for in pastebin dumps"
    )
    timeout: int = Field(
        15,
        ge=5,
        le=60,
        description="Request timeout in seconds (default: 15)"
    )
    max_results: int = Field(
        10,
        ge=1,
        le=50,
        description="Maximum number of results to display (default: 10)"
    )


class PasteOSINTTool(Tool):
    name = "paste_osint"
    description = (
        "Search Pastebin dumps for mentions of an email or username using psbdmp.ws. "
        "Finds public paste entries that may contain leaked credentials or information."
    )
    kind = ToolKind.READ
    schema = PasteOSINTParams
    
    PSBDMP_URL = "https://psbdmp.ws/api/search/{query}"
    
    async def execute(self, invocation: ToolInvokation) -> ToolResult:
        params = PasteOSINTParams(**invocation.params)
        
        try:
            response = requests.get(
                self.PSBDMP_URL.format(query=params.query),
                timeout=params.timeout
            )
            
            if response.status_code == 404:
                return ToolResult.success_result(
                    f"No pastes found mentioning '{params.query}'.",
                    metadata={"query": params.query, "paste_count": 0}
                )
            
            if response.status_code != 200:
                return ToolResult.error_result(
                    f"psbdmp.ws returned HTTP {response.status_code}."
                )
            
            data = response.json()
            pastes = data.get("data", []) if isinstance(data, dict) else []
            
            if not pastes:
                return ToolResult.success_result(
                    f"No pastes found mentioning '{params.query}'.",
                    metadata={"query": params.query, "paste_count": 0}
                )
            
            count = len(pastes)
            shown = pastes[:params.max_results]
            
            lines = [f"Found in {count} paste(s) for '{params.query}':\n"]
            for paste in shown:
                paste_id = paste.get("id", "unknown")
                date = paste.get("time", "unknown date")
                lines.append(f"[+] https://pastebin.com/{paste_id} ({date})")
            
            if count > params.max_results:
                lines.append(f"\n... and {count - params.max_results} more.")
            
            output = "\n".join(lines)
            
            return ToolResult.success_result(
                output=output,
                metadata={
                    "query": params.query,
                    "paste_count": count,
                    "shown": len(shown)
                }
            )
        
        except requests.Timeout:
            return ToolResult.error_result(
                f"Request timed out after {params.timeout} seconds",
                metadata={"query": params.query}
            )
        except requests.RequestException as e:
            return ToolResult.error_result(
                f"Network error querying psbdmp.ws: {str(e)}",
                metadata={"query": params.query}
            )
        except Exception as e:
            return ToolResult.error_result(
                f"Unexpected error: {str(e)}",
                metadata={"query": params.query}
            )