# Tools/builtin/osint_ip.py
import os
from pydantic import BaseModel, Field
import requests

from nexus_agent.Tools.base import Tool, ToolInvokation, ToolKind, ToolResult


class IPOSINTParams(BaseModel):
    ip: str = Field(
        ...,
        description="IPv4 or IPv6 address to lookup"
    )
    timeout: int = Field(
        10,
        ge=5,
        le=30,
        description="Request timeout in seconds (default: 10)"
    )


class IPOSINTTool(Tool):
    name = "ip_osint"
    description = (
        "Get geolocation, ASN, hostname, and organization data for an IP address using ipinfo.io. "
        "Free tier: 50k requests/month. Set IPINFO_TOKEN for higher limits."
    )
    kind = ToolKind.READ
    schema = IPOSINTParams
    
    IPINFO_URL = "https://ipinfo.io/{ip}/json"
    
    async def execute(self, invocation: ToolInvokation) -> ToolResult:
        params = IPOSINTParams(**invocation.params)
        
        token = os.environ.get("IPINFO_TOKEN", "")
        request_params = {"token": token} if token else {}
        
        try:
            response = requests.get(
                self.IPINFO_URL.format(ip=params.ip),
                params=request_params,
                timeout=params.timeout
            )
            
            if response.status_code == 429:
                return ToolResult.error_result(
                    "ipinfo.io rate limit exceeded. "
                    "Set IPINFO_TOKEN for higher limits: https://ipinfo.io/signup"
                )
            
            if response.status_code != 200:
                return ToolResult.error_result(
                    f"ipinfo.io returned HTTP {response.status_code}."
                )
            
            data = response.json()
            
            # Check for bogon/private IP
            if "bogon" in data:
                return ToolResult.success_result(
                    f"'{params.ip}' is a bogon/private address — no public data available.",
                    metadata={"ip": params.ip, "bogon": True}
                )
            
            # Format results
            fields = ["ip", "hostname", "org", "city", "region", "country", "loc", "timezone"]
            lines = [f"IP intelligence for '{params.ip}':\n"]
            
            metadata_dict = {"ip": params.ip}
            for field in fields:
                value = data.get(field)
                if value:
                    lines.append(f"[+] {field.capitalize()}: {value}")
                    metadata_dict[field] = value
            
            output = "\n".join(lines)
            
            return ToolResult.success_result(
                output=output,
                metadata=metadata_dict
            )
        
        except requests.Timeout:
            return ToolResult.error_result(
                f"Request timed out after {params.timeout} seconds",
                metadata={"ip": params.ip}
            )
        except requests.RequestException as e:
            return ToolResult.error_result(
                f"Network error querying ipinfo.io: {str(e)}",
                metadata={"ip": params.ip}
            )
        except Exception as e:
            return ToolResult.error_result(
                f"Unexpected error: {str(e)}",
                metadata={"ip": params.ip}
            )