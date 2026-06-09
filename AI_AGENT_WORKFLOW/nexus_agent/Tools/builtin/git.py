# Tools/builtin/git.py
import asyncio
from pathlib import Path
from pydantic import BaseModel, Field
from typing import Literal

from nexus_agent.Tools.base import Tool, ToolInvokation, ToolKind, ToolResult
from nexus_agent.utils.path import resolve_path


class GitParams(BaseModel):
    action: Literal[
        "clone",
        "status",
        "add",
        "commit",
        "push",
        "pull",
        "branch",
        "checkout",
        "log",
        "diff",
        "init",
        "remote"
    ] = Field(
        ...,
        description="The git action to perform"
    )
    
    # Clone specific
    repo_url: str | None = Field(
        None,
        description="Repository URL for clone action"
    )
    destination: str | None = Field(
        None,
        description="Destination directory for clone (optional)"
    )
    
    # Commit specific
    message: str | None = Field(
        None,
        description="Commit message for commit action"
    )
    
    # Add specific
    files: list[str] | None = Field(
        None,
        description="Files to add (use ['.'] for all files)"
    )
    
    # Branch/Checkout specific
    branch_name: str | None = Field(
        None,
        description="Branch name for branch/checkout actions"
    )
    create_branch: bool = Field(
        False,
        description="Create new branch when checking out (checkout -b)"
    )
    
    # Push/Pull specific
    remote: str | None = Field(
        "origin",
        description="Remote name (default: origin)"
    )
    
    # Log specific
    max_count: int | None = Field(
        10,
        ge=1,
        le=100,
        description="Maximum number of commits to show in log"
    )
    
    # Diff specific
    staged: bool = Field(
        False,
        description="Show staged changes for diff (--cached)"
    )
    
    # Remote specific
    remote_url: str | None = Field(
        None,
        description="Remote URL for remote add action"
    )
    remote_action: Literal["add", "remove", "show"] | None = Field(
        "show",
        description="Action for remote command"
    )
    
    # General
    cwd: str | None = Field(
        None,
        description="Working directory for git command"
    )


class GitTool(Tool):
    name = "git"
    description = (
        "Execute git commands for version control operations. "
        "Supports clone, status, add, commit, push, pull, branch, checkout, log, diff, init, and remote operations. "
        "Use this for managing git repositories and version control workflows."
    )
    kind = ToolKind.SHELL
    schema = GitParams
    
    async def execute(self, invocation: ToolInvokation) -> ToolResult:
        params = GitParams(**invocation.params)
        
        # Determine working directory
        if params.cwd:
            cwd = resolve_path(invocation.cwd, params.cwd)
        else:
            cwd = invocation.cwd
        
        # Build git command based on action
        try:
            command = self._build_git_command(params, cwd)
        except ValueError as e:
            return ToolResult.error_result(str(e))
        
        # Execute the command
        return await self._execute_git_command(command, cwd, params.action)
    
    def _build_git_command(self, params: GitParams, cwd: Path) -> list[str]:
        """Build the git command array based on action and parameters."""
        base = ["git"]
        
        if params.action == "clone":
            if not params.repo_url:
                raise ValueError("repo_url is required for clone action")
            base.extend(["clone", params.repo_url])
            if params.destination:
                base.append(params.destination)
        
        elif params.action == "status":
            base.extend(["status", "--short", "--branch"])
        
        elif params.action == "add":
            if not params.files:
                raise ValueError("files is required for add action (use ['.'] for all)")
            base.append("add")
            base.extend(params.files)
        
        elif params.action == "commit":
            if not params.message:
                raise ValueError("message is required for commit action")
            base.extend(["commit", "-m", params.message])
        
        elif params.action == "push":
            base.extend(["push", params.remote or "origin"])
            if params.branch_name:
                base.append(params.branch_name)
        
        elif params.action == "pull":
            base.extend(["pull", params.remote or "origin"])
            if params.branch_name:
                base.append(params.branch_name)
        
        elif params.action == "branch":
            base.append("branch")
            if params.branch_name:
                base.append(params.branch_name)
            else:
                base.append("-a")  # Show all branches
        
        elif params.action == "checkout":
            base.append("checkout")
            if params.create_branch:
                base.append("-b")
            if not params.branch_name:
                raise ValueError("branch_name is required for checkout action")
            base.append(params.branch_name)
        
        elif params.action == "log":
            base.extend([
                "log",
                f"--max-count={params.max_count or 10}",
                "--oneline",
                "--graph",
                "--decorate"
            ])
        
        elif params.action == "diff":
            base.append("diff")
            if params.staged:
                base.append("--cached")
        
        elif params.action == "init":
            base.append("init")
        
        elif params.action == "remote":
            base.append("remote")
            if params.remote_action == "add":
                if not params.remote or not params.remote_url:
                    raise ValueError("remote and remote_url required for remote add")
                base.extend(["add", params.remote, params.remote_url])
            elif params.remote_action == "remove":
                if not params.remote:
                    raise ValueError("remote name required for remote remove")
                base.extend(["remove", params.remote])
            else:  # show
                base.append("-v")
        
        return base
    
    async def _execute_git_command(
        self,
        command: list[str],
        cwd: Path,
        action: str
    ) -> ToolResult:
        """Execute the git command and return appropriate ToolResult."""
        
        # For clone, we might need to work in parent directory
        if action == "clone":
            if not cwd.exists():
                cwd.mkdir(parents=True, exist_ok=True)
        else:
            # Check if we're in a git repository for non-init/clone actions
            if action not in ["init", "clone"]:
                git_dir = cwd / ".git"
                if not git_dir.exists():
                    return ToolResult.error_result(
                        f"Not a git repository: {cwd}",
                        metadata={"action": action, "cwd": str(cwd)}
                    )
        
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd
            )
            
            stdout_data, stderr_data = await asyncio.wait_for(
                process.communicate(),
                timeout=300  # 5 minutes for git operations
            )
            
            stdout = stdout_data.decode('utf-8', errors='replace').strip()
            stderr = stderr_data.decode('utf-8', errors='replace').strip()
            exit_code = process.returncode
            
            # Build output
            output = ""
            if stdout:
                output += stdout
            if stderr:
                if output:
                    output += "\n----stderr----\n"
                output += stderr
            
            # Determine success
            success = exit_code == 0
            
            if not output:
                output = f"Git {action} completed successfully" if success else f"Git {action} failed"
            
            # Build metadata
            metadata = {
                "action": action,
                "exit_code": exit_code,
                "cwd": str(cwd),
                "command": " ".join(command)
            }
            
            if success:
                return ToolResult.success_result(
                    output=output,
                    metadata=metadata
                )
            else:
                return ToolResult.error_result(
                    output or f"Git {action} failed with exit code {exit_code}",
                    metadata=metadata
                )
        
        except asyncio.TimeoutError:
            return ToolResult.error_result(
                f"Git {action} timed out after 300 seconds",
                metadata={"action": action, "command": " ".join(command)}
            )
        except FileNotFoundError:
            return ToolResult.error_result(
                "Git is not installed or not in PATH",
                metadata={"action": action}
            )
        except Exception as e:
            return ToolResult.error_result(
                f"Git command failed: {str(e)}",
                metadata={"action": action, "command": " ".join(command)}
            )