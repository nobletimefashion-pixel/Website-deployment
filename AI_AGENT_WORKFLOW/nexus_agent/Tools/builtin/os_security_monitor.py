# Tools/builtin/os_security_monitor.py
"""
OS Security Monitor & Hardening Tool

Monitors the operating system for suspicious activity, malicious processes,
and security weaknesses — then either terminates confirmed threats or reports
borderline cases. Also applies OS-level security hardening.

Philosophy:
  - TERMINATE only when confidence >= TERMINATE_THRESHOLD (default 95%)
  - REPORT  when confidence is in the [REPORT_THRESHOLD, TERMINATE_THRESHOLD) range
  - IGNORE  everything below REPORT_THRESHOLD (too noisy)

All actions are logged to a timestamped Markdown report.
"""

import asyncio
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field as PydanticField

from nexus_agent.Tools.base import Tool, ToolInvokation, ToolKind, ToolResult

# ─────────────────────────────────────────────────────────────────────────────
# DATA CLASSES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ProcessInfo:
    pid: int
    name: str
    exe: str
    cmdline: str
    user: str
    cpu_percent: float
    mem_percent: float
    status: str
    ppid: int
    open_ports: list[int] = field(default_factory=list)
    connections: list[str] = field(default_factory=list)


@dataclass
class ThreatSignal:
    """A single piece of evidence contributing to a threat score."""
    rule: str          # short rule ID
    description: str   # human-readable
    weight: int        # 1–100; weights add up to form confidence %


@dataclass
class ThreatReport:
    process: ProcessInfo
    signals: list[ThreatSignal]
    confidence: int        # 0–100
    verdict: str           # TERMINATED | REPORTED | MONITORED | CLEAN
    action_taken: str
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")


@dataclass
class HardeningAction:
    category: str
    title: str
    command: str
    output: str
    success: bool
    skipped: bool = False
    skip_reason: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# TOOL PARAMS
# ─────────────────────────────────────────────────────────────────────────────

class OSSecurityMonitorParams(BaseModel):
    mode: Literal["monitor", "harden", "full"] = PydanticField(
        "full",
        description=(
            "'monitor' — scan for suspicious processes only; "
            "'harden'  — apply OS hardening only; "
            "'full'    — both (default)"
        )
    )
    output_path: str = PydanticField(
        "os_security_report.md",
        description="Path for the Markdown report (default: os_security_report.md)"
    )
    terminate_threshold: int = PydanticField(
        95,
        ge=80,
        le=100,
        description=(
            "Minimum confidence score (0-100) required to TERMINATE a process. "
            "Default 95 — only kill when we are virtually certain."
        )
    )
    report_threshold: int = PydanticField(
        50,
        ge=10,
        le=94,
        description=(
            "Minimum confidence score to REPORT a process as suspicious. "
            "Below this it is quietly ignored. Default 50."
        )
    )
    save_json: bool = PydanticField(
        False,
        description="Also save a machine-readable JSON alongside the Markdown"
    )
    dry_run: bool = PydanticField(
        False,
        description=(
            "If True, never actually terminate processes or apply hardening — "
            "only show what WOULD happen. Useful for auditing without side-effects."
        )
    )
    harden_categories: list[str] | None = PydanticField(
        None,
        description=(
            "Which hardening categories to apply. None = all. "
            "Options: firewall, sysctl, ssh, filesystem, services, accounts, auditd, apparmor"
        )
    )


# ─────────────────────────────────────────────────────────────────────────────
# DETECTION RULES  (each rule returns a ThreatSignal or None)
# ─────────────────────────────────────────────────────────────────────────────

# Known-bad process names / exe patterns — weight 70
_KNOWN_BAD_NAMES: dict[str, str] = {
    "nc":              "netcat — common C2 / reverse-shell tool",
    "ncat":            "ncat — nmap netcat variant",
    "nmap":            "nmap port scanner",
    "masscan":         "masscan high-speed scanner",
    "msfconsole":      "Metasploit Framework console",
    "msfvenom":        "Metasploit payload generator",
    "meterpreter":     "Metasploit Meterpreter shell",
    "empire":          "PowerShell Empire C2 framework",
    "cobaltstrike":    "Cobalt Strike beacon",
    "mimikatz":        "credential dumper",
    "lazagne":         "LaZagne credential harvester",
    "hydra":           "THC-Hydra brute-forcer",
    "medusa":          "Medusa brute-forcer",
    "john":            "John the Ripper password cracker",
    "hashcat":         "Hashcat password cracker",
    "sqlmap":          "sqlmap SQL injection tool",
    "beef":            "BeEF browser exploitation framework",
    "setoolkit":       "Social Engineering Toolkit",
    "arpspoof":        "ARP spoofing / MITM tool",
    "ettercap":        "Ettercap MITM framework",
    "bettercap":       "bettercap MITM framework",
    "wireshark":       "packet capture (unexpected on servers)",
    "tcpdump":         "packet capture",
    "tshark":          "terminal Wireshark",
    "cryptominer":     "generic crypto miner label",
    "xmrig":           "XMRig Monero miner",
    "minerd":          "cpuminer",
    "cgminer":         "cgminer crypto miner",
    "bfgminer":        "bfgminer crypto miner",
    "stratum":         "mining stratum protocol binary",
    "kinsing":         "Kinsing cloud malware",
    "kerberoas":       "Kerberoast attack tool",
    "impacket":        "Impacket network exploitation",
}

