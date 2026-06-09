# Tools/builtin/web_security_audit.py
"""
OWASP Top 10 Web Security Audit Tool
Tests for the OWASP Top 10 vulnerabilities and generates a detailed report.
"""

import asyncio
import json
import re
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from nexus_agent.Tools.base import Tool, ToolInvokation, ToolKind, ToolResult


# ─────────────────────────────────────────────
#  SEVERITY / FINDING MODELS
# ─────────────────────────────────────────────

class Finding:
    def __init__(
        self,
        owasp_id: str,
        title: str,
        severity: str,   # CRITICAL / HIGH / MEDIUM / LOW / INFO
        description: str,
        evidence: str,
        remediation: str,
        url: str = "",
        cwe: str = "",
    ):
        self.owasp_id    = owasp_id
        self.title       = title
        self.severity    = severity
        self.description = description
        self.evidence    = evidence
        self.remediation = remediation
        self.url         = url
        self.cwe         = cwe
        self.timestamp   = datetime.utcnow().isoformat() + "Z"


# ─────────────────────────────────────────────
#  TOOL PARAMS
# ─────────────────────────────────────────────

class WebSecurityAuditParams(BaseModel):
    target_url: str = Field(
        ...,
        description="Target website URL to audit (e.g. https://example.com)"
    )
    output_path: str = Field(
        "security_audit_report.md",
        description="Path to save the Markdown report (default: security_audit_report.md)"
    )
    checks: list[str] | None = Field(
        None,
        description=(
            "List of OWASP checks to run. Leave None to run all. "
            "Options: broken_access, crypto_failures, injection, insecure_design, "
            "security_misconfig, vulnerable_components, auth_failures, "
            "integrity_failures, logging_failures, ssrf"
        )
    )
    timeout: int = Field(
        15,
        ge=5,
        le=60,
        description="HTTP request timeout in seconds (default: 15)"
    )
    user_agent: str = Field(
        "NexusSecurityAudit/1.0 (OWASP-Testing; Educational)",
        description="User-Agent header to use for requests"
    )
    save_json: bool = Field(
        False,
        description="Also save a machine-readable JSON report alongside the Markdown"
    )
    follow_redirects: bool = Field(
        True,
        description="Follow HTTP redirects (default: True)"
    )
    crawl_depth: int = Field(
        1,
        ge=0,
        le=3,
        description="How many levels of internal links to crawl for additional context (0 = homepage only)"
    )


# ─────────────────────────────────────────────
#  MAIN TOOL
# ─────────────────────────────────────────────

