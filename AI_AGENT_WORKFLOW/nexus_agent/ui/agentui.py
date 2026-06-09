from pathlib import Path
from typing import Any
from nexus_agent.Tools.base import ToolConfirmation
from rich.theme import Theme
from rich.console import Console
from rich.rule import Rule
from rich.text import Text
from rich.panel import Panel
from rich.table import Table
from rich import box
from rich.console import Group
from rich.prompt import Prompt
from nexus_agent.config.config import Config
from rich.markdown import Markdown
from nexus_agent.utils.path import display_path_to_cwd
import re
from rich.syntax import Syntax
from nexus_agent.utils.text import truncate_text


AGENT_THEME = Theme(
    {
    # Core semantic roles
    "info": "cyan",
    "warning": "yellow",
    "error": "bold red",
    "success": "bold green",
    "muted": "grey50",

    # Agent roles
    "agent": "bold magenta",
    "system": "dim cyan",
    "user": "bright_blue bold",
    "code": "white",
    "tool.read": "cyan",
    "tool": "bright_magenta bold",
    "tool.write": "yellow",
    "tool.shell": "magenta",
    "token": "green",
    "tool.memory": "green",
    "tool.mcp": "bright_cyan",
    "tool.network": "bright_blue",
    "token.dim": "dim green",
    "header": "bold blue",
    "border": "cyan",
    "panel.title": "bold cyan",
    "assistant": "bright_white",

    # Debug / logs
    "debug": "dim",
    "trace": "dim italic",

    # Special states
    "thinking": "italic yellow",
    "processing": "bold cyan",
    "highlight": "bold magenta",

    # Errors (granular)
    "error.critical": "bold white on red",
    "error.soft": "red",

    # Warnings (granular)
    "warning.soft": "yellow",
    "warning.strong": "bold yellow",

    # Success variations
    "success.soft": "green",
    "success.strong": "bold green",
    }
)

_console: Console | None = None
def get_console() -> Console:
    global _console
    if _console is None:
        _console = Console(theme=AGENT_THEME,highlight=False)
    return _console