# Suspicious command-line substrings — weight varies
_SUSPICIOUS_CMDLINE: list[tuple[str, int, str]] = [
    # (substring,               weight, description)
    ("bash -i >&",              90,  "bash reverse shell redirect"),
    ("sh -i >&",                90,  "sh reverse shell redirect"),
    ("/dev/tcp/",               90,  "bash /dev/tcp reverse shell"),
    ("/dev/udp/",               85,  "bash /dev/udp reverse shell"),
    ("python -c 'import socket", 85, "Python reverse shell one-liner"),
    ("python3 -c 'import socket",85, "Python3 reverse shell one-liner"),
    ("perl -e 'use Socket",     85,  "Perl reverse shell one-liner"),
    ("ruby -rsocket",           85,  "Ruby reverse shell"),
    ("php -r '$sock",           85,  "PHP reverse shell one-liner"),
    ("exec /bin/sh",            80,  "exec shell substitution"),
    ("exec /bin/bash",          80,  "exec bash substitution"),
    ("0>&1 2>&1",               75,  "stdout/stderr redirection to socket"),
    ("mkfifo",                  70,  "named pipe — common in reverse shells"),
    ("base64 -d |",             65,  "base64 decode pipe — often obfuscation"),
    ("base64 --decode |",       65,  "base64 decode pipe — often obfuscation"),
    ("curl | bash",             80,  "curl-pipe-bash remote code execution"),
    ("wget -O- | bash",         80,  "wget-pipe-bash remote code execution"),
    ("wget -q -O /tmp/",        60,  "silent wget to /tmp — dropper pattern"),
    ("curl -o /tmp/",           60,  "curl download to /tmp — dropper pattern"),
    ("chmod +x /tmp/",          65,  "make /tmp file executable — dropper"),
    ("chmod 777 /tmp/",         60,  "world-writable /tmp binary"),
    ("crontab -",               55,  "crontab modification"),
    ("LD_PRELOAD=",             85,  "LD_PRELOAD hijack"),
    ("ptrace",                  70,  "ptrace — process injection / debugging"),
    ("keylogger",               95,  "keylogger string in command"),
    ("cryptominer",             95,  "miner string in command"),
    ("stratum+tcp",             95,  "mining stratum protocol"),
    ("stratum+ssl",             95,  "mining stratum SSL protocol"),
    ("-e 'system(",             80,  "system() call in one-liner"),
    ("nohup ./ &",              60,  "daemonised executable in current dir"),
    ("disown",                  55,  "job disown — persistence pattern"),
    ("/proc/self/mem",          90,  "direct /proc/self/mem write — injection"),
    ("memfd_create",            90,  "memfd fileless execution"),
    ("dd if=/dev/mem",          90,  "raw memory read"),
    ("insmod",                  70,  "kernel module insertion"),
    ("rmmod",                   65,  "kernel module removal"),
    ("modprobe",                55,  "kernel module management"),
]

# High CPU heuristic thresholds
_HIGH_CPU_THRESHOLD = 85.0      # >85 % sustained CPU → suspicious
_MINER_CPU_THRESHOLD = 95.0     # >95 % → very likely miner

# Suspicious listening ports
_SUSPICIOUS_PORTS: dict[int, str] = {
    1337:  "common hacker/backdoor port",
    4444:  "Metasploit default reverse-shell port",
    4445:  "Metasploit secondary port",
    5555:  "common RAT / ADB port",
    6666:  "common IRC / trojan port",
    6667:  "IRC — sometimes used for C2",
    6668:  "IRC variant",
    6669:  "IRC variant",
    8080:  "alternate HTTP — watch for unauthorised servers",
    8888:  "Jupyter / common dev port on production",
    9001:  "Tor ORPort default",
    9050:  "Tor SOCKS proxy default",
    9051:  "Tor control port",
    31337: "Back Orifice / elite hacker port",
    65535: "highest port — often chosen by RATs",
}

# Paths whose presence in cmdline is always suspicious
_SUSPICIOUS_PATHS = [
    "/tmp/.",        # hidden files in /tmp
    "/dev/shm/",     # shared memory — fileless malware staging
    "/var/tmp/.",    # hidden in /var/tmp
    "/run/shm/",     # old shared memory path
    "/proc/",        # direct /proc access
]


# ─────────────────────────────────────────────────────────────────────────────
# MAIN TOOL CLASS
# ─────────────────────────────────────────────────────────────────────────────