class WebSecurityAuditTool(Tool):
    name = "web_security_audit"
    description = (
        "Performs automated OWASP Top 10 security testing on a target website and "
        "generates a professional Markdown (and optionally JSON) report. "
        "Tests for: Broken Access Control, Cryptographic Failures, Injection, "
        "Insecure Design, Security Misconfiguration, Vulnerable Components, "
        "Identification & Authentication Failures, Software Integrity Failures, "
        "Logging & Monitoring Failures, and SSRF. "
        "Safe/non-destructive — read-only probing only."
    )
    kind = ToolKind.NETWORK
    schema = WebSecurityAuditParams

    # Common paths to probe
    _SENSITIVE_PATHS = [
        "/.env", "/.git/config", "/.git/HEAD", "/config.php", "/wp-config.php",
        "/config.yml", "/config.yaml", "/config.json", "/settings.py",
        "/admin", "/admin/", "/administrator", "/phpmyadmin", "/phpinfo.php",
        "/server-status", "/server-info", "/.htaccess", "/web.config",
        "/backup.zip", "/backup.sql", "/db.sql", "/dump.sql",
        "/api/v1/users", "/api/users", "/api/admin", "/swagger.json",
        "/openapi.json", "/api-docs", "/actuator", "/actuator/env",
        "/actuator/health", "/actuator/info", "/debug", "/console",
        "/.DS_Store", "/robots.txt", "/sitemap.xml", "/crossdomain.xml",
        "/security.txt", "/.well-known/security.txt",
    ]

    # Security headers that should be present
    _REQUIRED_HEADERS = {
        "Strict-Transport-Security":   "Protects against protocol downgrade / MITM",
        "Content-Security-Policy":     "Mitigates XSS and data injection attacks",
        "X-Content-Type-Options":      "Prevents MIME-type sniffing",
        "X-Frame-Options":             "Prevents clickjacking",
        "Referrer-Policy":             "Controls referrer information leakage",
        "Permissions-Policy":          "Restricts browser feature access",
    }

    # Headers that should NOT be present (information disclosure)
    _LEAKY_HEADERS = [
        "Server", "X-Powered-By", "X-AspNet-Version", "X-AspNetMvc-Version",
        "X-Generator", "X-Drupal-Cache", "X-Varnish",
    ]

    # Basic injection payloads (safe, non-destructive — just observe reflections)
    _XSS_PROBES = [
        "<script>alert(1)</script>",
        '"><img src=x onerror=alert(1)>',
        "javascript:alert(1)",
        "<svg/onload=alert(1)>",
    ]
    _SQLI_PROBES = [
        "'", '"', "1' OR '1'='1", "1 AND 1=1", "' OR SLEEP(0)--",
        "1; SELECT 1", "' UNION SELECT NULL--",
    ]
    _SSRF_PROBES = [
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://127.0.0.1/",
        "http://localhost/",
        "http://0.0.0.0/",
        "file:///etc/passwd",
    ]

    # Common default / weak credentials (just enumerated, not bruteforced here)
    _WEAK_CREDS_NOTE = ["admin:admin", "admin:password", "admin:123456", "root:root", "test:test"]

    def __init__(self, config):
        super().__init__(config)
        self._client: httpx.AsyncClient | None = None

    # ──────────────────────────────────────────
    #  ENTRY POINT
    # ──────────────────────────────────────────

    async def execute(self, invocation: ToolInvokation) -> ToolResult:
        params = WebSecurityAuditParams(**invocation.params)

        # Normalise URL
        target = params.target_url.rstrip("/")
        if not target.startswith(("http://", "https://")):
            target = "https://" + target

        print(f"\n🔍  Web Security Audit — {target}")
        print(f"⏱   Started at {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n")

        findings: list[Finding] = []
        crawled_urls: list[str] = [target]

        headers = {
            "User-Agent": params.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(params.timeout),
            follow_redirects=params.follow_redirects,
            verify=False,            # Allow self-signed certs; we report on them
            headers=headers,
        ) as client:
            self._client = client

            # ── Crawl phase ──────────────────────────────
            if params.crawl_depth > 0:
                extra = await self._crawl(target, params.crawl_depth)
                crawled_urls.extend(extra)
                crawled_urls = list(dict.fromkeys(crawled_urls))  # deduplicate, preserve order
                print(f"🕷   Crawled {len(crawled_urls)} URL(s)\n")

            # ── Fetch homepage once (needed by many checks) ──
            try:
                home_resp = await client.get(target)
            except Exception as exc:
                return ToolResult.error_result(
                    f"Cannot reach target '{target}': {exc}"
                )

            enabled = set(params.checks) if params.checks else None  # None = all

            # ── Run checks ─────────────────────────────────
            check_map = {
                "broken_access":         self._check_broken_access,
                "crypto_failures":       self._check_crypto_failures,
                "injection":             self._check_injection,
                "insecure_design":       self._check_insecure_design,
                "security_misconfig":    self._check_security_misconfig,
                "vulnerable_components": self._check_vulnerable_components,
                "auth_failures":         self._check_auth_failures,
                "integrity_failures":    self._check_integrity_failures,
                "logging_failures":      self._check_logging_failures,
                "ssrf":                  self._check_ssrf,
            }

            for key, fn in check_map.items():
                if enabled and key not in enabled:
                    continue
                label = key.replace("_", " ").title()
                print(f"  ▶  {label} …")
                try:
                    results = await fn(target, home_resp, crawled_urls)
                    findings.extend(results)
                except Exception as exc:
                    print(f"     ⚠  Check failed: {exc}")

        # ── Generate report ────────────────────────────────
        report_md   = self._build_markdown_report(target, findings, crawled_urls)
        output_path = Path(invocation.cwd) / params.output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report_md, encoding="utf-8")

        if params.save_json:
            json_path = output_path.with_suffix(".json")
            json_path.write_text(
                json.dumps(self._findings_to_dict(target, findings), indent=2),
                encoding="utf-8"
            )
            print(f"\n📄  JSON report saved → {json_path}")

        # Summary counts
        sev_order  = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]
        sev_counts = {s: sum(1 for f in findings if f.severity == s) for s in sev_order}

        summary = (
            f"✅  Audit complete — {len(findings)} finding(s)\n"
            + "  ".join(f"{k}: {v}" for k, v in sev_counts.items() if v)
            + f"\n📄  Report → {output_path}"
        )
        print(f"\n{summary}\n")

        return ToolResult.success_result(
            output=summary,
            metadata={
                "target":       target,
                "findings":     len(findings),
                "report_path":  str(output_path),
                "severity":     sev_counts,
                "crawled_urls": len(crawled_urls),
            }
        )

    # ══════════════════════════════════════════
    #  CRAWL HELPER
    # ══════════════════════════════════════════

    async def _crawl(self, base_url: str, depth: int) -> list[str]:
        """Very lightweight internal link crawler."""
        visited: set[str] = {base_url}
        frontier: list[str] = [base_url]

        for _ in range(depth):
            next_frontier: list[str] = []
            for url in frontier:
                try:
                    resp = await self._client.get(url)
                    links = re.findall(r'href=["\']([^"\']+)["\']', resp.text)
                    for link in links:
                        abs_link = urllib.parse.urljoin(url, link).split("?")[0].split("#")[0]
                        if abs_link.startswith(base_url) and abs_link not in visited:
                            visited.add(abs_link)
                            next_frontier.append(abs_link)
                except Exception:
                    pass
            frontier = next_frontier[:20]  # cap breadth

        return list(visited - {base_url})

    # ══════════════════════════════════════════
    #  A01 – BROKEN ACCESS CONTROL
    # ══════════════════════════════════════════

    async def _check_broken_access(
        self, target: str, home_resp: httpx.Response, crawled: list[str]
    ) -> list[Finding]:
        findings: list[Finding] = []

        # 1. Probe sensitive / admin paths
        exposed: list[str] = []
        for path in self._SENSITIVE_PATHS:
            url = target + path
            try:
                r = await self._client.get(url)
                if r.status_code in (200, 301, 302, 403):
                    status_note = (
                        "🔴 EXPOSED (200)" if r.status_code == 200
                        else f"🟡 Redirects/Forbidden ({r.status_code})"
                    )
                    exposed.append(f"[{r.status_code}] {url} — {status_note}")
            except Exception:
                pass

        if exposed:
            findings.append(Finding(
                owasp_id    = "A01",
                title       = "Broken Access Control — Sensitive Paths Accessible",
                severity    = "HIGH",
                description = (
                    "One or more sensitive paths returned non-404 responses, "
                    "indicating that access controls may be missing or misconfigured."
                ),
                evidence    = "\n".join(exposed[:20]),
                remediation = (
                    "Ensure all sensitive paths require authentication/authorisation. "
                    "Return 404 (not 403) for truly non-existent resources to avoid enumeration. "
                    "Apply deny-by-default policies."
                ),
                cwe = "CWE-284",
            ))

        # 2. IDOR hint — check if numeric IDs appear in URLs
        idor_urls = [u for u in crawled if re.search(r'/\d+', u)]
        if idor_urls:
            findings.append(Finding(
                owasp_id    = "A01",
                title       = "Broken Access Control — Potential IDOR Pattern",
                severity    = "MEDIUM",
                description = (
                    "URLs with numeric IDs were found. If object-level access "
                    "controls are absent, an attacker may access other users' data "
                    "by manipulating the ID."
                ),
                evidence    = "\n".join(idor_urls[:10]),
                remediation = (
                    "Implement per-object authorisation checks server-side. "
                    "Use UUIDs or opaque tokens instead of sequential numeric IDs."
                ),
                cwe = "CWE-639",
            ))

        # 3. Missing CORS restriction
        cors = home_resp.headers.get("Access-Control-Allow-Origin", "")
        if cors == "*":
            findings.append(Finding(
                owasp_id    = "A01",
                title       = "Broken Access Control — Overly Permissive CORS",
                severity    = "HIGH",
                description = "The response includes `Access-Control-Allow-Origin: *`, "
                              "allowing any origin to make cross-origin requests with credentials.",
                evidence    = f"Access-Control-Allow-Origin: {cors}",
                remediation = "Restrict CORS to explicit, trusted origins. "
                              "Never combine `*` with `Access-Control-Allow-Credentials: true`.",
                cwe         = "CWE-942",
            ))

        return findings

    # ══════════════════════════════════════════
    #  A02 – CRYPTOGRAPHIC FAILURES
    # ══════════════════════════════════════════

    async def _check_crypto_failures(
        self, target: str, home_resp: httpx.Response, crawled: list[str]
    ) -> list[Finding]:
        findings: list[Finding] = []

        # 1. HTTP (no TLS)
        if target.startswith("http://"):
            findings.append(Finding(
                owasp_id    = "A02",
                title       = "Cryptographic Failures — No TLS/HTTPS",
                severity    = "CRITICAL",
                description = "The site serves content over plain HTTP. All traffic "
                              "is transmitted in cleartext and is vulnerable to interception.",
                evidence    = f"Target URL: {target}",
                remediation = "Deploy TLS (HTTPS). Obtain a certificate from Let's Encrypt "
                              "or a commercial CA. Redirect all HTTP to HTTPS.",
                cwe         = "CWE-311",
            ))

        # 2. HSTS missing / weak
        hsts = home_resp.headers.get("Strict-Transport-Security", "")
        if not hsts:
            findings.append(Finding(
                owasp_id    = "A02",
                title       = "Cryptographic Failures — HSTS Header Missing",
                severity    = "MEDIUM",
                description = "The Strict-Transport-Security header is absent. "
                              "Browsers will not be told to always use HTTPS for this domain.",
                evidence    = "Header not present in response",
                remediation = "Add: Strict-Transport-Security: max-age=31536000; includeSubDomains; preload",
                cwe         = "CWE-319",
            ))
        elif "max-age" in hsts:
            max_age_match = re.search(r"max-age=(\d+)", hsts)
            if max_age_match and int(max_age_match.group(1)) < 31536000:
                findings.append(Finding(
                    owasp_id    = "A02",
                    title       = "Cryptographic Failures — Weak HSTS max-age",
                    severity    = "LOW",
                    description = f"HSTS max-age is set to {max_age_match.group(1)}s (< 1 year). "
                                  "Short max-age reduces effectiveness.",
                    evidence    = f"Strict-Transport-Security: {hsts}",
                    remediation = "Set max-age to at least 31536000 (1 year).",
                    cwe         = "CWE-319",
                ))

        # 3. Cookie flags
        for cookie in home_resp.headers.get_list("Set-Cookie"):
            cookie_lower = cookie.lower()
            name = cookie.split("=")[0].strip()
            issues = []
            if "secure" not in cookie_lower:
                issues.append("missing Secure flag")
            if "httponly" not in cookie_lower:
                issues.append("missing HttpOnly flag")
            if "samesite" not in cookie_lower:
                issues.append("missing SameSite attribute")
            if issues:
                findings.append(Finding(
                    owasp_id    = "A02",
                    title       = f"Cryptographic Failures — Insecure Cookie: {name}",
                    severity    = "MEDIUM",
                    description = f"Cookie `{name}` has security issues: {', '.join(issues)}.",
                    evidence    = cookie[:200],
                    remediation = "Set Secure, HttpOnly, and SameSite=Strict (or Lax) on all cookies.",
                    cwe         = "CWE-614",
                ))

        # 4. Mixed content hint
        if "http://" in home_resp.text and target.startswith("https://"):
            findings.append(Finding(
                owasp_id    = "A02",
                title       = "Cryptographic Failures — Potential Mixed Content",
                severity    = "LOW",
                description = "The HTTPS page appears to reference HTTP resources. "
                              "This may trigger browser mixed-content warnings.",
                evidence    = "http:// references found in HTTPS page body",
                remediation = "Ensure all sub-resources (scripts, images, stylesheets) "
                              "are loaded over HTTPS.",
                cwe         = "CWE-311",
            ))

        return findings

    # ══════════════════════════════════════════
    #  A03 – INJECTION
    # ══════════════════════════════════════════

    async def _check_injection(
        self, target: str, home_resp: httpx.Response, crawled: list[str]
    ) -> list[Finding]:
        findings: list[Finding] = []

        # Collect URLs with query params for probing
        param_urls: list[str] = []
        for url in [target] + crawled:
            if "?" in url and "=" in url:
                param_urls.append(url)

        # Extract form action URLs from homepage
        form_actions = re.findall(r'<form[^>]+action=["\']([^"\']+)["\']', home_resp.text, re.I)

        # ── XSS ─────────────────────────────────────
        xss_hits: list[str] = []
        for url in param_urls[:5]:
            parsed = urllib.parse.urlparse(url)
            qs = urllib.parse.parse_qs(parsed.query)
            for param, values in qs.items():
                for probe in self._XSS_PROBES[:2]:  # limit probes
                    new_qs = {**qs, param: [probe]}
                    test_url = parsed._replace(
                        query=urllib.parse.urlencode(new_qs, doseq=True)
                    ).geturl()
                    try:
                        r = await self._client.get(test_url)
                        if probe in r.text:
                            xss_hits.append(
                                f"Reflected in param `{param}` → {test_url}"
                            )
                    except Exception:
                        pass

        if xss_hits:
            findings.append(Finding(
                owasp_id    = "A03",
                title       = "Injection — Reflected XSS Detected",
                severity    = "HIGH",
                description = "User-supplied input is reflected in the response without encoding. "
                              "An attacker can inject malicious scripts.",
                evidence    = "\n".join(xss_hits[:5]),
                remediation = "HTML-encode all user input before reflecting in responses. "
                              "Implement a strict Content-Security-Policy. "
                              "Use framework-level auto-escaping.",
                cwe         = "CWE-79",
            ))
        elif param_urls:
            findings.append(Finding(
                owasp_id    = "A03",
                title       = "Injection — XSS Probes Not Reflected (Low Signal)",
                severity    = "INFO",
                description = f"XSS probes were tested on {len(param_urls)} URL parameter(s) "
                              "and were not reflected. Manual testing still recommended.",
                evidence    = f"Tested URLs: {', '.join(param_urls[:3])}",
                remediation = "Continue using output encoding and CSP as defence in depth.",
                cwe         = "CWE-79",
            ))

        # ── SQLi ────────────────────────────────────
        sqli_hits: list[str] = []
        sqli_errors = [
            "sql syntax", "mysql_fetch", "ora-", "sqlite_", "pg_query",
            "sqlstate", "unclosed quotation", "syntax error", "database error",
        ]
        for url in param_urls[:5]:
            parsed = urllib.parse.urlparse(url)
            qs = urllib.parse.parse_qs(parsed.query)
            for param, values in qs.items():
                for probe in self._SQLI_PROBES[:3]:
                    new_qs = {**qs, param: [probe]}
                    test_url = parsed._replace(
                        query=urllib.parse.urlencode(new_qs, doseq=True)
                    ).geturl()
                    try:
                        r = await self._client.get(test_url)
                        body_lower = r.text.lower()
                        for err in sqli_errors:
                            if err in body_lower:
                                sqli_hits.append(
                                    f"Error `{err}` in param `{param}` → {test_url}"
                                )
                    except Exception:
                        pass

        if sqli_hits:
            findings.append(Finding(
                owasp_id    = "A03",
                title       = "Injection — SQL Error Disclosure (Possible SQLi)",
                severity    = "CRITICAL",
                description = "Database error messages were returned when injecting SQL "
                              "metacharacters. This strongly suggests SQL injection vulnerability.",
                evidence    = "\n".join(sqli_hits[:5]),
                remediation = "Use parameterised queries / prepared statements exclusively. "
                              "Disable detailed database error messages in production. "
                              "Apply input validation and an ORM.",
                cwe         = "CWE-89",
            ))

        # ── Command injection hint via path traversal ──
        traversal_probes = ["../etc/passwd", "..%2Fetc%2Fpasswd", "....//etc/passwd"]
        for path in traversal_probes:
            try:
                r = await self._client.get(f"{target}/{path}")
                if "root:" in r.text:
                    findings.append(Finding(
                        owasp_id    = "A03",
                        title       = "Injection — Path Traversal: /etc/passwd Exposed",
                        severity    = "CRITICAL",
                        description = "The server returned /etc/passwd content via path traversal.",
                        evidence    = r.text[:300],
                        remediation = "Validate and canonicalize all file paths server-side. "
                                      "Restrict file access using a chroot or container.",
                        cwe         = "CWE-22",
                    ))
                    break
            except Exception:
                pass

        return findings

    # ══════════════════════════════════════════
    #  A04 – INSECURE DESIGN
    # ══════════════════════════════════════════

    async def _check_insecure_design(
        self, target: str, home_resp: httpx.Response, crawled: list[str]
    ) -> list[Finding]:
        findings: list[Finding] = []

        # 1. Verbose error disclosure
        error_probes = [
            f"{target}/this_page_does_not_exist_xyz123",
            f"{target}/error?id=1/0",
        ]
        for url in error_probes:
            try:
                r = await self._client.get(url)
                error_keywords = [
                    "stack trace", "traceback", "exception in thread",
                    "line number", "sqlexception", "at java.", "at org.", "at com.",
                    "debug:", "undefined method", "nomethod error",
                ]
                body_lower = r.text.lower()
                hit = next((kw for kw in error_keywords if kw in body_lower), None)
                if hit:
                    findings.append(Finding(
                        owasp_id    = "A04",
                        title       = "Insecure Design — Verbose Error / Stack Trace Disclosure",
                        severity    = "MEDIUM",
                        description = f"The application reveals internal error details ('{hit}') "
                                      "that can aid attackers in understanding the technology stack.",
                        evidence    = f"Keyword '{hit}' found in response to: {url}\n"
                                      + r.text[:400],
                        remediation = "Display generic error pages in production. "
                                      "Log details server-side only. Disable debug modes.",
                        cwe         = "CWE-209",
                    ))
                    break
            except Exception:
                pass

        # 2. Directory listing
        dirs_to_check = ["/images/", "/uploads/", "/static/", "/files/", "/assets/", "/backup/"]
        for d in dirs_to_check:
            try:
                r = await self._client.get(target + d)
                if r.status_code == 200 and (
                    "index of" in r.text.lower() or "directory listing" in r.text.lower()
                    or "<a href=" in r.text.lower() and "parent directory" in r.text.lower()
                ):
                    findings.append(Finding(
                        owasp_id    = "A04",
                        title       = f"Insecure Design — Directory Listing Enabled: {d}",
                        severity    = "MEDIUM",
                        description = f"The directory `{d}` has directory listing enabled, "
                                      "exposing file names and structure.",
                        evidence    = f"HTTP 200 with index page at {target + d}",
                        remediation = "Disable directory listing in your web server configuration "
                                      "(e.g. `Options -Indexes` for Apache).",
                        cwe         = "CWE-548",
                    ))
            except Exception:
                pass

        return findings

    # ══════════════════════════════════════════
    #  A05 – SECURITY MISCONFIGURATION
    # ══════════════════════════════════════════

    async def _check_security_misconfig(
        self, target: str, home_resp: httpx.Response, crawled: list[str]
    ) -> list[Finding]:
        findings: list[Finding] = []

        # 1. Missing security headers
        missing: list[str] = []
        for header, desc in self._REQUIRED_HEADERS.items():
            if header.lower() not in [k.lower() for k in home_resp.headers.keys()]:
                missing.append(f"• **{header}** — {desc}")

        if missing:
            findings.append(Finding(
                owasp_id    = "A05",
                title       = "Security Misconfiguration — Missing Security Headers",
                severity    = "MEDIUM",
                description = f"{len(missing)} recommended security header(s) are absent.",
                evidence    = "\n".join(missing),
                remediation = "Add the missing headers to all HTTP responses via your web server or application middleware.",
                cwe         = "CWE-16",
            ))

        # 2. Information-disclosing headers
        leaky: list[str] = []
        for h in self._LEAKY_HEADERS:
            val = home_resp.headers.get(h)
            if val:
                leaky.append(f"{h}: {val}")

        if leaky:
            findings.append(Finding(
                owasp_id    = "A05",
                title       = "Security Misconfiguration — Technology Disclosure via Headers",
                severity    = "LOW",
                description = "Response headers reveal technology stack details that help attackers.",
                evidence    = "\n".join(leaky),
                remediation = "Remove or obfuscate Server, X-Powered-By and similar headers.",
                cwe         = "CWE-200",
            ))

        # 3. HTTP methods
        try:
            r = await self._client.request("OPTIONS", target)
            allow = r.headers.get("Allow", "")
            dangerous = [m for m in ["PUT", "DELETE", "TRACE", "CONNECT", "PATCH"] if m in allow]
            if dangerous:
                findings.append(Finding(
                    owasp_id    = "A05",
                    title       = "Security Misconfiguration — Dangerous HTTP Methods Enabled",
                    severity    = "MEDIUM",
                    description = f"The server advertises potentially dangerous HTTP methods: {', '.join(dangerous)}.",
                    evidence    = f"Allow: {allow}",
                    remediation = "Disable HTTP methods not required by the application (especially TRACE, PUT, DELETE).",
                    cwe         = "CWE-16",
                ))
        except Exception:
            pass

        # 4. robots.txt intelligence gathering
        try:
            r = await self._client.get(f"{target}/robots.txt")
            if r.status_code == 200:
                disallowed = [
                    line.split(":", 1)[1].strip()
                    for line in r.text.splitlines()
                    if line.lower().startswith("disallow:")
                ]
                if disallowed:
                    findings.append(Finding(
                        owasp_id    = "A05",
                        title       = "Security Misconfiguration — Sensitive Paths in robots.txt",
                        severity    = "INFO",
                        description = "robots.txt discloses paths that hint at sensitive areas of the application.",
                        evidence    = "Disallowed paths:\n" + "\n".join(disallowed[:20]),
                        remediation = "Avoid listing sensitive paths in robots.txt. "
                                      "Security through obscurity is not access control.",
                        cwe         = "CWE-200",
                    ))
        except Exception:
            pass

        # 5. .git exposure check
        try:
            r = await self._client.get(f"{target}/.git/HEAD")
            if r.status_code == 200 and "ref:" in r.text:
                findings.append(Finding(
                    owasp_id    = "A05",
                    title       = "Security Misconfiguration — .git Directory Exposed",
                    severity    = "CRITICAL",
                    description = "The .git directory is publicly accessible. "
                                  "Attackers can download the full source code.",
                    evidence    = f"/.git/HEAD returned HTTP 200:\n{r.text[:200]}",
                    remediation = "Block access to .git at the web server level. "
                                  "Never deploy .git to public web roots.",
                    cwe         = "CWE-538",
                ))
        except Exception:
            pass

        return findings

    # ══════════════════════════════════════════
    #  A06 – VULNERABLE AND OUTDATED COMPONENTS
    # ══════════════════════════════════════════

    async def _check_vulnerable_components(
        self, target: str, home_resp: httpx.Response, crawled: list[str]
    ) -> list[Finding]:
        findings: list[Finding] = []
        body = home_resp.text

        # Known version disclosure patterns
        version_patterns = {
            "jQuery":       r"jquery[/-](\d+\.\d+[\.\d]*)(\.min)?\.js",
            "Bootstrap":    r"bootstrap[/-](\d+\.\d+[\.\d]*)(\.min)?\.(?:js|css)",
            "WordPress":    r"wp-content|wp-includes|wordpress",
            "Drupal":       r"drupal\.js|/sites/default/files",
            "Joomla":       r"/components/com_|joomla",
            "Angular":      r"angular(?:\.min)?\.js[^\"']*?(\d+\.\d+[\.\d]*)",
            "React":        r"react(?:\.min)?\.js",
            "Vue":          r"vue(?:\.min)?\.js",
        }

        detected: list[str] = []
        for lib, pattern in version_patterns.items():
            match = re.search(pattern, body, re.I)
            if match:
                ver_info = f"version {match.group(1)}" if match.lastindex and match.lastindex >= 1 else "(version unknown)"
                detected.append(f"• {lib} — {ver_info}")

        if detected:
            findings.append(Finding(
                owasp_id    = "A06",
                title       = "Vulnerable Components — Frontend Libraries Detected",
                severity    = "INFO",
                description = "The following client-side libraries were detected. "
                              "Check each against known CVEs.",
                evidence    = "\n".join(detected),
                remediation = "Keep all libraries up to date. Use a SCA tool (e.g., Dependabot, Snyk). "
                              "Avoid including unused libraries.",
                cwe         = "CWE-1104",
            ))

        # Server header version
        server = home_resp.headers.get("Server", "")
        if re.search(r"\d+\.\d+", server):
            findings.append(Finding(
                owasp_id    = "A06",
                title       = "Vulnerable Components — Server Version Disclosed",
                severity    = "LOW",
                description = f"The Server header reveals version information: `{server}`. "
                              "Attackers can search for known CVEs for this version.",
                evidence    = f"Server: {server}",
                remediation = "Suppress version information from the Server header.",
                cwe         = "CWE-200",
            ))

        return findings

    # ══════════════════════════════════════════
    #  A07 – IDENTIFICATION & AUTHENTICATION FAILURES
    # ══════════════════════════════════════════

    async def _check_auth_failures(
        self, target: str, home_resp: httpx.Response, crawled: list[str]
    ) -> list[Finding]:
        findings: list[Finding] = []

        # 1. Login page detection + weak credential note
        login_indicators = ["login", "signin", "sign-in", "auth", "password", "username"]
        login_urls = [
            u for u in [target] + crawled
            if any(ind in u.lower() for ind in login_indicators)
        ]

        if login_urls:
            findings.append(Finding(
                owasp_id    = "A07",
                title       = "Auth Failures — Login Page Found (Manual Review Required)",
                severity    = "INFO",
                description = "Login endpoint(s) detected. Verify rate limiting, account lockout, "
                              "MFA availability, and that default credentials are not accepted.",
                evidence    = "\n".join(login_urls[:5]) + "\n\nCommon default credentials to test manually:\n"
                              + ", ".join(self._WEAK_CREDS_NOTE),
                remediation = "Implement rate limiting, CAPTCHA, account lockout after N failures, "
                              "and Multi-Factor Authentication.",
                cwe         = "CWE-287",
            ))

        # 2. Session token entropy / predictability (surface check)
        session_cookies = [
            c for c in home_resp.headers.get_list("Set-Cookie")
            if any(k in c.lower() for k in ["session", "sessid", "phpsessid", "jsessionid", "sid", "token"])
        ]
        for sc in session_cookies:
            name = sc.split("=")[0]
            value_match = re.search(r"=([^;]+)", sc)
            if value_match:
                value = value_match.group(1)
                if len(value) < 16:
                    findings.append(Finding(
                        owasp_id    = "A07",
                        title       = f"Auth Failures — Short Session Token: {name}",
                        severity    = "HIGH",
                        description = f"Session cookie `{name}` has a short value ({len(value)} chars) "
                                      "which may be brute-forceable.",
                        evidence    = f"Cookie: {sc[:150]}",
                        remediation = "Use cryptographically secure random session IDs of at least 128 bits.",
                        cwe         = "CWE-331",
                    ))

        # 3. Password reset page check
        reset_paths = ["/forgot-password", "/reset-password", "/password-reset", "/account/recover"]
        for path in reset_paths:
            try:
                r = await self._client.get(target + path)
                if r.status_code == 200:
                    findings.append(Finding(
                        owasp_id    = "A07",
                        title       = "Auth Failures — Password Reset Page Found",
                        severity    = "INFO",
                        description = "A password reset page was found. Verify it is rate-limited "
                                      "and uses time-limited, single-use tokens.",
                        evidence    = f"HTTP 200 at {target + path}",
                        remediation = "Ensure reset tokens expire quickly, are single-use, "
                                      "and that the endpoint is rate-limited.",
                        cwe         = "CWE-640",
                    ))
                    break
            except Exception:
                pass

        return findings

    # ══════════════════════════════════════════
    #  A08 – SOFTWARE & DATA INTEGRITY FAILURES
    # ══════════════════════════════════════════

    async def _check_integrity_failures(
        self, target: str, home_resp: httpx.Response, crawled: list[str]
    ) -> list[Finding]:
        findings: list[Finding] = []

        # 1. CDN resources without SRI
        sri_pattern = re.compile(
            r'<(script|link)[^>]+(?:src|href)=["\']https?://(?!{host})[^"\']+["\'][^>]*>'.format(
                host=re.escape(urllib.parse.urlparse(target).netloc)
            ),
            re.I
        )
        external_resources = sri_pattern.findall(home_resp.text)
        sri_missing = []
        for tag in re.finditer(sri_pattern, home_resp.text):
            full_tag = tag.group(0)
            if "integrity=" not in full_tag.lower():
                sri_missing.append(full_tag[:150])

        if sri_missing:
            findings.append(Finding(
                owasp_id    = "A08",
                title       = "Integrity Failures — External Resources Without SRI",
                severity    = "MEDIUM",
                description = "External scripts or stylesheets are loaded without Subresource Integrity (SRI) hashes. "
                              "A compromised CDN could inject malicious code.",
                evidence    = "\n".join(sri_missing[:5]),
                remediation = "Add `integrity` and `crossorigin` attributes to all external <script> and <link> tags. "
                              "Use https://www.srihash.org to generate hashes.",
                cwe         = "CWE-353",
            ))

        # 2. Dangerous deserialization endpoints (heuristic)
        deser_paths = ["/api/deserialize", "/api/object", "/rpc", "/remoting"]
        for path in deser_paths:
            try:
                r = await self._client.get(target + path)
                if r.status_code in (200, 405):
                    findings.append(Finding(
                        owasp_id    = "A08",
                        title       = "Integrity Failures — Possible Deserialization Endpoint",
                        severity    = "INFO",
                        description = f"A potential deserialization endpoint was found at `{path}`. "
                                      "Manual review recommended.",
                        evidence    = f"HTTP {r.status_code} at {target + path}",
                        remediation = "Avoid deserialising untrusted data. "
                                      "Use integrity checks and type-safe serialisation formats (JSON with schema).",
                        cwe         = "CWE-502",
                    ))
            except Exception:
                pass

        return findings

    # ══════════════════════════════════════════
    #  A09 – LOGGING & MONITORING FAILURES
    # ══════════════════════════════════════════

    async def _check_logging_failures(
        self, target: str, home_resp: httpx.Response, crawled: list[str]
    ) -> list[Finding]:
        findings: list[Finding] = []

        # We can only observe external signals; flag as a manual review item
        findings.append(Finding(
            owasp_id    = "A09",
            title       = "Logging & Monitoring — Manual Review Required",
            severity    = "INFO",
            description = "Logging and Monitoring failures cannot be fully assessed via black-box "
                          "testing. The following questions require internal review:",
            evidence    = (
                "• Are failed login attempts logged?\n"
                "• Are access control failures alerted on?\n"
                "• Are logs stored in a tamper-evident, centralised system?\n"
                "• Is there alerting for suspicious patterns (e.g. repeated 403s)?\n"
                "• Are logs reviewed regularly?\n"
                "• Is there an incident response plan?"
            ),
            remediation = "Implement centralised logging (ELK, Splunk, CloudWatch). "
                          "Set up alerts for failed auth, access violations, and anomalies. "
                          "Ensure logs cannot be deleted by attackers.",
            cwe         = "CWE-778",
        ))

        # Check if X-Request-ID or similar correlation headers are absent
        tracing_headers = ["X-Request-ID", "X-Correlation-ID", "X-Trace-ID"]
        if not any(h in home_resp.headers for h in tracing_headers):
            findings.append(Finding(
                owasp_id    = "A09",
                title       = "Logging & Monitoring — No Request Correlation Header",
                severity    = "INFO",
                description = "No request correlation header (X-Request-ID, X-Correlation-ID) found. "
                              "These help trace requests through logs.",
                evidence    = "None of the following headers found in response: " + ", ".join(tracing_headers),
                remediation = "Add a unique request ID to each response and include it in server logs.",
                cwe         = "CWE-778",
            ))

        return findings

    # ══════════════════════════════════════════
    #  A10 – SSRF
    # ══════════════════════════════════════════

    async def _check_ssrf(
        self, target: str, home_resp: httpx.Response, crawled: list[str]
    ) -> list[Finding]:
        findings: list[Finding] = []

        # Look for parameters that accept URLs
        url_param_patterns = ["url", "uri", "link", "src", "source", "redirect",
                               "callback", "fetch", "proxy", "load", "path", "file"]

        ssrf_params: list[str] = []
        for url in [target] + crawled:
            if "?" not in url:
                continue
            parsed = urllib.parse.urlparse(url)
            qs = urllib.parse.parse_qs(parsed.query)
            for param in qs:
                if any(kw in param.lower() for kw in url_param_patterns):
                    ssrf_params.append(f"`{param}` in {url}")

        if ssrf_params:
            findings.append(Finding(
                owasp_id    = "A10",
                title       = "SSRF — URL-Accepting Parameters Found",
                severity    = "HIGH",
                description = "Parameters that may accept URLs were found. "
                              "If the server fetches these URLs, SSRF may be possible, "
                              "allowing access to internal services.",
                evidence    = "\n".join(ssrf_params[:10])
                              + "\n\nProbes to test manually:\n"
                              + "\n".join(self._SSRF_PROBES),
                remediation = "Validate and whitelist URLs server-side. "
                              "Use an allowlist of permitted domains/IPs. "
                              "Block requests to 169.254.x.x (cloud metadata) and RFC 1918 ranges.",
                cwe         = "CWE-918",
            ))
        else:
            findings.append(Finding(
                owasp_id    = "A10",
                title       = "SSRF — No Obvious URL Parameters Found",
                severity    = "INFO",
                description = "No URL-accepting query parameters were detected via passive analysis. "
                              "SSRF may still exist in POST bodies or API endpoints.",
                evidence    = "No URL-like parameters found in crawled URLs.",
                remediation = "Review all server-side HTTP requests. "
                              "Apply SSRF mitigations for any functionality that fetches remote resources.",
                cwe         = "CWE-918",
            ))

        # Open redirect check
        redirect_params = ["redirect", "return", "next", "goto", "url", "dest", "destination"]
        for url in [target] + crawled:
            if "?" not in url:
                continue
            parsed = urllib.parse.urlparse(url)
            qs = urllib.parse.parse_qs(parsed.query)
            for param in qs:
                if param.lower() in redirect_params:
                    test_url = parsed._replace(
                        query=urllib.parse.urlencode({**qs, param: ["https://evil.example.com"]}, doseq=True)
                    ).geturl()
                    try:
                        r = await self._client.get(test_url, follow_redirects=False)
                        loc = r.headers.get("Location", "")
                        if "evil.example.com" in loc:
                            findings.append(Finding(
                                owasp_id    = "A10",
                                title       = "SSRF / A01 — Open Redirect Confirmed",
                                severity    = "MEDIUM",
                                description = f"Parameter `{param}` performs an unvalidated redirect to an attacker-controlled URL.",
                                evidence    = f"Request: {test_url}\nLocation: {loc}",
                                remediation = "Validate redirect targets against a strict allowlist. "
                                              "Never redirect to user-supplied external URLs.",
                                cwe         = "CWE-601",
                            ))
                    except Exception:
                        pass

        return findings

    # ══════════════════════════════════════════
    #  REPORT GENERATION
    # ══════════════════════════════════════════

    def _findings_to_dict(self, target: str, findings: list[Finding]) -> dict:
        return {
            "target":     target,
            "generated":  datetime.utcnow().isoformat() + "Z",
            "findings":   [
                {
                    "owasp_id":    f.owasp_id,
                    "title":       f.title,
                    "severity":    f.severity,
                    "description": f.description,
                    "evidence":    f.evidence,
                    "remediation": f.remediation,
                    "url":         f.url,
                    "cwe":         f.cwe,
                    "timestamp":   f.timestamp,
                }
                for f in findings
            ],
        }

    def _build_markdown_report(
        self, target: str, findings: list[Finding], crawled: list[str]
    ) -> str:
        sev_emoji = {
            "CRITICAL": "🔴",
            "HIGH":     "🟠",
            "MEDIUM":   "🟡",
            "LOW":      "🔵",
            "INFO":     "⚪",
        }
        sev_order = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]
        sev_counts = {s: sum(1 for f in findings if f.severity == s) for s in sev_order}

        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

        lines: list[str] = []

        # ── Cover ─────────────────────────────────────
        lines += [
            "# 🛡️ Web Security Audit Report",
            "",
            f"| Field | Value |",
            f"|---|---|",
            f"| **Target** | `{target}` |",
            f"| **Generated** | {now} |",
            f"| **Standard** | OWASP Top 10 (2021) |",
            f"| **Tool** | Nexus Agent — WebSecurityAuditTool |",
            f"| **URLs Crawled** | {len(crawled)} |",
            "",
            "---",
            "",
            "## 📊 Executive Summary",
            "",
            "| Severity | Count |",
            "|---|---|",
        ]
        for sev in sev_order:
            emoji = sev_emoji[sev]
            lines.append(f"| {emoji} {sev} | {sev_counts[sev]} |")

        total = len(findings)
        risk = (
            "🔴 CRITICAL" if sev_counts["CRITICAL"] > 0
            else "🟠 HIGH"   if sev_counts["HIGH"]     > 0
            else "🟡 MEDIUM" if sev_counts["MEDIUM"]   > 0
            else "🔵 LOW"    if sev_counts["LOW"]       > 0
            else "✅ CLEAN"
        )
        lines += [
            "",
            f"**Overall Risk:** {risk}  ",
            f"**Total Findings:** {total}",
            "",
            "---",
            "",
            "## 🔍 OWASP Top 10 Coverage",
            "",
            "| ID | Category | Status |",
            "|---|---|---|",
        ]

        owasp_categories = {
            "A01": "Broken Access Control",
            "A02": "Cryptographic Failures",
            "A03": "Injection",
            "A04": "Insecure Design",
            "A05": "Security Misconfiguration",
            "A06": "Vulnerable & Outdated Components",
            "A07": "Identification & Auth Failures",
            "A08": "Software & Data Integrity Failures",
            "A09": "Logging & Monitoring Failures",
            "A10": "Server-Side Request Forgery (SSRF)",
        }

        for oid, cat in owasp_categories.items():
            cat_findings = [f for f in findings if f.owasp_id == oid]
            if not cat_findings:
                lines.append(f"| {oid} | {cat} | ✅ No issues found |")
            else:
                worst = next(
                    (s for s in sev_order if any(f.severity == s for f in cat_findings)),
                    "INFO"
                )
                lines.append(f"| {oid} | {cat} | {sev_emoji[worst]} {len(cat_findings)} finding(s) |")

        lines += ["", "---", "", "## 📋 Detailed Findings", ""]

        # ── Group by severity ──────────────────────────
        for sev in sev_order:
            sev_findings = [f for f in findings if f.severity == sev]
            if not sev_findings:
                continue

            lines += [
                f"### {sev_emoji[sev]} {sev} ({len(sev_findings)})",
                "",
            ]

            for idx, f in enumerate(sev_findings, 1):
                lines += [
                    f"#### {idx}. {f.title}",
                    "",
                    f"| Attribute | Value |",
                    f"|---|---|",
                    f"| **OWASP** | A{f.owasp_id.lstrip('A0')} – {owasp_categories.get(f.owasp_id, '')} |",
                    f"| **Severity** | {sev_emoji[sev]} {f.severity} |",
                    f"| **CWE** | {f.cwe or 'N/A'} |",
                    f"| **Detected** | {f.timestamp} |",
                    "",
                    "**Description**",
                    "",
                    f.description,
                    "",
                    "**Evidence**",
                    "",
                    "```",
                    f.evidence[:800],
                    "```",
                    "",
                    "**Remediation**",
                    "",
                    f.remediation,
                    "",
                    "---",
                    "",
                ]

        # ── Crawled URLs ───────────────────────────────
        if crawled:
            lines += [
                "## 🕷️ Crawled URLs",
                "",
                "```",
            ]
            lines += crawled[:50]
            if len(crawled) > 50:
                lines.append(f"... and {len(crawled) - 50} more")
            lines += ["```", ""]

        # ── Disclaimer ────────────────────────────────
        lines += [
            "---",
            "",
            "## ⚠️ Disclaimer",
            "",
            "> This report was generated by an automated tool performing **read-only, non-destructive** "
            "probes against the target. Automated testing cannot replace manual penetration testing. "
            "Some findings may be false positives; all findings should be manually verified. "
            "Only test systems you own or have explicit written permission to test.",
            "",
        ]

        return "\n".join(lines)
