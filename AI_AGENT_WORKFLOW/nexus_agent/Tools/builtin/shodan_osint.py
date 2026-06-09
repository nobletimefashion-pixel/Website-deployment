# Tools/builtin/osint_shodan.py
import os
import re
from pydantic import BaseModel, Field

from nexus_agent.Tools.base import Tool, ToolInvokation, ToolKind, ToolResult


class ShodanOSINTParams(BaseModel):
    query: str = Field(
        ...,
        description="IP address for host lookup or keyword for general search"
    )
    timeout: int = Field(
        30,
        ge=10,
        le=120,
        description="Query timeout in seconds (default: 30)"
    )


class ShodanOSINTTool(Tool):
    name = "shodan_osint"
    description = (
        "Query Shodan for internet-facing host intelligence or keyword search. "
        "IP addresses return host details (ports, services, vulns). "
        "Keywords return search results across Shodan's database. "
        "Requires SHODAN_API_KEY environment variable."
    )
    kind = ToolKind.READ
    schema = ShodanOSINTParams
    
    IP_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
    
    async def execute(self, invocation: ToolInvokation) -> ToolResult:
        params = ShodanOSINTParams(**invocation.params)
        
        api_key = os.environ.get("SHODAN_API_KEY", "")
        if not api_key:
            return ToolResult.error_result(
                "SHODAN_API_KEY environment variable is not set. "
                "Get a free key at https://account.shodan.io"
            )
        
        try:
            import shodan  # type: ignore
        except ImportError:
            return ToolResult.error_result(
                "shodan library is not installed. Install it with: pip install shodan"
            )
        
        try:
            api = shodan.Shodan(api_key)
            
            # Determine if query is an IP or keyword
            is_ip = bool(self.IP_RE.match(params.query.strip()))
            
            if is_ip:
                # Host lookup
                data = api.host(params.query)
                output = self._format_host(data, params.query)
                metadata = {
                    "query": params.query,
                    "type": "host",
                    "ip": data.get("ip_str"),
                    "open_ports": [s.get("port") for s in data.get("data", [])]
                }
            else:
                # Keyword search
                results = api.search(params.query, limit=10)
                output = self._format_search(results, params.query)
                metadata = {
                    "query": params.query,
                    "type": "search",
                    "total_results": results.get("total", 0),
                    "shown": len(results.get("matches", []))
                }
            
            return ToolResult.success_result(
                output=output,
                metadata=metadata
            )
        
        except shodan.APIError as e:  # type: ignore
            return ToolResult.error_result(
                f"Shodan API error: {str(e)}",
                metadata={"query": params.query}
            )
        except Exception as e:
            return ToolResult.error_result(
                f"Unexpected error: {str(e)}",
                metadata={"query": params.query}
            )
    
    def _format_host(self, data: dict, ip: str) -> str:
        lines = [f"Shodan host intelligence for '{ip}':\n"]
        
        if data.get("ip_str"):
            lines.append(f"[+] IP: {data['ip_str']}")
        if data.get("org"):
            lines.append(f"[+] Org: {data['org']}")
        if data.get("country_name"):
            lines.append(f"[+] Country: {data['country_name']}")
        if data.get("city"):
            lines.append(f"[+] City: {data['city']}")
        if data.get("os"):
            lines.append(f"[+] OS: {data['os']}")
        if data.get("hostnames"):
            lines.append(f"[+] Hostnames: {', '.join(data['hostnames'][:5])}")
        
        ports = [str(s.get("port")) for s in data.get("data", []) if s.get("port")]
        if ports:
            lines.append(f"[+] Open ports: {', '.join(ports[:20])}")
        
        vulns = list(data.get("vulns", {}).keys())
        if vulns:
            lines.append(f"[+] Vulnerabilities: {', '.join(vulns[:10])}")
        
        if len(lines) == 1:
            lines.append(f"[+] No additional details available for {ip}.")
        
        return "\n".join(lines)
    
    def _format_search(self, results: dict, query: str) -> str:
        total = results.get("total", 0)
        matches = results.get("matches", [])
        
        if not matches:
            return f"No Shodan results found for '{query}'."
        
        lines = [
            f"Shodan search results for '{query}' ({total} total, showing {len(matches)}):\n"
        ]
        
        for m in matches:
            ip = m.get("ip_str", "unknown")
            port = m.get("port", "?")
            org = m.get("org", "unknown")
            country = m.get("location", {}).get("country_name", "unknown")
            lines.append(f"[+] {ip}:{port} — {org} — {country}")
        
        return "\n".join(lines)