class TUI:
    def __init__(self,config:Config, console: Console | None= None) -> None:
        self.console = console or get_console()
        self._assistant_stream_open = False
        self._tool_args_call_id: dict[str,dict[str, Any]] = {}
        self.config = config
        self.cwd = self.config.cwd
        self._max_block_tokens = 3400
    def begin_assistant(self) -> None:
        self.console.print(Rule(Text("Assistant",style='assistant')))
        self._assistant_stream_open = True
    def end_assistant(self) -> None:
        self.console.print()
        self._assistant_stream_open = False

    def stream_assistant_delta(self, content: str) -> None:
        self.console.print(content,end="",markup=False)
        
    def _ordered_args(self,tool_name:str, args: dict[str,Any]) -> list[tuple[str,Any]]:
        _PREFERED_ORDER = {
            'read_file': ['path', 'offset', 'limit'],
            'write_file':['path','create_directories','content'],
            'edit':['path','replace_all','old_string','new_string'],
            'shell':['command','timeout','cwd'],
            'list_dir':['path','include_hidden'],
            'grep':['path','case_insensitive','pattern'],
            'glob':['path','pattern'],
            'todos':['id','action','content'],
            'memory':['action','key','value'],
            # Add more tools and their preferred argument orders here
        }
        prefered = _PREFERED_ORDER.get(tool_name, [])
        ordered: list[tuple[str,Any]] = [] # meaning str is path and Any is the value of path, and it is a list of tuples because we want to maintain order, and we will add the rest of the args after the prefered ones
        seen = set() # to keep track of which args we have already added to the ordered list
        for key in prefered:#goes over every list in like path,offset,limit and if they are in args, it yields them first
            if key in args:
                ordered.append((key,args[key]))
                seen.add(key)
        remaining_keys = set(args.keys() - seen) # the rest of the keys that are not in the prefered list
        ordered.extend((key, args[key]) for key in remaining_keys) # add the rest of the args to the ordered list
        return ordered
    
    
    def _render_args_table(self,tool_name:str, args: dict[str,Any]) -> Table:
        table = Table.grid(padding=(0,1))
        table.add_column(style="muted",justify="right", no_wrap=True)
        table.add_column(style="code",overflow="fold")
        for key, value in self._ordered_args(tool_name,args):
            if isinstance(value, str):
                if key in {'content','old_string','new_string'}:
                    line_count = len(value.splitlines()) or 0
                    byte_count = len(value.encode('utf-8',errors='replace'))
                    value = f"<{line_count} lines * {byte_count} bytes>"
                if isinstance(value, bool):
                    value = str(value)
            table.add_row(key, str(value))
        return table

    def tool_call_start(self,call_id: str, name: str, tool_kind: str | None, arguments: dict[str,Any]) -> None:
        self._tool_args_call_id[call_id] = arguments
        border_style = f"tool.{tool_kind}" if tool_kind else "tool"
        title = Text.assemble(
            {" • ", "muted"},
            {name, tool_kind if tool_kind else "tool"},
            {" ‣ ", "muted"},
            {f"#{call_id[:8]}", "muted"}
        )
        
        display_args = dict(arguments)
        for key in ('path', 'cwd'):
            val = display_args.get(key)
            if isinstance(val, str) and self.cwd:
                display_args[key] = str(display_path_to_cwd(self.cwd, val))
        panel = Panel(
            self._render_args_table(name, display_args) if display_args else Text("No arguments", style="muted"),
            title=title,
            border_style=border_style,
            title_align="left",
            subtitle_align="right",
            subtitle=Text("running", style="muted"),
            box=box.ROUNDED,
            padding=(1,2),
            
        )
        self.console.print()
        self.console.print(panel)
        
    def _extract_read_file_code(self, text:str) -> tuple[int, str] | None:
        body = text
        code_lines: list[str] = []
        start_line: int | None = None
        header_match = re.match(r"^showing lines (\d+)-(\d+) of (\d+)\n\n",text)
        if header_match:
            body = text[header_match.end() :]
            
            
            for line in body.splitlines():
                m = re.match(r"^\s*(\d+)\|(.*)$",line)
                if not m:
                    return None
                line_no = int(m.group(1))
                if start_line is None:
                    start_line = line_no
                code_lines.append(m.group(2))
        if start_line is None:
            return None
        return start_line, "\n".join(code_lines)
    
    def _guess_language(self,path:str | None) -> str:
        if not path:
            return "text"
        suffix = Path(path).suffix.lower()
        return {
            ".py":"python",
            ".js":"javascript",
            ".jsx":"jsx",
            ".ts":"typescript",
            ".tsx":"tsx",
            ".json":"json",
            ".toml":"toml",
            ".yaml":"yaml",
            ".yml":"yml",
            ".md":"markdown",
            ".sh":"bash",
            ".zsh":"bash",
            ".rs":"rust",
            ".go":"go",
            ".java":"java",
            ".kt":"kotlin",
            ".swift":"swift",
            ".h":"C",
            ".c":"C",
            ".cpp":"C++",
            ".hpp":"C++",
            ".xml":"XML",
            ".sql":"SQL",
            ".html":"HTML",
            ".css":"CSS"
        }.get(suffix, "text")
        
    def print_welcome(self,title:str,lines: list[str]) -> None:
        body = "\n".join(lines)
        self.console.print(
            Panel(
                Text(body,style='code'),
                title=Text(title,style="highlight"),
                title_align="left",
                border_style="border",
                box=box.ROUNDED,
                padding=(1,2),
            )
        )
    
    def tool_call_complete(
        self,call_id: str,
        name: str,
        tool_kind: str | None,
        success: bool,
        output: str,
        error: str | None,
        metadata:dict[str,Any] | None,
        diff: str | None,
        truncated:bool,
        exit_code: str | None) -> None:
        border_style = f"tool.{tool_kind}" if tool_kind else "tool"
        status_icon = '^' if success else 'x'
        status_style = 'success' if success else 'error'
        title = Text.assemble(
            {f"{status_icon}", status_style},
            {name, border_style},
            {" ", "muted"},
            {f"#{call_id[:8]}", "muted"}
        )
        args = self._tool_args_call_id.get(call_id,{})
        primary_path = None
        blocks = []
        if isinstance(metadata, dict) and isinstance(metadata.get('path'),str):
            primary_path = metadata.get('path')
        
        if name == "read_file" and success:
            if primary_path:
                result = self._extract_read_file_code(output)
                if result:
                    start_line, code = result
                    shown_start = metadata.get('shown_start')
                    shown_end = metadata.get('shown_end')
                    total_lines = metadata.get('total_lines')
                    pl = self._guess_language(primary_path)
                    
                    header_parts = [display_path_to_cwd(primary_path,self.cwd)]
                    header_parts.append(" * ")
                    if shown_start and shown_end and total_lines:
                        header_parts.append(f"lines {shown_start}-{shown_end} of {total_lines}")
                    
                    header = "".join(header_parts)
                    blocks.append(Text(header, style='muted'))
                    blocks.append(Syntax(
                        code,
                        pl,
                        theme='monokai',
                        line_numbers=True,
                        start_line=start_line,
                        word_wrap=False
                    ))
                else:
            # Fallback if parsing failed
                    output_display = truncate_text(output,"", 240)
                    blocks.append(Syntax(
                         output_display,
                         'text',
                         theme='monokai',
                         word_wrap=False
                     ))
            else:
                output_display = truncate_text(output,"",self._max_block_tokens)
                blocks.append(Syntax(
                    output_display,
                    'text',
                    theme='monokai',
                    word_wrap=False
                ))
        elif name in {"write_file","edit"} and success and diff:
            output_line = output.strip() if output.strip() else "Completed"
            blocks.append(Text(output_line, style='muted'))
            diff_text = diff
            diff_display = truncate_text(diff_text,self.config.model_name,self._max_block_tokens)
            blocks.append(Syntax(diff_display, 'diff',theme='monokai',word_wrap=True))
        elif name == 'shell' and success:
            command = args.get('command')
            if isinstance(command, str) and command.strip():
                blocks.append(Text(f'$ {command.strip()}',style='muted'))
            if exit_code is not None:
                blocks.append(Text(f"exit_code={exit_code}",style='muted'))
                
            output_display = truncate_text(output,self.config.model_name,self._max_block_tokens)
            blocks.append(Syntax(
                    output_display,
                    'text',
                    theme='monokai',
                    word_wrap=False
                ))
        elif name == "list_dir" and success:
            entries = metadata.get('entries')
            path = metadata.get('path')
            summary = []
            if isinstance(path, str):
                summary.append(path)
            
            if isinstance(entries, int):
                summary.append(f"{entries} entries")
                
            if summary:
                blocks.append(Text(" * ".join(summary),style="muted"))
            
            output_display = truncate_text(output,self.config.model_name,self._max_block_tokens)

            blocks.append(Syntax(output_display, 'text',theme='monokai',word_wrap=True))
            
        elif name == "grep" and success:
            matches = metadata.get('matches')
            files_searched = metadata.get('files_searched')
            summary = []
            if isinstance(matches, int):
                summary.append(f"{matches} matches")
            if isinstance(files_searched, int):
                summary.append(f"searched {files_searched} files")
            if summary:
                blocks.append(Text(" * ".join(summary),style='muted'))
            output_display = truncate_text(output,self.config.model_name,self._max_block_tokens)
            blocks.append(Syntax(output_display, 'text',theme='monokai',word_wrap=True))
        
        elif name == "glob" and success:
            matches = metadata.get('matches')
            
            if isinstance(matches, int):
                blocks.append(Text(f"{matches} matches",style='muted'))
            
            output_display = truncate_text(output,self.config.model_name,self._max_block_tokens)
            blocks.append(Syntax(output_display, 'text',theme='monokai',word_wrap=True))
        
        elif name == "web_search" and success:
            results = metadata.get('results')
            query = args.get('query')
            summary = []
            if isinstance(query, str):
                summary.append(query)
            if isinstance(results, int):
                summary.append(f"{results} results")
            if summary:
                blocks.append(Text(" * ".join(summary),style='muted'))
            output_display = truncate_text(output,self.config.model_name,self._max_block_tokens)
            blocks.append(Syntax(output_display, 'text',theme='monokai',word_wrap=True))
        
        elif name == "web_fetch" and success:
            status_code = metadata.get('status_code')
            content_length = metadata.get('content_length')
            url = args.get('url')
            summary = []
            if isinstance(status_code, int):
                summary.append(f"{str(status_code)} status_code")
            if isinstance(content_length, int):
                summary.append(f"{content_length} bytes")
            if isinstance(url, str):
                summary.append(url)
            if summary:
                blocks.append(Text(" * ".join(summary),style='muted'))
            output_display = truncate_text(output,self.config.model_name,self._max_block_tokens)
            blocks.append(Syntax(output_display, 'text',theme='monokai',word_wrap=True))
        
        elif name == "todos" and success:

            output_display = truncate_text(output,self.config.model_name,self._max_block_tokens)
            blocks.append(Syntax(output_display, 'text',theme='monokai',word_wrap=True))
        elif name == "memory" and success:
            action = args.get('action')
            key = args.get('key')
            found = metadata.get('found')
            summary = []
            if isinstance(action,str) and action:
                summary.append(action)
            if isinstance(key,str) and key:
                summary.append(key)
            if isinstance(found,bool):
                summary.append('found' if found else 'missing')
            if summary:
                blocks.append(Text(" * ".join(summary),style='muted'))
            output_display = truncate_text(output,self.config.model_name,self._max_block_tokens)
            blocks.append(Syntax(output_display, 'text',theme='monokai',word_wrap=True))
        else: 
            if error and not success:
                blocks.append(Text(error,style='error')) 
            output_display = truncate_text(output,self.config.model_name,self._max_block_tokens)
            if output_display.strip():
                blocks.append(Syntax(output_display, 'text',theme='monokai',word_wrap=True))
            else:
                blocks.append(Text('(no output)',style='muted'))
        if truncated:
            blocks.append(Text("note: Tool output was truncated",style="warning"))
        
        panel = Panel(
            Group(
                *blocks
                ),
            title=title,
            border_style=border_style,
            title_align="left",
            subtitle_align="right",
            subtitle=Text("done" if success else "failed", style=status_style),
            box=box.ROUNDED,
            padding=(1,2),
            
        )
        self.console.print()
        self.console.print(panel)
        
    def handle_confirmation(self, confirmation: ToolConfirmation) -> bool:
        output = [
            Text(confirmation.tool_name, style="tool"),
            Text(confirmation.description, style="code"),
        ]

        if confirmation.command:
            output.append(Text(f"$ {confirmation.command}", style="warning"))

        if confirmation.diff:
            diff_text = confirmation.diff.create_diff()
            output.append(
                Syntax(
                    diff_text,
                    "diff",
                    theme="monokai",
                    word_wrap=True,
                )
            )

        self.console.print()
        self.console.print(
            Panel(
                Group(*output),
                title=Text("Approval required", style="warning"),
                title_align="left",
                border_style="warning",
                box=box.ROUNDED,
                padding=(1, 2),
            )
        )

        response = Prompt.ask(
            "\nApprove?", choices=["y", "n", "yes", "no"], default="n"
        )

        return response.lower() in {"y", "yes"}


    def show_help(self) -> None:
        help_text = """
## Commands

- `/help` - Show this help
- `/exit` or `/quit` - Exit the agent
- `/clear` - Clear conversation history
- `/config` - Show current configuration
- `/model <name>` - Change the model
- `/approval <mode>` - Change approval mode
- `/stats` - Show session statistics
- `/tools` - List available tools
- `/mcp` - Show MCP server status
- `/save` - Save current session
- `/checkpoint [name]` - Create a checkpoint
- `/checkpoints` - List available checkpoints
- `/restore <checkpoint_id>` - Restore a checkpoint
- `/sessions` - List saved sessions
- `/resume <session_id>` - Resume a saved session
- `/rag <directory>` - Rag system

## Tips

- Just type your message to chat with the agent
- The agent can read, write, and execute code
- Some operations require approval (can be configured)
"""
        self.console.print(Markdown(help_text))