class OSSecurityMonitorTool(Tool):
    name = "os_security_monitor"
    description = (
        "Monitors the operating system for suspicious or malicious processes and "
        "applies security hardening. "
        "Processes are scored using multiple detection rules; a process is only "
        "TERMINATED when confidence >= terminate_threshold (default 95%). "
        "Lower-confidence cases are REPORTED for human review. "
        "Hardening covers: firewall (ufw/iptables), sysctl kernel params, SSH config, "
        "filesystem permissions, unused services, account security, auditd, and AppArmor. "
        "Generates a detailed Markdown (and optionally JSON) report. "
        "Linux/macOS only."
    )
    kind = ToolKind.SHELL
    schema = OSSecurityMonitorParams

    # ──────────────────────────────────────────────────────────────────────────
    # ENTRY POINT
    # ──────────────────────────────────────────────────────────────────────────

    async def execute(self, invocation: ToolInvokation) -> ToolResult:
        params = OSSecurityMonitorParams(**invocation.params)

        os_name = platform.system()
        if os_name not in ("Linux", "Darwin"):
            return ToolResult.error_result(
                f"OSSecurityMonitorTool supports Linux and macOS only. "
                f"Detected: {os_name}"
            )

        if params.terminate_threshold <= params.report_threshold:
            return ToolResult.error_result(
                "terminate_threshold must be greater than report_threshold."
            )

        print(f"\n🛡️  OS Security Monitor — {os_name} {platform.release()}")
        print(f"    PID: {os.getpid()} | User: {self._current_user()}")
        print(f"    Mode: {params.mode.upper()}")
        print(f"    Terminate at: ≥{params.terminate_threshold}% | Report at: ≥{params.report_threshold}%")
        if params.dry_run:
            print("    ⚠️  DRY-RUN — no changes will be made\n")
        else:
            print()

        threat_reports: list[ThreatReport] = []
        hardening_actions: list[HardeningAction] = []

        # ── Monitor ──────────────────────────────────────────────────────────
        if params.mode in ("monitor", "full"):
            print("🔍  Scanning processes …")
            processes = await self._collect_processes()
            print(f"    Collected {len(processes)} process(es)\n")

            for proc in processes:
                report = self._analyse_process(proc, params)
                if report.verdict != "CLEAN":
                    threat_reports.append(report)
                    print(f"    [{report.verdict}] PID {proc.pid} {proc.name} "
                          f"— confidence {report.confidence}%")
                    if report.verdict == "TERMINATED" and not params.dry_run:
                        await self._terminate_process(proc.pid)

            terminated = sum(1 for r in threat_reports if r.verdict == "TERMINATED")
            reported   = sum(1 for r in threat_reports if r.verdict == "REPORTED")
            print(f"\n    ✅  Scan complete — {terminated} terminated, {reported} reported\n")

        # ── Harden ───────────────────────────────────────────────────────────
        if params.mode in ("harden", "full"):
            print("🔒  Applying OS hardening …\n")
            hardening_actions = await self._harden_os(params, os_name)
            ok  = sum(1 for h in hardening_actions if h.success and not h.skipped)
            skp = sum(1 for h in hardening_actions if h.skipped)
            err = sum(1 for h in hardening_actions if not h.success and not h.skipped)
            print(f"\n    ✅  Hardening complete — {ok} applied, {skp} skipped, {err} failed\n")

        # ── Report ───────────────────────────────────────────────────────────
        report_md   = self._build_report(params, threat_reports, hardening_actions, os_name)
        output_path = Path(invocation.cwd) / params.output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report_md, encoding="utf-8")
        print(f"📄  Report saved → {output_path}")

        if params.save_json:
            json_path = output_path.with_suffix(".json")
            json_path.write_text(
                json.dumps(self._to_dict(params, threat_reports, hardening_actions, os_name), indent=2),
                encoding="utf-8"
            )
            print(f"📄  JSON saved   → {json_path}")

        terminated = sum(1 for r in threat_reports if r.verdict == "TERMINATED")
        reported   = sum(1 for r in threat_reports if r.verdict == "REPORTED")
        hardened   = sum(1 for h in hardening_actions if h.success and not h.skipped)

        summary = (
            f"🛡️  OS Security Monitor complete\n"
            f"  Processes: {terminated} terminated | {reported} reported suspicious\n"
            f"  Hardening: {hardened} control(s) applied\n"
            f"  Report: {output_path}"
        )

        return ToolResult.success_result(
            output=summary,
            metadata={
                "terminated":        terminated,
                "reported":          reported,
                "hardening_applied": hardened,
                "report_path":       str(output_path),
                "dry_run":           params.dry_run,
            }
        )

    # ──────────────────────────────────────────────────────────────────────────
    # PROCESS COLLECTION  (cross-platform)
    # ──────────────────────────────────────────────────────────────────────────

    async def _collect_processes(self) -> list[ProcessInfo]:
        """Collect running processes using psutil (preferred) or /proc fallback."""
        try:
            import psutil
            return self._collect_via_psutil(psutil)
        except ImportError:
            print("    ⚠️  psutil not installed — falling back to /proc parser")
            return await self._collect_via_proc()

    def _collect_via_psutil(self, psutil) -> list[ProcessInfo]:
        procs: list[ProcessInfo] = []
        for proc in psutil.process_iter(
            ["pid", "name", "exe", "cmdline", "username",
             "cpu_percent", "memory_percent", "status", "ppid"]
        ):
            try:
                info = proc.info
                # CPU needs two samples; use interval=0.1 for speed
                cpu = proc.cpu_percent(interval=0.1)

                # Connections
                conns = []
                ports = []
                try:
                    for c in proc.connections(kind="inet"):
                        if c.laddr:
                            ports.append(c.laddr.port)
                        conns.append(
                            f"{c.laddr} → {c.raddr if c.raddr else 'LISTEN'} [{c.status}]"
                        )
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    pass

                procs.append(ProcessInfo(
                    pid         = info["pid"],
                    name        = info["name"] or "",
                    exe         = info["exe"] or "",
                    cmdline     = " ".join(info["cmdline"] or []),
                    user        = info["username"] or "",
                    cpu_percent = cpu,
                    mem_percent = info["memory_percent"] or 0.0,
                    status      = info["status"] or "",
                    ppid        = info["ppid"] or 0,
                    open_ports  = ports,
                    connections = conns,
                ))
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
        return procs

    async def _collect_via_proc(self) -> list[ProcessInfo]:
        """Minimal /proc-based fallback for Linux without psutil."""
        procs: list[ProcessInfo] = []
        proc_root = Path("/proc")
        if not proc_root.exists():
            return procs

        for pid_dir in proc_root.iterdir():
            if not pid_dir.name.isdigit():
                continue
            pid = int(pid_dir.name)
            try:
                cmdline = (pid_dir / "cmdline").read_text().replace("\x00", " ").strip()
                comm    = (pid_dir / "comm").read_text().strip()
                status_text = (pid_dir / "status").read_text()

                user  = ""
                ppid  = 0
                for line in status_text.splitlines():
                    if line.startswith("Uid:"):
                        uid = line.split()[1]
                        try:
                            import pwd
                            user = pwd.getpwuid(int(uid)).pw_name
                        except Exception:
                            user = uid
                    if line.startswith("PPid:"):
                        ppid = int(line.split()[1])

                exe = ""
                try:
                    exe = str((pid_dir / "exe").resolve())
                except Exception:
                    pass

                procs.append(ProcessInfo(
                    pid         = pid,
                    name        = comm,
                    exe         = exe,
                    cmdline     = cmdline,
                    user        = user,
                    cpu_percent = 0.0,
                    mem_percent = 0.0,
                    status      = "unknown",
                    ppid        = ppid,
                ))
            except Exception:
                pass
        return procs

    # ──────────────────────────────────────────────────────────────────────────
    # THREAT ANALYSIS ENGINE
    # ──────────────────────────────────────────────────────────────────────────

    def _analyse_process(
        self, proc: ProcessInfo, params: OSSecurityMonitorParams
    ) -> ThreatReport:
        """
        Apply all detection rules, accumulate signals, compute confidence,
        and decide verdict.
        """
        signals: list[ThreatSignal] = []
        name_lower    = proc.name.lower()
        cmdline_lower = proc.cmdline.lower()
        exe_lower     = proc.exe.lower()

        # ── Rule 1: Known-bad process name ───────────────────────────────────
        for bad_name, desc in _KNOWN_BAD_NAMES.items():
            if bad_name in name_lower or bad_name in exe_lower:
                signals.append(ThreatSignal(
                    rule        = "KNOWN_BAD_NAME",
                    description = f"Process name matches known-bad tool: {bad_name} ({desc})",
                    weight      = 70,
                ))
                break

        # ── Rule 2: Suspicious cmdline patterns ──────────────────────────────
        for substring, weight, desc in _SUSPICIOUS_CMDLINE:
            if substring.lower() in cmdline_lower:
                signals.append(ThreatSignal(
                    rule        = "SUSPICIOUS_CMDLINE",
                    description = f"Command line contains: '{substring}' — {desc}",
                    weight      = weight,
                ))

        # ── Rule 3: Execution from writable / temp directories ────────────────
        writable_dirs = ["/tmp/", "/dev/shm/", "/var/tmp/", "/run/shm/"]
        for wdir in writable_dirs:
            if wdir in exe_lower or (wdir in cmdline_lower and "wget" not in cmdline_lower):
                signals.append(ThreatSignal(
                    rule        = "EXEC_FROM_TMP",
                    description = f"Process executing from world-writable directory: {wdir}",
                    weight      = 70,
                ))
                break

        # ── Rule 4: Hidden file execution  (starts with dot or space) ────────
        exe_basename = Path(proc.exe).name if proc.exe else proc.name
        if exe_basename.startswith(".") or exe_basename.startswith(" "):
            signals.append(ThreatSignal(
                rule        = "HIDDEN_EXE",
                description = f"Executable has hidden/space-prefixed name: '{exe_basename}'",
                weight      = 80,
            ))

        # ── Rule 5: Suspicious listening ports ────────────────────────────────
        for port in proc.open_ports:
            if port in _SUSPICIOUS_PORTS:
                signals.append(ThreatSignal(
                    rule        = "SUSPICIOUS_PORT",
                    description = f"Listening on suspicious port {port}: {_SUSPICIOUS_PORTS[port]}",
                    weight      = 65,
                ))

        # ── Rule 6: CPU heuristic (potential miner) ───────────────────────────
        if proc.cpu_percent >= _MINER_CPU_THRESHOLD:
            signals.append(ThreatSignal(
                rule        = "HIGH_CPU_MINER",
                description = f"CPU usage {proc.cpu_percent:.1f}% — consistent with crypto miner",
                weight      = 60,
            ))
        elif proc.cpu_percent >= _HIGH_CPU_THRESHOLD:
            signals.append(ThreatSignal(
                rule        = "HIGH_CPU",
                description = f"Unusually high CPU usage: {proc.cpu_percent:.1f}%",
                weight      = 30,
            ))

        # ── Rule 7: Running as root with network activity ─────────────────────
        if proc.user in ("root", "0") and proc.connections:
            signals.append(ThreatSignal(
                rule        = "ROOT_NETWORK",
                description = "Root-owned process has active network connections",
                weight      = 25,
            ))

        # ── Rule 8: Deleted/missing executable on disk ────────────────────────
        if proc.exe and "(deleted)" in proc.exe:
            signals.append(ThreatSignal(
                rule        = "DELETED_EXE",
                description = "Process executable has been deleted from disk (fileless malware pattern)",
                weight      = 85,
            ))

        # ── Rule 9: Shell spawned by web server / database process ────────────
        parent_suspicious = any(
            kw in (proc.user or "") or kw in cmdline_lower
            for kw in ["www-data", "apache", "nginx", "mysql", "postgres", "nobody"]
        )
        if parent_suspicious and any(sh in name_lower for sh in ["sh", "bash", "zsh", "python", "perl", "ruby"]):
            signals.append(ThreatSignal(
                rule        = "WEBSHELL_PATTERN",
                description = "Shell/interpreter spawned under web-server/database user — possible web shell",
                weight      = 75,
            ))

        # ── Rule 10: base64/obfuscated arguments ─────────────────────────────
        b64_chunks = re.findall(r'[A-Za-z0-9+/]{60,}={0,2}', proc.cmdline)
        if b64_chunks:
            signals.append(ThreatSignal(
                rule        = "BASE64_ARGS",
                description = f"Long base64-like string in arguments — possible obfuscation ({len(b64_chunks)} chunk(s))",
                weight      = 55,
            ))

        # ── Compute confidence ─────────────────────────────────────────────────
        # Confidence = min(100, sum of all signal weights), but we cap each
        # individual rule's contribution to avoid a single weak rule dominating.
        # We also prevent double-counting same rule ID.
        seen_rules: dict[str, int] = {}
        for sig in signals:
            seen_rules[sig.rule] = max(seen_rules.get(sig.rule, 0), sig.weight)

        raw_score = sum(seen_rules.values())
        confidence = min(100, raw_score)

        # ── Verdict ────────────────────────────────────────────────────────────
        # Guard: never terminate our own PID or PID 1 (init)
        safe_pids = {os.getpid(), 1}
        is_safe_pid = proc.pid in safe_pids

        if not signals or confidence < params.report_threshold:
            verdict = "CLEAN"
            action  = "No action — below report threshold"
        elif confidence >= params.terminate_threshold and not is_safe_pid:
            verdict = "TERMINATED"
            action  = (
                f"{'[DRY-RUN] Would terminate' if False else 'Terminated'} "
                f"PID {proc.pid} (confidence {confidence}% ≥ {params.terminate_threshold}%)"
            )
        else:
            verdict = "REPORTED"
            action  = (
                f"Flagged for human review — confidence {confidence}% "
                f"(threshold {params.terminate_threshold}%)"
            )
            if is_safe_pid and confidence >= params.terminate_threshold:
                action += " [safe PID — skipped termination]"

        return ThreatReport(
            process    = proc,
            signals    = signals,
            confidence = confidence,
            verdict    = verdict,
            action_taken = action,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # PROCESS TERMINATION
    # ──────────────────────────────────────────────────────────────────────────

    async def _terminate_process(self, pid: int) -> None:
        """Graceful SIGTERM → wait 3 s → SIGKILL if still alive."""
        try:
            os.kill(pid, signal.SIGTERM)
            await asyncio.sleep(3)
            # Check if still alive
            try:
                os.kill(pid, 0)  # signal 0 = existence check
                os.kill(pid, signal.SIGKILL)
                print(f"    ⚡  SIGKILL sent to PID {pid} (did not exit on SIGTERM)")
            except ProcessLookupError:
                print(f"    ✅  PID {pid} exited cleanly after SIGTERM")
        except ProcessLookupError:
            print(f"    ℹ️   PID {pid} already gone")
        except PermissionError:
            print(f"    ⛔  Permission denied to kill PID {pid} — needs root/sudo")

    # ──────────────────────────────────────────────────────────────────────────
    # OS HARDENING
    # ──────────────────────────────────────────────────────────────────────────

    async def _harden_os(
        self, params: OSSecurityMonitorParams, os_name: str
    ) -> list[HardeningAction]:
        actions: list[HardeningAction] = []
        enabled = set(params.harden_categories) if params.harden_categories else None

        def want(cat: str) -> bool:
            return enabled is None or cat in enabled

        is_linux = os_name == "Linux"
        is_root  = (os.geteuid() == 0) if hasattr(os, "geteuid") else False

        # ── 1. Firewall ───────────────────────────────────────────────────────
        if want("firewall") and is_linux:
            actions += await self._harden_firewall(params.dry_run, is_root)

        # ── 2. Sysctl kernel hardening ────────────────────────────────────────
        if want("sysctl") and is_linux:
            actions += await self._harden_sysctl(params.dry_run, is_root)

        # ── 3. SSH hardening ──────────────────────────────────────────────────
        if want("ssh"):
            actions += await self._harden_ssh(params.dry_run, is_root, is_linux)

        # ── 4. Filesystem permissions ─────────────────────────────────────────
        if want("filesystem") and is_linux:
            actions += await self._harden_filesystem(params.dry_run, is_root)

        # ── 5. Disable unused services ────────────────────────────────────────
        if want("services") and is_linux:
            actions += await self._harden_services(params.dry_run, is_root)

        # ── 6. Account hardening ──────────────────────────────────────────────
        if want("accounts") and is_linux:
            actions += await self._harden_accounts(params.dry_run, is_root)

        # ── 7. auditd ─────────────────────────────────────────────────────────
        if want("auditd") and is_linux:
            actions += await self._harden_auditd(params.dry_run, is_root)

        # ── 8. AppArmor ───────────────────────────────────────────────────────
        if want("apparmor") and is_linux:
            actions += await self._harden_apparmor(params.dry_run, is_root)

        return actions

    # ── Firewall ──────────────────────────────────────────────────────────────

    async def _harden_firewall(self, dry: bool, root: bool) -> list[HardeningAction]:
        acts: list[HardeningAction] = []

        # Prefer ufw, fall back to iptables
        if shutil.which("ufw"):
            rules = [
                ("ufw default deny incoming",   "Set default policy: deny all inbound"),
                ("ufw default allow outgoing",  "Set default policy: allow all outbound"),
                ("ufw allow ssh",               "Allow SSH (port 22)"),
                ("ufw allow 80/tcp",            "Allow HTTP"),
                ("ufw allow 443/tcp",           "Allow HTTPS"),
                ("ufw --force enable",          "Enable UFW firewall"),
                ("ufw logging on",              "Enable UFW logging"),
            ]
            for cmd, title in rules:
                acts.append(await self._run_hardening(
                    "firewall", title, cmd, dry, root,
                    need_root_msg="UFW commands require root"
                ))

        elif shutil.which("iptables"):
            rules = [
                ("iptables -P INPUT DROP",                          "Default INPUT policy: DROP"),
                ("iptables -P FORWARD DROP",                        "Default FORWARD policy: DROP"),
                ("iptables -P OUTPUT ACCEPT",                       "Default OUTPUT policy: ACCEPT"),
                ("iptables -A INPUT -i lo -j ACCEPT",               "Allow loopback"),
                ("iptables -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT",
                 "Allow established connections"),
                ("iptables -A INPUT -p tcp --dport 22 -j ACCEPT",  "Allow SSH"),
                ("iptables -A INPUT -p tcp --dport 80 -j ACCEPT",  "Allow HTTP"),
                ("iptables -A INPUT -p tcp --dport 443 -j ACCEPT", "Allow HTTPS"),
                ("iptables -A INPUT -p icmp -j ACCEPT",            "Allow ICMP ping"),
            ]
            for cmd, title in rules:
                acts.append(await self._run_hardening("firewall", title, cmd, dry, root))
        else:
            acts.append(HardeningAction(
                category="firewall", title="Install firewall",
                command="apt install ufw -y", output="", success=False,
                skipped=True, skip_reason="Neither ufw nor iptables found"
            ))

        return acts

    # ── sysctl ────────────────────────────────────────────────────────────────

    async def _harden_sysctl(self, dry: bool, root: bool) -> list[HardeningAction]:
        """Apply kernel hardening parameters via sysctl."""
        params_map = {
            # Network
            "net.ipv4.conf.all.rp_filter=1":                    "Reverse path filtering (anti-spoofing)",
            "net.ipv4.conf.default.rp_filter=1":                "RP filter on new interfaces",
            "net.ipv4.conf.all.accept_redirects=0":             "Ignore ICMP redirects (all)",
            "net.ipv4.conf.default.accept_redirects=0":         "Ignore ICMP redirects (default)",
            "net.ipv4.conf.all.secure_redirects=0":             "Ignore secure ICMP redirects",
            "net.ipv4.conf.all.send_redirects=0":               "Disable sending ICMP redirects",
            "net.ipv4.conf.all.accept_source_route=0":          "Disable source routing",
            "net.ipv4.conf.all.log_martians=1":                 "Log martian packets",
            "net.ipv4.icmp_echo_ignore_broadcasts=1":           "Ignore broadcast pings (Smurf protection)",
            "net.ipv4.icmp_ignore_bogus_error_responses=1":     "Ignore bogus ICMP errors",
            "net.ipv4.tcp_syncookies=1":                        "Enable SYN cookies (SYN flood protection)",
            "net.ipv4.ip_forward=0":                            "Disable IP forwarding (not a router)",
            "net.ipv6.conf.all.accept_redirects=0":             "Ignore IPv6 ICMP redirects",
            "net.ipv6.conf.all.accept_source_route=0":          "Disable IPv6 source routing",
            # Kernel
            "kernel.randomize_va_space=2":                      "Full ASLR (address space layout randomisation)",
            "kernel.dmesg_restrict=1":                          "Restrict dmesg to root",
            "kernel.kptr_restrict=2":                           "Hide kernel pointer addresses",
            "kernel.sysrq=0":                                   "Disable magic SysRq key",
            "kernel.core_uses_pid=1":                           "Append PID to core dump names",
            "kernel.yama.ptrace_scope=1":                       "Restrict ptrace to parent processes",
            # File system
            "fs.suid_dumpable=0":                               "No core dumps for SUID programs",
            "fs.protected_hardlinks=1":                         "Protect against hardlink attacks",
            "fs.protected_symlinks=1":                          "Protect against symlink attacks",
        }

        acts: list[HardeningAction] = []
        for kv, desc in params_map.items():
            cmd = f"sysctl -w {kv}"
            acts.append(await self._run_hardening("sysctl", desc, cmd, dry, root))

        # Persist to /etc/sysctl.d/
        persist_content = "\n".join(
            f"# {desc}\n{kv.replace('=', ' = ')}" for kv, desc in params_map.items()
        )
        persist_cmd = (
            f"echo '{persist_content}' > /etc/sysctl.d/99-nexus-hardening.conf "
            f"&& sysctl -p /etc/sysctl.d/99-nexus-hardening.conf"
        )
        acts.append(await self._run_hardening(
            "sysctl", "Persist sysctl settings to /etc/sysctl.d/99-nexus-hardening.conf",
            persist_cmd, dry, root
        ))

        return acts

    # ── SSH ───────────────────────────────────────────────────────────────────

    async def _harden_ssh(self, dry: bool, root: bool, is_linux: bool) -> list[HardeningAction]:
        acts: list[HardeningAction] = []

        sshd_cfg = Path("/etc/ssh/sshd_config")
        if not sshd_cfg.exists():
            return [HardeningAction(
                category="ssh", title="SSH config",
                command="", output="", success=False,
                skipped=True, skip_reason="/etc/ssh/sshd_config not found"
            )]

        settings = {
            "PermitRootLogin":           "no",
            "PasswordAuthentication":    "no",
            "PermitEmptyPasswords":      "no",
            "X11Forwarding":             "no",
            "MaxAuthTries":              "3",
            "LoginGraceTime":            "30",
            "AllowAgentForwarding":      "no",
            "AllowTcpForwarding":        "no",
            "Protocol":                  "2",
            "ClientAliveInterval":       "300",
            "ClientAliveCountMax":        "2",
            "UsePAM":                    "yes",
            "IgnoreRhosts":              "yes",
            "HostbasedAuthentication":   "no",
            "Banner":                    "/etc/issue.net",
        }

        for setting, value in settings.items():
            # Use sed to set/replace the value in sshd_config
            cmd = (
                f"grep -q '^{setting}' /etc/ssh/sshd_config "
                f"&& sed -i 's/^{setting}.*/{setting} {value}/' /etc/ssh/sshd_config "
                f"|| echo '{setting} {value}' >> /etc/ssh/sshd_config"
            )
            acts.append(await self._run_hardening(
                "ssh", f"SSH: set {setting} {value}", cmd, dry, root
            ))

        # Reload sshd
        acts.append(await self._run_hardening(
            "ssh", "Reload SSH daemon to apply changes",
            "systemctl reload sshd || service sshd reload",
            dry, root
        ))

        # SSH login banner
        banner_cmd = (
            "echo 'Authorised access only. All activity is monitored and logged.' "
            "> /etc/issue.net"
        )
        acts.append(await self._run_hardening(
            "ssh", "Set SSH login warning banner", banner_cmd, dry, root
        ))

        return acts

    # ── Filesystem ────────────────────────────────────────────────────────────

    async def _harden_filesystem(self, dry: bool, root: bool) -> list[HardeningAction]:
        acts: list[HardeningAction] = []

        checks = [
            # (title, command)
            ("Restrict /tmp permissions",
             "chmod 1777 /tmp && chmod 1777 /var/tmp"),

            ("Remove world-writable files in /etc (report only)",
             "find /etc -maxdepth 2 -perm -o+w -ls 2>/dev/null | head -20"),

            ("Find SUID/SGID binaries",
             "find / -perm /6000 -type f 2>/dev/null | grep -v proc | head -30"),

            ("Remove unnecessary SUID from common tools",
             "for f in /usr/bin/wall /usr/bin/newgrp /usr/bin/chsh /usr/bin/chfn; "
             "do [ -f $f ] && chmod u-s $f 2>/dev/null && echo 'Removed SUID: '$f; done"),

            ("Lock /etc/passwd against unauthorised writes",
             "chmod 644 /etc/passwd && chown root:root /etc/passwd"),

            ("Lock /etc/shadow",
             "chmod 640 /etc/shadow && chown root:shadow /etc/shadow 2>/dev/null "
             "|| chmod 600 /etc/shadow"),

            ("Lock /etc/group",
             "chmod 644 /etc/group && chown root:root /etc/group"),

            ("Disable noexec on /tmp if supported (fstab line)",
             "grep -q '/tmp' /etc/fstab && echo 'Review /tmp mount options — add noexec,nosuid,nodev' "
             "|| echo '/tmp not separately mounted — consider adding to fstab'"),

            ("Set sticky bit on /tmp",
             "chmod +t /tmp"),
        ]

        for title, cmd in checks:
            acts.append(await self._run_hardening("filesystem", title, cmd, dry, root))

        return acts

    # ── Services ──────────────────────────────────────────────────────────────

    async def _harden_services(self, dry: bool, root: bool) -> list[HardeningAction]:
        acts: list[HardeningAction] = []

        # Services that are rarely needed on a hardened server
        unnecessary = [
            "telnet",   "rsh",       "rlogin",  "rexec",
            "tftp",     "talk",      "ntalk",   "xinetd",
            "avahi-daemon", "cups",  "nfs",     "rpcbind",
            "dovecot",  "sendmail",  "postfix",
        ]

        for svc in unnecessary:
            cmd = (
                f"systemctl is-active --quiet {svc} 2>/dev/null && "
                f"systemctl stop {svc} && systemctl disable {svc} && "
                f"echo 'Stopped and disabled: {svc}' || echo 'Not running: {svc}'"
            )
            acts.append(await self._run_hardening(
                "services", f"Disable {svc} if running", cmd, dry, root
            ))

        return acts

    # ── Accounts ──────────────────────────────────────────────────────────────

    async def _harden_accounts(self, dry: bool, root: bool) -> list[HardeningAction]:
        acts: list[HardeningAction] = []

        checks = [
            ("List accounts with empty passwords (manual review)",
             "awk -F: '($2==\"\" || $2==\"*\") {print $1}' /etc/shadow 2>/dev/null | head -20"),

            ("Lock system accounts that should not log in",
             "for u in daemon bin sys sync games man lp mail news uucp proxy www-data backup list irc gnats nobody; "
             "do id $u >/dev/null 2>&1 && passwd -l $u 2>/dev/null && echo 'Locked: '$u; done"),

            ("Set password max age (90 days)",
             "sed -i 's/^PASS_MAX_DAYS.*/PASS_MAX_DAYS   90/' /etc/login.defs 2>/dev/null "
             "|| echo 'login.defs not found'"),

            ("Set password min age (1 day)",
             "sed -i 's/^PASS_MIN_DAYS.*/PASS_MIN_DAYS   1/' /etc/login.defs 2>/dev/null"),

            ("Set password warning days (14)",
             "sed -i 's/^PASS_WARN_AGE.*/PASS_WARN_AGE   14/' /etc/login.defs 2>/dev/null"),

            ("Set default umask to 027",
             "grep -q 'umask 027' /etc/profile || echo 'umask 027' >> /etc/profile"),

            ("Disable core dumps for all users",
             "echo '* hard core 0' >> /etc/security/limits.conf"),

            ("Find UID=0 accounts that are not root (critical)",
             "awk -F: '($3==0 && $1!=\"root\") {print $1}' /etc/passwd"),
        ]

        for title, cmd in checks:
            acts.append(await self._run_hardening("accounts", title, cmd, dry, root))

        return acts

    # ── auditd ────────────────────────────────────────────────────────────────

    async def _harden_auditd(self, dry: bool, root: bool) -> list[HardeningAction]:
        acts: list[HardeningAction] = []

        # Install if missing
        acts.append(await self._run_hardening(
            "auditd", "Install auditd if not present",
            "which auditd >/dev/null 2>&1 || (apt-get install -y auditd 2>/dev/null || yum install -y audit 2>/dev/null)",
            dry, root
        ))

        audit_rules = [
            ("-w /etc/passwd -p wa -k identity",         "Monitor /etc/passwd changes"),
            ("-w /etc/shadow -p wa -k identity",         "Monitor /etc/shadow changes"),
            ("-w /etc/group  -p wa -k identity",         "Monitor /etc/group changes"),
            ("-w /etc/sudoers -p wa -k sudoers",         "Monitor sudoers changes"),
            ("-w /var/log/auth.log -p wa -k auth_log",   "Monitor auth log"),
            ("-w /bin/su -p x -k priv_esc",              "Monitor su execution"),
            ("-w /usr/bin/sudo -p x -k priv_esc",        "Monitor sudo execution"),
            ("-w /sbin/insmod -p x -k kernel_modules",   "Monitor kernel module loading"),
            ("-w /sbin/rmmod -p x -k kernel_modules",    "Monitor kernel module removal"),
            ("-a always,exit -F arch=b64 -S execve -k exec", "Audit all execve calls (64-bit)"),
            ("-a always,exit -F arch=b32 -S execve -k exec", "Audit all execve calls (32-bit)"),
            ("-a always,exit -F arch=b64 -S open -F dir=/etc -k etc_access",
             "Audit /etc file opens"),
            ("-e 2",                                      "Make audit rules immutable until reboot"),
        ]

        for rule, desc in audit_rules:
            cmd = f"auditctl {rule} 2>/dev/null || echo 'auditctl not available'"
            acts.append(await self._run_hardening("auditd", desc, cmd, dry, root))

        acts.append(await self._run_hardening(
            "auditd", "Enable and start auditd",
            "systemctl enable auditd && systemctl start auditd",
            dry, root
        ))

        return acts

    # ── AppArmor ──────────────────────────────────────────────────────────────

    async def _harden_apparmor(self, dry: bool, root: bool) -> list[HardeningAction]:
        acts: list[HardeningAction] = []

        acts.append(await self._run_hardening(
            "apparmor", "Install AppArmor if not present",
            "which apparmor_status >/dev/null 2>&1 || apt-get install -y apparmor apparmor-utils 2>/dev/null",
            dry, root
        ))

        steps = [
            ("Enable AppArmor service",
             "systemctl enable apparmor && systemctl start apparmor"),
            ("Set all profiles to enforce mode",
             "aa-enforce /etc/apparmor.d/* 2>/dev/null || echo 'No profiles to enforce'"),
            ("Show AppArmor status",
             "apparmor_status 2>/dev/null || echo 'AppArmor status unavailable'"),
        ]

        for title, cmd in steps:
            acts.append(await self._run_hardening("apparmor", title, cmd, dry, root))

        return acts

    # ── Generic command runner ─────────────────────────────────────────────────

    async def _run_hardening(
        self,
        category: str,
        title: str,
        cmd: str,
        dry: bool,
        root: bool,
        need_root_msg: str = "This command requires root privileges"
    ) -> HardeningAction:
        if dry:
            return HardeningAction(
                category=category, title=title, command=cmd,
                output="[DRY-RUN] would execute", success=True,
                skipped=True, skip_reason="dry_run=True"
            )

        if not root and any(
            priv_cmd in cmd
            for priv_cmd in ["sysctl", "iptables", "ufw", "systemctl", "sed -i /etc",
                              "chmod 6", "chown root", "auditctl", "aa-enforce",
                              "passwd -l", "useradd", "userdel"]
        ):
            return HardeningAction(
                category=category, title=title, command=cmd,
                output="", success=False,
                skipped=True, skip_reason=f"Insufficient privileges — {need_root_msg}"
            )

        try:
            result = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(result.communicate(), timeout=30)
            output = stdout.decode("utf-8", errors="replace").strip()
            success = result.returncode == 0
            print(f"    {'✅' if success else '⚠️ '} {title[:60]}")
            return HardeningAction(
                category=category, title=title, command=cmd,
                output=output[:400], success=success
            )
        except asyncio.TimeoutError:
            return HardeningAction(
                category=category, title=title, command=cmd,
                output="Timed out after 30s", success=False
            )
        except Exception as exc:
            return HardeningAction(
                category=category, title=title, command=cmd,
                output=str(exc), success=False
            )

    # ──────────────────────────────────────────────────────────────────────────
    # HELPERS
    # ──────────────────────────────────────────────────────────────────────────

    def _current_user(self) -> str:
        try:
            import pwd
            return pwd.getpwuid(os.getuid()).pw_name
        except Exception:
            return os.environ.get("USER", "unknown")

    # ──────────────────────────────────────────────────────────────────────────
    # REPORT
    # ──────────────────────────────────────────────────────────────────────────

    def _to_dict(
        self,
        params: OSSecurityMonitorParams,
        threats: list[ThreatReport],
        hardening: list[HardeningAction],
        os_name: str,
    ) -> dict:
        return {
            "generated":  datetime.utcnow().isoformat() + "Z",
            "os":         os_name,
            "hostname":   platform.node(),
            "dry_run":    params.dry_run,
            "thresholds": {
                "terminate": params.terminate_threshold,
                "report":    params.report_threshold,
            },
            "threats": [
                {
                    "pid":          r.process.pid,
                    "name":         r.process.name,
                    "exe":          r.process.exe,
                    "cmdline":      r.process.cmdline[:300],
                    "user":         r.process.user,
                    "confidence":   r.confidence,
                    "verdict":      r.verdict,
                    "action":       r.action_taken,
                    "signals":      [{"rule": s.rule, "desc": s.description, "weight": s.weight}
                                     for s in r.signals],
                    "timestamp":    r.timestamp,
                }
                for r in threats
            ],
            "hardening": [
                {
                    "category": h.category,
                    "title":    h.title,
                    "command":  h.command,
                    "output":   h.output,
                    "success":  h.success,
                    "skipped":  h.skipped,
                }
                for h in hardening
            ],
        }

    def _build_report(
        self,
        params: OSSecurityMonitorParams,
        threats: list[ThreatReport],
        hardening: list[HardeningAction],
        os_name: str,
    ) -> str:
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        verdict_emoji = {
            "TERMINATED": "🔴",
            "REPORTED":   "🟠",
            "MONITORED":  "🟡",
            "CLEAN":      "✅",
        }
        sev_by_conf = lambda c: (
            "CRITICAL" if c >= 90 else
            "HIGH"     if c >= 70 else
            "MEDIUM"   if c >= 50 else
            "LOW"
        )

        terminated = [t for t in threats if t.verdict == "TERMINATED"]
        reported   = [t for t in threats if t.verdict == "REPORTED"]

        h_ok  = [h for h in hardening if h.success and not h.skipped]
        h_skp = [h for h in hardening if h.skipped]
        h_err = [h for h in hardening if not h.success and not h.skipped]

        lines: list[str] = []

        # ── Cover ──────────────────────────────────────────────────────────────
        lines += [
            "# 🛡️ OS Security Monitor Report",
            "",
            f"| Field | Value |",
            f"|---|---|",
            f"| **Hostname** | `{platform.node()}` |",
            f"| **OS** | {os_name} {platform.release()} |",
            f"| **Generated** | {now} |",
            f"| **Mode** | {params.mode.upper()} |",
            f"| **Dry Run** | {'Yes — no changes made' if params.dry_run else 'No — changes applied'} |",
            f"| **Terminate Threshold** | ≥ {params.terminate_threshold}% confidence |",
            f"| **Report Threshold** | ≥ {params.report_threshold}% confidence |",
            "",
            "---",
            "",
            "## 📊 Executive Summary",
            "",
            "### Process Threats",
            "",
            f"| Verdict | Count |",
            f"|---|---|",
            f"| 🔴 TERMINATED | {len(terminated)} |",
            f"| 🟠 REPORTED (suspicious) | {len(reported)} |",
            f"| ✅ Clean (below threshold) | — |",
            "",
            "### Hardening",
            "",
            f"| Status | Count |",
            f"|---|---|",
            f"| ✅ Applied | {len(h_ok)} |",
            f"| ⚪ Skipped | {len(h_skp)} |",
            f"| ❌ Failed | {len(h_err)} |",
            "",
            "---",
        ]

        # ── Threat section ─────────────────────────────────────────────────────
        if threats:
            lines += ["", "## 🚨 Threat Findings", ""]

            for group, label in [(terminated, "TERMINATED"), (reported, "REPORTED")]:
                if not group:
                    continue
                lines += [f"### {verdict_emoji[label]} {label} ({len(group)})", ""]
                for idx, t in enumerate(group, 1):
                    p = t.process
                    lines += [
                        f"#### {idx}. PID {p.pid} — `{p.name}`",
                        "",
                        f"| Attribute | Value |",
                        f"|---|---|",
                        f"| **Verdict** | {verdict_emoji[label]} {t.verdict} |",
                        f"| **Confidence** | {t.confidence}% ({sev_by_conf(t.confidence)}) |",
                        f"| **Action** | {t.action_taken} |",
                        f"| **PID** | {p.pid} |",
                        f"| **Name** | `{p.name}` |",
                        f"| **Executable** | `{p.exe or 'unknown'}` |",
                        f"| **User** | {p.user} |",
                        f"| **CPU** | {p.cpu_percent:.1f}% |",
                        f"| **Memory** | {p.mem_percent:.1f}% |",
                        f"| **Status** | {p.status} |",
                        f"| **Parent PID** | {p.ppid} |",
                        f"| **Detected** | {t.timestamp} |",
                        "",
                        "**Command Line**",
                        "",
                        "```",
                        (p.cmdline[:500] or "(empty)"),
                        "```",
                        "",
                        "**Network Connections**",
                        "",
                        "```",
                        ("\n".join(p.connections[:10]) if p.connections else "(none)"),
                        "```",
                        "",
                        "**Detection Signals**",
                        "",
                        f"| Rule | Weight | Description |",
                        f"|---|---|---|",
                    ]
                    for sig in sorted(t.signals, key=lambda s: s.weight, reverse=True):
                        lines.append(f"| `{sig.rule}` | {sig.weight} | {sig.description} |")

                    lines += ["", "---", ""]
        else:
            lines += [
                "",
                "## ✅ Process Monitoring",
                "",
                "No suspicious processes detected above the reporting threshold.",
                "",
                "---",
            ]

        # ── Hardening section ──────────────────────────────────────────────────
        if hardening:
            lines += ["", "## 🔒 Hardening Actions", ""]

            # Group by category
            categories: dict[str, list[HardeningAction]] = {}
            for h in hardening:
                categories.setdefault(h.category, []).append(h)

            cat_icons = {
                "firewall":   "🔥",
                "sysctl":     "⚙️",
                "ssh":        "🔑",
                "filesystem": "📂",
                "services":   "🔧",
                "accounts":   "👤",
                "auditd":     "📋",
                "apparmor":   "🛡️",
            }

            for cat, acts in categories.items():
                icon = cat_icons.get(cat, "🔹")
                ok  = sum(1 for a in acts if a.success and not a.skipped)
                skp = sum(1 for a in acts if a.skipped)
                err = sum(1 for a in acts if not a.success and not a.skipped)

                lines += [
                    f"### {icon} {cat.upper()} — {ok} applied / {skp} skipped / {err} failed",
                    "",
                    f"| Status | Title | Output |",
                    f"|---|---|---|",
                ]
                for a in acts:
                    if a.skipped:
                        status = f"⚪ SKIPPED ({a.skip_reason})"
                    elif a.success:
                        status = "✅ OK"
                    else:
                        status = "❌ FAIL"

                    out_snippet = (a.output or "").replace("\n", " ")[:120]
                    lines.append(f"| {status} | {a.title} | `{out_snippet}` |")

                lines.append("")

        # ── Detection rules reference ──────────────────────────────────────────
        lines += [
            "---",
            "",
            "## 📖 Detection Rules Reference",
            "",
            "| Rule ID | Description | Base Weight |",
            "|---|---|---|",
            "| `KNOWN_BAD_NAME` | Process name matches known attack tool | 70 |",
            "| `SUSPICIOUS_CMDLINE` | Command line contains reverse-shell / dropper pattern | 55–95 |",
            "| `EXEC_FROM_TMP` | Binary executing from world-writable directory | 70 |",
            "| `HIDDEN_EXE` | Executable has hidden or space-prefixed name | 80 |",
            "| `SUSPICIOUS_PORT` | Listening on known-bad port | 65 |",
            "| `HIGH_CPU_MINER` | CPU > 95% — consistent with crypto miner | 60 |",
            "| `HIGH_CPU` | CPU > 85% — elevated but not conclusive | 30 |",
            "| `ROOT_NETWORK` | Root process with active network connections | 25 |",
            "| `DELETED_EXE` | Executable deleted from disk (fileless pattern) | 85 |",
            "| `WEBSHELL_PATTERN` | Shell spawned under web-server user | 75 |",
            "| `BASE64_ARGS` | Long base64-like string in arguments | 55 |",
            "",
            "> **Confidence = min(100, sum of signal weights)**  ",
            f"> Terminate threshold: **{params.terminate_threshold}%** | Report threshold: **{params.report_threshold}%**",
            "",
            "---",
            "",
            "## ⚠️ Disclaimer",
            "",
            "> This tool performs **read-only process inspection** and targeted hardening commands. "
            "Hardening changes may affect running services — review the output carefully. "
            "Process termination only occurs when confidence is ≥ the configured threshold. "
            "Always run with `dry_run=True` first on production systems.",
            "",
        ]

        return "\n".join(lines)
