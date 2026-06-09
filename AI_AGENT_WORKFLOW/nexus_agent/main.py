
import asyncio
from pathlib import Path
import sys
from typing import Any
import click
from nexus_agent.Agent.agent import Agent
from nexus_agent.Agent.events import AgentEventType
from nexus_agent.config.config import ApprovalPolicy, Config
from nexus_agent.config.loader import load_config
from nexus_agent.ui.agentui import TUI, get_console
from nexus_agent.Agent.persistence import PersistenceManager, SessionSnapshot
from nexus_agent.Agent.session import Session


console = get_console()


class CLI:
    def __init__(self, config: Config):
        self.agent: Agent | None = None
        self.config = config
        self.tui = TUI(config, console)
        self.rag_mode = False
        self.rag_collection = None

    async def run_single(self, message: str) -> str | None:
        async with Agent(self.config) as agent:
            self.agent = agent
            return await self._process_message(message)

    async def run_rag_mode(self, directory: Path):
        """Run in RAG mode - ingest PDFs and allow queries."""
        self.rag_mode = True

        console.print("\n[highlight]═══ RAG Mode Activated ═══[/highlight]\n")
        console.print(f"[info]Directory:[/info] {directory}")

        async with Agent(self.config) as agent:
            self.agent = agent

            console.print("\n[processing]Ingesting PDF documents...[/processing]\n")

            ingest_result = await self.agent.session.tool_registry.invoke(
                "rag",
                {
                    "action": "ingest",
                    "directory": str(directory),
                    "strategy": "fast",
                    "recursive": True,
                },
                self.config.cwd,
            )

            if not ingest_result.success:
                console.print(f"[error]Ingestion failed: {ingest_result.error}[/error]")
                return

            console.print(f"[success]{ingest_result.output}[/success]\n")

            self.rag_collection = ingest_result.metadata.get("collection_name")

            console.print("[highlight]You can now ask questions about the documents![/highlight]")
            console.print(
                "[dim]Commands: /list (list collections), /switch <name> (switch collection), /exit (quit RAG mode)[/dim]\n"
            )

            while True:
                try:
                    question = console.input("\n[user]Question>[/user] ").strip()

                    if not question:
                        continue

                    if question.lower() in ["/exit", "/quit"]:
                        console.print("\n[dim]Exiting RAG mode...[/dim]")
                        break

                    if question.lower() == "/list":
                        await self._list_rag_collections()
                        continue

                    if question.lower().startswith("/switch "):
                        collection_name = question[8:].strip()
                        self.rag_collection = collection_name
                        console.print(f"[success]Switched to collection: {collection_name}[/success]")
                        continue

                    await self._query_rag(question)

                except KeyboardInterrupt:
                    console.print("\n[dim]Use /exit to quit RAG mode[/dim]")
                except EOFError:
                    break

        console.print("\n[dim]RAG mode ended[/dim]")

    async def _query_rag(self, question: str):
        """Query the RAG system."""
        if not self.rag_collection:
            console.print("[error]No collection selected[/error]")
            return

        console.print("\n[processing]Searching documents...[/processing]")

        result = await self.agent.session.tool_registry.invoke(
            "rag",
            {
                "action": "query",
                "query": question,
                "collection_name": self.rag_collection,
                "top_k": 3,
                "score_threshold": 0.3,
            },
            self.config.cwd,
        )

        if result.success:
            console.print(f"\n[assistant]{result.output}[/assistant]")
        else:
            console.print(f"\n[error]{result.error}[/error]")

    async def _list_rag_collections(self):
        """List available RAG collections."""
        result = await self.agent.session.tool_registry.invoke(
            "rag",
            {"action": "list_collections"},
            self.config.cwd,
        )
        console.print(f"\n{result.output}")

    async def run_interactive(self):
        self.tui.print_welcome(
            "Nexus Agent",
            lines=[
                f"model: {self.config.model_name}",
                f"cwd: {self.config.cwd}",
                f"commands: /help /config /approval /model /rag /exit",
            ],
        )

        async with Agent(self.config, confirmation_callback=self.tui.handle_confirmation) as agent:
            self.agent = agent

            while True:
                try:
                    user_input = console.input("\n[user]>[/user] ").strip()

                    if not user_input:
                        continue
                    if user_input.startswith("/"):
                        should_continue = await self._handle_command(user_input)
                        if not should_continue:
                            break
                        continue
                    
                    if user_input.lower().startswith("/rag "):
                        directory = user_input[5:].strip()
                        dir_path = Path(directory)
                        if not dir_path.exists():
                            console.print(f"[error]Directory not found: {directory}[/error]")
                            continue
                        await self.run_rag_mode(dir_path)
                        continue

                    await self._process_message(user_input)

                except KeyboardInterrupt:
                    console.print("\n[dim]Use /exit to quit[/dim]")
                except EOFError:
                    break

        console.print("\n[dim]Goodbye![/dim]")

    def _get_tool_kind(self, tool_name: str) -> str | None:
        tool = self.agent.session.tool_registry.get(tool_name)
        if not tool:
            return None
        return tool.kind.value

    async def _process_message(self, message: str) -> str | None:
        if not self.agent:
            return None

        assistant_streaming = False
        final_response: str | None = None

        async for event in self.agent.run(message):
            if event.type == AgentEventType.TEXT_DELTA:
                content = event.data.get("content", "")
                if not assistant_streaming:
                    self.tui.begin_assistant()
                    assistant_streaming = True
                self.tui.stream_assistant_delta(content)

            elif event.type == AgentEventType.TEXT_COMPLETE:
                final_response = event.data.get("content")
                if assistant_streaming:
                    self.tui.end_assistant()
                    assistant_streaming = False

            elif event.type == AgentEventType.AGENT_ERROR:
                error = event.data.get("error", "unknown error")
                console.print(f"\n[error]Error {error}[/error]")

            elif event.type == AgentEventType.TOOL_CALL_START:
                tool_name = event.data.get("name", "unknown tool")
                tool_kind = self._get_tool_kind(tool_name)
                self.tui.tool_call_start(
                    event.data.get("call_id", ""),
                    tool_name,
                    tool_kind,
                    event.data.get("arguments", {}),
                )

            elif event.type == AgentEventType.TOOL_CALL_COMPLETE:
                tool_name = event.data.get("name", "unknown")
                tool_kind = self._get_tool_kind(tool_name)
                self.tui.tool_call_complete(
                    event.data.get("call_id", ""),
                    tool_name,
                    tool_kind,
                    event.data.get("success", False),
                    event.data.get("output", ""),
                    event.data.get("error"),
                    event.data.get("metadata"),
                    event.data.get("diff"),
                    event.data.get("truncated", False),
                    event.data.get("exit_code"),
                )

        return final_response


    async def _handle_command(self, command: str) -> bool:
        cmd = command.lower().strip()
        parts = cmd.split(maxsplit=1)
        cmd_name = parts[0]
        cmd_args = parts[1] if len(parts) > 1 else ""
        if cmd_name == "/exit" or cmd_name == "/quit":
            return False
        elif command == "/help":
            self.tui.show_help()
        elif command == "/clear":
            self.agent.session.context_manager.clear()
            self.agent.session.loop_detecter.clear()
            console.print("[success]Conversation cleared [/success]")
        elif command == "/config":
            console.print("\n[bold]Current Configuration[/bold]")
            console.print(f"  Model: {self.config.model_name}")
            console.print(f"  Temperature: {self.config.temperature}")
            console.print(f"  Approval: {self.config.approval.value}")
            console.print(f"  Working Dir: {self.config.cwd}")
            console.print(f"  Max Turns: {self.config.max_turns}")
            console.print(f"  Hooks Enabled: {self.config.hooks_enabled}")
        elif cmd_name == "/model":
            if cmd_args:
                self.config.model_name = cmd_args  #/model opus4.6
                console.print(f"[success]Model changed to: {cmd_args} [/success]")
            else:
                console.print(f"Current model: {self.config.model_name}")
        elif cmd_name == "/approval":
            if cmd_args:
                try:
                    approval = ApprovalPolicy(cmd_args)
                    self.config.approval = approval
                    console.print(
                        f"[success]Approval policy changed to: {cmd_args} [/success]"
                    )
                except:
                    console.print(
                        f"[error]Incorrect approval policy: {cmd_args} [/error]"
                    )
                    console.print(
                        f"Valid options: {', '.join(p for p in ApprovalPolicy)}"
                    )
            else:
                console.print(f"Current approval policy: {self.config.approval.value}")
        elif cmd_name == "/stats":
            stats = self.agent.session.get_stats()
            console.print("\n[bold]Session Statistics [/bold]")
            for key, value in stats.items():
                console.print(f"   {key}: {value}")
        elif cmd_name == "/tools":
            tools = self.agent.session.tool_registry.get_tools()
            console.print(f"\n[bold]Available tools ({len(tools)}) [/bold]")
            for tool in tools:
                console.print(f"  • {tool.name}")
        elif cmd_name == "/mcp":
            mcp_servers = self.agent.session.mcp_manager.get_all_servers()
            console.print(f"\n[bold]MCP Servers ({len(mcp_servers)}) [/bold]")
            for server in mcp_servers:
                status = server["status"]
                status_color = "green" if status == "connected" else "red"
                console.print(
                    f"  • {server['name']}: [{status_color}]{status}[/{status_color}] ({server['tools']} tools)"
                )
        elif cmd_name == "/save":
            persistence_manager = PersistenceManager()
            session_snapshot = SessionSnapshot(
                session_id=self.agent.session.session_id,
                created_at=self.agent.session.created_at,
                updated_at=self.agent.session.updated_at,
                turn_count=self.agent.session.turn_count,
                messages=self.agent.session.context_manager.get_messages(),
                total_usage=self.agent.session.context_manager.total_usage,
            )
            persistence_manager.save_session(session_snapshot)
            console.print(
                f"[success]Session saved: {self.agent.session.session_id}[/success]"
            )
        elif cmd_name == "/sessions":
            persistence_manager = PersistenceManager()
            sessions = persistence_manager.list_sessions()
            console.print("\n[bold]Saved Sessions[/bold]")
            for s in sessions:
                console.print(
                    f"  • {s['session_id']} (turns: {s['turn_count']}, updated: {s['updated_at']})"
                )
        elif cmd_name == "/resume":
            if not cmd_args:
                console.print(f"[error]Usage: /resume <session_id> [/error]")
            else:
                persistence_manager = PersistenceManager()
                snapshot = persistence_manager.load_session(cmd_args)
                if not snapshot:
                    console.print(f"[error]Session does not exist [/error]")
                else:
                    session = Session(
                        config=self.config,
                    )
                    await session.initialize()
                    session.session_id = snapshot.session_id
                    session.created_at = snapshot.created_at
                    session.updated_at = snapshot.updated_at
                    session.turn_count = snapshot.turn_count
                    session.context_manager.total_usage = snapshot.total_usage

                    for msg in snapshot.messages:
                        if msg.get("role") == "system":
                            continue
                        elif msg["role"] == "user":
                            session.context_manager.add_user_message(
                                msg.get("content", "")
                            )
                        elif msg["role"] == "assistant":
                            session.context_manager.add_assistant_message(
                                msg.get("content", ""), msg.get("tool_calls")
                            )
                        elif msg["role"] == "tool":
                            session.context_manager.add_tool_result(
                                msg.get("tool_call_id", ""), msg.get("content", "")
                            )

                    await self.agent.session.client.close()
                    await self.agent.session.mcp_manager.shutdown()

                    self.agent.session = session
                    console.print(
                        f"[success]Resumed session: {session.session_id}[/success]"
                    )
        elif cmd_name == "/checkpoint":
            persistence_manager = PersistenceManager()
            session_snapshot = SessionSnapshot(
                session_id=self.agent.session.session_id,
                created_at=self.agent.session.created_at,
                updated_at=self.agent.session.updated_at,
                turn_count=self.agent.session.turn_count,
                messages=self.agent.session.context_manager.get_messages(),
                total_usage=self.agent.session.context_manager.total_usage,
            )
            checkpoint_id = persistence_manager.save_checkpoint(session_snapshot)
            console.print(f"[success]Checkpoint created: {checkpoint_id}[/success]")
        elif cmd_name == "/restore":
            if not cmd_args:
                console.print(f"[error]Usage: /restire <checkpoint_id> [/error]")
            else:
                persistence_manager = PersistenceManager()
                snapshot = persistence_manager.load_checkpoint(cmd_args)
                if not snapshot:
                    console.print(f"[error]Checkpoint does not exist [/error]")
                else:
                    session = Session(
                        config=self.config,
                    )
                    await session.initialize()
                    session.session_id = snapshot.session_id
                    session.created_at = snapshot.created_at
                    session.updated_at = snapshot.updated_at
                    session.turn_count = snapshot.turn_count
                    session.context_manager.total_usage = snapshot.total_usage

                    for msg in snapshot.messages:
                        if msg.get("role") == "system":
                            continue
                        elif msg["role"] == "user":
                            session.context_manager.add_user_message(
                                msg.get("content", "")
                            )
                        elif msg["role"] == "assistant":
                            session.context_manager.add_assistant_message(
                                msg.get("content", ""), msg.get("tool_calls")
                            )
                        elif msg["role"] == "tool":
                            session.context_manager.add_tool_result(
                                msg.get("tool_call_id", ""), msg.get("content", "")
                            )

                    await self.agent.session.client.close()
                    await self.agent.session.mcp_manager.shutdown()

                    self.agent.session = session
                    console.print(
                        f"[success]Resumed session: {session.session_id}, checkpoint: {checkpoint_id}[/success]"
                    )
        else:
            console.print(f"[error]Unknown command: {cmd_name}[/error]")

        return True




