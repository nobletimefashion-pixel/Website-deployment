import importlib.util
import inspect
from pathlib import Path
import sys
from typing import Any
from nexus_agent.config.config import Config
from nexus_agent.config.loader import get_config_dir
from nexus_agent.Tools.base import Tool
from nexus_agent.Tools.registry import ToolRegistry


class ToolDiscoveryManager:
    def __init__(self, config: Config, registry: ToolRegistry):
        self.config = config
        self.registry = registry

    def _load_tool_modules(self, file_path: Path) -> Any:#loads tools initialization
        module_name = f"discovered_tool_{file_path.stem}"#rmoving .py from the file
        spec = importlib.util.spec_from_file_location(module_name, file_path)#name, filepath and how to load it

        if spec is None or spec.loader is None:
            return ImportError(f"Could not load spec from {file_path}")

        module = importlib.util.module_from_spec(spec)#creates an empty container for module
        sys.modules[module_name] = module#making it so that python knows this module

        spec.loader.exec_module(module)
        #module.testTool
        return module

    def _find_tool_classes(self, module: Any) -> list[Tool]:
        tools: list[Tool] = []

        for name in dir(module):
            obj = getattr(module, name)
            if (
                inspect.isclass(obj)
                and issubclass(obj, Tool)
                and obj is not Tool
                and obj.__module__ == module.__name__
            ):
                tools.append(obj)

        return tools

    def discover_from_directory(self, directory: Path) -> None:
        tool_dir = directory / ".ai-agent" / "tools"

        if not tool_dir.exists() or not tool_dir.is_dir():
            return

        for py_file in tool_dir.glob("*.py"):
            try:
                if py_file.name.startswith("__"):#ignoring __init__.py
                    continue

                module = self._load_tool_modules(py_file)
                tool_classes = self._find_tool_classes(module)

                if not tool_classes:
                    continue

                for tool_class in tool_classes:
                    tool = tool_class(self.config)
                    self.registry.register(tool)
            except Exception:
                continue

    def discover_all(self) -> None:
        self.discover_from_directory(self.config.cwd)
        self.discover_from_directory(get_config_dir())
