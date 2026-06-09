# 🛡️ WebSecurityAuditTool — Integration Guide

## 1. Copy the file

```bash
cp web_security_audit.py nexus_agent/Tools/builtin/web_security_audit.py
```

## 2. Register the tool in `Tools/builtin/__init__.py`

Add **one import line** and **one entry** to `get_all_builtin_tools()`:

```python
# existing imports …
from Tools.builtin.web_security_audit import WebSecurityAuditTool   # ← ADD THIS

__all__ = [
    # … existing entries …
    "WebSecurityAuditTool",   # ← ADD THIS
]

def get_all_builtin_tools() -> list[type]:
    return [
        # … existing tools …
        WebSecurityAuditTool,   # ← ADD THIS
    ]
```

## 3. No extra dependencies needed

The tool uses only:
- `httpx` — already used by `web_fetch.py`
- Python stdlib (`re`, `json`, `urllib`, `datetime`, `pathlib`)

---

## 4. Usage examples (inside the agent)

### Full audit (all OWASP Top 10 checks)
```
Audit https://example.com for OWASP Top 10 vulnerabilities and save the report to reports/audit.md
```

### Targeted checks only
```
Run only injection and auth_failures checks on https://example.com
```

### With JSON output
```
Audit https://testphp.vulnweb.com, save JSON too, crawl 2 levels deep
```

### Via tool call directly
```json
{
  "target_url":       "https://example.com",
  "output_path":      "security_report.md",
  "save_json":        true,
  "crawl_depth":      1,
  "timeout":          15,
  "checks":           null
}
```

---

## 5. What it checks

| OWASP ID | Category | What's tested |
|---|---|---|
| A01 | Broken Access Control | Sensitive path enumeration, IDOR patterns, CORS wildcard |
| A02 | Cryptographic Failures | HTTP vs HTTPS, HSTS strength, insecure cookie flags, mixed content |
| A03 | Injection | Reflected XSS, SQL error disclosure, path traversal |
| A04 | Insecure Design | Verbose error/stack trace exposure, directory listing |
| A05 | Security Misconfiguration | Missing security headers, leaky headers, HTTP methods, robots.txt, .git exposure |
| A06 | Vulnerable Components | Frontend library detection, server version disclosure |
| A07 | Auth Failures | Login page detection, weak session token length, password reset endpoint |
| A08 | Integrity Failures | External resources without SRI hashes |
| A09 | Logging & Monitoring | Request correlation headers, manual review checklist |
| A10 | SSRF | URL-accepting parameters, open redirect detection |

---

## 6. Report output

The tool generates a **professional Markdown report** with:
- Executive summary table (severity counts + overall risk rating)
- OWASP Top 10 coverage matrix
- Detailed findings sorted by severity (CRITICAL → INFO)
  - Description, evidence, remediation, CWE reference
- Crawled URL list
- Legal disclaimer

Optionally also outputs a **machine-readable JSON** file.

---

## ⚠️ Legal notice

Only use against systems you **own or have explicit written permission** to test.
