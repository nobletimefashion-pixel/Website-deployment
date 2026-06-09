# Tools/builtin/osint_whois.py
import asyncio
from pydantic import BaseModel, Field

from nexus_agent.Tools.base import Tool, ToolInvokation, ToolKind, ToolResult


class WHOISOSINTParams(BaseModel):
    domain: str = Field(
        ...,
        description="Domain to lookup WHOIS registration data for (e.g., example.com)"
    )
    timeout: int = Field(
        15,
        ge=5,
        le=60,
        description="Query timeout in seconds (default: 15)"
    )


class WHOISOSINTTool(Tool):
    name = "whois_osint"
    description = (
        "Query WHOIS registration data for a domain using python-whois. "
        "Returns registrar, creation/expiration dates, name servers, and contact info. "
        "Requires python-whois: pip install python-whois"
    )
    kind = ToolKind.READ
    schema = WHOISOSINTParams
    
    async def execute(self, invocation: ToolInvokation) -> ToolResult:
        params = WHOISOSINTParams(**invocation.params)
        
        try:
            import whois  # type: ignore
        except ImportError:
            return ToolResult.error_result(
                "python-whois is not installed. Run: pip install python-whois"
            )
        
        try:
            # Run the synchronous whois call in executor
            loop = asyncio.get_event_loop()
            data = await asyncio.wait_for(
                loop.run_in_executor(None, whois.whois, params.domain),
                timeout=float(params.timeout)
            )
            
            # Format results
            fields = {
                "Domain": getattr(data, "domain_name", None),
                "Registrar": getattr(data, "registrar", None),
                "Created": getattr(data, "creation_date", None),
                "Expires": getattr(data, "expiration_date", None),
                "Updated": getattr(data, "updated_date", None),
                "Name Servers": getattr(data, "name_servers", None),
                "Emails": getattr(data, "emails", None),
                "Org": getattr(data, "org", None),
                "Country": getattr(data, "country", None),
            }
            
            lines = [f"WHOIS results for '{params.domain}':\n"]
            metadata_dict = {"domain": params.domain}
            
            for key, val in fields.items():
                if not val:
                    continue
                if isinstance(val, list):
                    val = val[0] if len(val) == 1 else ", ".join(str(v) for v in val[:3])
                lines.append(f"[+] {key}: {val}")
                metadata_dict[key.lower().replace(" ", "_")] = str(val)
            
            output = "\n".join(lines) if len(lines) > 1 else f"No WHOIS data found for '{params.domain}'."
            
            return ToolResult.success_result(
                output=output,
                metadata=metadata_dict
            )
        
        except asyncio.TimeoutError:
            return ToolResult.error_result(
                f"WHOIS lookup timed out after {params.timeout} seconds",
                metadata={"domain": params.domain}
            )
        except Exception as e:
            return ToolResult.error_result(
                f"WHOIS query failed: {str(e)}",
                metadata={"domain": params.domain}
            )