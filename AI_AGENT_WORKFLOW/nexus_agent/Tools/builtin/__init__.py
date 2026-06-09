from nexus_agent.Tools.builtin.edit_file import EditTool
from nexus_agent.Tools.builtin.glob import GlobTool
from nexus_agent.Tools.builtin.grep import GrepTool
from nexus_agent.Tools.builtin.list_dir import ListDirTool
from nexus_agent.Tools.builtin.memory import MemoryTool
from nexus_agent.Tools.builtin.rag import RAGTool
from nexus_agent.Tools.builtin.read_file import ReadFileTool
from nexus_agent.Tools.builtin.shell import ShellTool
from nexus_agent.Tools.builtin.todo import TodosTool
from nexus_agent.Tools.builtin.web_fetch import WebFetchTool
from nexus_agent.Tools.builtin.web_search import WebSearchTool
from nexus_agent.Tools.builtin.write_file import WriteFileTool
from nexus_agent.Tools.builtin.browerser import BrowserTool
from nexus_agent.Tools.builtin.domain_osint import DomainOSINTTool
from nexus_agent.Tools.builtin.email_osint import EmailOSINTTool
from nexus_agent.Tools.builtin.git import GitTool
from nexus_agent.Tools.builtin.ip_osint import IPOSINTTool
from nexus_agent.Tools.builtin.username_osint import UsernameOSINTTool
from nexus_agent.Tools.builtin.phone_osint import PhoneOSINTTool
from nexus_agent.Tools.builtin.video_generate import VideoCreatorTool
from nexus_agent.Tools.builtin.whois_osint import WHOISOSINTTool
from nexus_agent.Tools.builtin.censys_osint import CensysOSINTTool
from nexus_agent.Tools.builtin.web_security_audit import WebSecurityAuditTool
from nexus_agent.Tools.builtin.os_security_monitor import OSSecurityMonitorTool
from nexus_agent.Tools.builtin.report_generator import ReportGeneratorTool
from nexus_agent.Tools.builtin.software_architect import SoftwareArchitectTool

__all__ = ["ReadFileTool","WriteFileTool","EditTool","ShellTool","ListDirTool","GrepTool","GlobTool","WebSearchTool","WebFetchTool","TodosTool","MemoryTool","RagTool","BrowserTool","DomainOSINTTool","EmailOSINTTool","GitTool","IPOSINTTool","UsernameOSINTTool","PhoneOSINTTool","VideoCreatorTool","WHOISOSINTTool","CensysOSINTTool","WebSecurityAuditTool","OSSecurityMonitorTool","ReportGeneratorTool","SoftwareArchitectTool"]

def get_all_builtin_tools() -> list[type]:
    return [
        ReadFileTool,
        WriteFileTool,
        EditTool,
        ShellTool,
        ListDirTool,
        GrepTool,
        GlobTool,
        WebSearchTool,
        WebFetchTool,
        TodosTool,
        MemoryTool,
        RAGTool,
        BrowserTool,
        DomainOSINTTool,
        EmailOSINTTool,
        GitTool,
        IPOSINTTool,
        UsernameOSINTTool,
        PhoneOSINTTool,
        VideoCreatorTool,
        WHOISOSINTTool,
        CensysOSINTTool,
        WebSecurityAuditTool,
        OSSecurityMonitorTool,
        ReportGeneratorTool,
        SoftwareArchitectTool,
        #now whenever we want to add a tool will register it here
    ]