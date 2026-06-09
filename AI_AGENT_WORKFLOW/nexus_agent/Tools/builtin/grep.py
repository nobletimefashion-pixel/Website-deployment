import os
from pathlib import Path
import re
from pydantic import BaseModel, Field
from nexus_agent.Tools.base import Tool, ToolInvokation, ToolKind, ToolResult
from nexus_agent.utils.path import is_binary_file, resolve_path


class GrepParams(BaseModel):
    pattern: str = Field(
        ..., description="Regular expression pattern to search for"
    )
    path: str = Field('.', description="File or directory to search (default: current directory)")
    case_insensitive: bool = Field(False,description='Case-insensitive search (deafult: false)')

class GrepTool(Tool):
    name = "grep"
    description="Search for a regex patter in file contents. Returns matching lines with file paths and line numbers."
    kind = ToolKind.READ
    schema = GrepParams
    
    async def execute(self, invocation:ToolInvokation) -> ToolResult:
        params = GrepParams(**invocation.params)
        search_path = resolve_path(invocation.cwd, params.path)
        
        if not search_path.exists():
            return ToolResult.error_result(f"The path does not exist: {search_path}")
        try:
            flags = re.IGNORECASE if params.case_insensitive else 0
            pattern = re.compile(params.pattern,flags)
                        
        except re.error as e:
            return ToolResult.error_result(f"Invalid regex patter: {e}")
        
        if search_path.is_dir():
            files =self._find_files(search_path)
        else:
            files = [search_path]
            
        output_lines = []
        matches = 0
        for file_path in files:
            try:
                content = file_path.read_text( encoding="utf-8")
            except Exception as e:
                continue
            lines = content.splitlines()
            
            file_matchs = False
            for i,line in enumerate(lines, start=1):
                if pattern.search(line):
                    matches += 1
                    if not file_matchs:
                        rel_path = file_path.relative_to(invocation.cwd)
                        output_lines.append(f"=== {rel_path} ===")
                        file_matchs = True
                    output_lines.append(f"{i}:{line}")
            if file_matchs:
                output_lines.append("")
        
        if not output_lines:
            return ToolResult.success_result(f"No matches found for pattern '{params.pattern}'",metadata={
                'path':str(search_path),
                'entries':matches,
                'files_searched':len(files),
            })
                
        return ToolResult.success_result(
            '\n'.join(output_lines),
            metadata={
                'path':str(search_path),
                'entries':matches,
                'files_searched':len(files),
            }
        )
    
    def _find_file(self,search_path:Path) -> list[Path]:
        files = []
        for root,dir,filenames in os.walk(search_path):
            dir[:] = [d for d in dir if d not in {'node_modules','__pycache__','.git','.venv','venv'}]
            for filename in filenames:
                if filename.startswith('.'):
                    continue
                file_path = Path(root) / filename
                if not is_binary_file(file_path):
                    files.append(file_path)
                    if len(files) >= 500:
                        return files
        return files