@click.command()
@click.argument("prompt", required=False)
@click.option(
    "--cwd", "-c",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Working directory for the agent",
)
@click.option(
    "--rag", "-r",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Enable RAG mode — path to directory containing PDFs",
)
@click.option(
    "--ui",
    is_flag=True,
    default=False,
    help="Launch the browser-based web UI instead of the terminal interface",
)
@click.option(
    "--ui-port",
    default=7860,
    show_default=True,
    help="Port for the web UI server",
)
@click.option(
    "--ui-host",
    default="127.0.0.1",
    show_default=True,
    help="Host for the web UI server (use 0.0.0.0 to expose on LAN)",
)
@click.option(
    "--no-browser",
    is_flag=True,
    default=False,
    help="Do not open a browser tab automatically when starting the web UI",
)
def main(
    prompt: str | None,
    cwd: Path | None,
    rag: Path | None,
    ui: bool,
    ui_port: int,
    ui_host: str,
    no_browser: bool,
):
    # ── Web UI mode ───────────────────────────────────────────────────────
    if ui:
        try:
            from ui.ui_server import launch as launch_ui
        except ImportError as e:
            console.print(
                f"\n[error]Web UI dependencies missing: {e}\n"
                "Run:  pip install fastapi \"uvicorn[standard]\" websockets[/error]"
            )
            sys.exit(1)

        console.print(
            f"\n[highlight]  Nexus Agent Web UI[/highlight]  "
            f"→  [info]http://{ui_host}:{ui_port}[/info]\n"
        )
        launch_ui(host=ui_host, port=ui_port, open_browser=not no_browser)
        return

    # ── All other modes: load & validate config ───────────────────────────
    try:
        config = load_config(cwd=cwd)
    except Exception as e:
        console.print(f"\n[error]Configuration Error: {e}[/error]")
        sys.exit(1)

    errors = config.validate()
    if errors:
        for error in errors:
            console.print(f"\n[error]Configuration Error: {error}[/error]")
        sys.exit(1)

    cli = CLI(config)

    # ── RAG mode ──────────────────────────────────────────────────────────
    if rag:
        asyncio.run(cli.run_rag_mode(rag))

    # ── Single-prompt mode ────────────────────────────────────────────────
    elif prompt:
        result = asyncio.run(cli.run_single(prompt))
        if result is None:
            sys.exit("Failed to get a response from the agent.")

    # ── Interactive terminal mode (default) ───────────────────────────────
    else:
        asyncio.run(cli.run_interactive())


if __name__ == "__main__":
    main()