# Tools/builtin/osint_censys.py
import os
import re
from pydantic import BaseModel, Field

from nexus_agent.Tools.base import Tool, ToolInvokation, ToolKind, ToolResult


class CensysOSINTParams(BaseModel):
    target: str = Field(
        ...,
        description="IP address or domain to lookup"
    )
    timeout: int = Field(
        30,
        ge=10,
        le=120,
        description="Query timeout in seconds (default: 30)"
    )


class CensysOSINTTool(Tool):
    name = "censys_osint"
    description = (
        "Query Censys for internet infrastructure data. "
        "IP addresses return open ports, services, ASN, location. "
        "Domains return certificate history, SANs, issuer info. "
        "Requires CENSYS_API_ID and CENSYS_SECRET environment variables."
    )
    kind = ToolKind.READ
    schema = CensysOSINTParams
    
    IP_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
    
    async def execute(self, invocation: ToolInvokation) -> ToolResult:
        params = CensysOSINTParams(**invocation.params)
        
        api_id = os.environ.get("CENSYS_API_ID", "")
        api_secret = os.environ.get("CENSYS_SECRET", "")
        
        if not api_id:
            return ToolResult.error_result(
                "CENSYS_API_ID environment variable is not set. "
                "Get credentials at https://censys.io/account"
            )
        if not api_secret:
            return ToolResult.error_result(
                "CENSYS_SECRET environment variable is not set. "
                "Get credentials at https://censys.io/account"
            )
        
        try:
            from censys.search import CensysHosts  # type: ignore
        except ImportError:
            return ToolResult.error_result(
                "censys library is not installed. Install it with: pip install censys"
            )
        
        target = params.target.strip()
        is_ip = bool(self.IP_RE.match(target))
        
        try:
            if is_ip:
                hosts = CensysHosts(api_id=api_id, api_secret=api_secret)
                data = hosts.view(target)
                output = self._format_ip_result(data, target)
                metadata = {"target": target, "type": "ip"}
            else:
                try:
                    from censys.search import CensysCerts  # type: ignore
                    certs = CensysCerts(api_id=api_id, api_secret=api_secret)
                    query = f"parsed.names: {target}"
                    search_results = list(certs.search(
                        query,
                        fields=[
                            "parsed.names",
                            "parsed.issuer.organization",
                            "parsed.validity.start",
                            "parsed.validity.end",
                        ],
                        max_records=50,
                    ))
                except ImportError:
                    hosts = CensysHosts(api_id=api_id, api_secret=api_secret)
                    search_results = list(
                        hosts.search(
                            f"services.tls.certificates.leaf_data.names: {target}",
                            per_page=10,
                        )
                    )
                output = self._format_domain_result(search_results, target)
                metadata = {"target": target, "type": "domain", "cert_count": len(search_results)}
            
            return ToolResult.success_result(
                output=output,
                metadata=metadata
            )
        
        except Exception as e:
            exc_str = str(e).lower()
            if "rate" in exc_str or "429" in exc_str:
                return ToolResult.error_result(
                    "Censys rate limit reached. Try again later.",
                    metadata={"target": target}
                )
            if "not found" in exc_str or "404" in exc_str:
                return ToolResult.error_result(
                    "No Censys data found for target.",
                    metadata={"target": target}
                )
            if "401" in exc_str or "unauthorized" in exc_str or "forbidden" in exc_str:
                return ToolResult.error_result(
                    "Censys authentication failed. Check your CENSYS_API_ID and CENSYS_SECRET.",
                    metadata={"target": target}
                )
            return ToolResult.error_result(
                f"Unexpected error: {str(e)}",
                metadata={"target": target}
            )
    
    def _format_ip_result(self, data: dict, ip: str) -> str:
        lines = ["[Censys] Type: ip", f"[Censys] IP: {ip}"]
        
        services = data.get("services", [])
        ports = sorted({str(s.get("port")) for s in services if s.get("port")})
        if ports:
            lines.append(f"[Censys] Open Ports: {', '.join(ports[:20])}")
        
        svc_names = []
        seen = set()
        for s in services:
            name = s.get("service_name", "")
            if name and name not in seen:
                seen.add(name)
                svc_names.append(name)
        if svc_names:
            lines.append(f"[Censys] Services: {', '.join(svc_names[:20])}")
        
        asn_data = data.get("autonomous_system", {})
        asn = asn_data.get("asn", "")
        asn_name = asn_data.get("name", "")
        if asn:
            lines.append(f"[Censys] ASN: AS{asn} {asn_name}".rstrip())
        
        location = data.get("location", {})
        country = location.get("country", "")
        if country:
            lines.append(f"[Censys] Country: {country}")
        
        last_updated = data.get("last_updated_at", "")
        if last_updated:
            lines.append(f"[Censys] Last Updated: {last_updated[:10]}")
        
        return "\n".join(lines)
    
    def _format_domain_result(self, results: list, domain: str) -> str:
        lines = [
            "[Censys] Type: domain",
            f"[Censys] Domain: {domain}",
            f"[Censys] Certificates Found: {len(results)}",
        ]
        
        if not results:
            return "\n".join(lines)
        
        first = results[0]
        parsed = first.get("parsed", {})
        
        issuer_orgs = parsed.get("issuer", {}).get("organization", [])
        if isinstance(issuer_orgs, list) and issuer_orgs:
            lines.append(f"[Censys] Issuer: {issuer_orgs[0]}")
        elif isinstance(issuer_orgs, str) and issuer_orgs:
            lines.append(f"[Censys] Issuer: {issuer_orgs}")
        
        names = parsed.get("names", [])
        if names:
            lines.append(f"[Censys] SANs: {', '.join(names[:10])}")
        
        starts = []
        ends = []
        for r in results:
            validity = r.get("parsed", {}).get("validity", {})
            if validity.get("start"):
                starts.append(validity["start"])
            if validity.get("end"):
                ends.append(validity["end"])
        
        if starts:
            lines.append(f"[Censys] First Seen: {min(starts)[:10]}")
        if ends:
            lines.append(f"[Censys] Last Seen: {max(ends)[:10]}")
        
        return "\n".join(lines)