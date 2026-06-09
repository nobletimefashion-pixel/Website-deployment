import asyncio
from pathlib import Path
from nexus_agent.config.loader import load_config
from nexus_agent.Tools.mcp.mcp_manager import MCPManager
from nexus_agent.Tools.mcp.client import MCPServerStatus

async def list_mcp_tools():
    config = load_config(Path.cwd())
    manager = MCPManager(config)
    await manager.initialize()
    print("Connected MCP Servers:")
    for name, client in manager._clients.items():
        if client.status == MCPServerStatus.CONNECTED:
            print(f"\nServer: {name}")
            print(f"  Status: {client.status.value}")
            print("  Tools:")
            for tool in client.tools:
                print(f"    - {tool.name}: {tool.description}")
        else:
            print(f"\nServer: {name}")
            print(f"  Status: {client.status.value}")
            print("  Tools: (not connected)")

if __name__ == "__main__":
    asyncio.run(list_mcp_tools())