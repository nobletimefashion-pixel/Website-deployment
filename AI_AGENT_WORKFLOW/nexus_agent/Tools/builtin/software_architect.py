# Tools/builtin/software_architect.py
"""
SoftwareArchitectTool
═════════════════════════════════════════════════════════════════════════════
A force-multiplier that turns any LLM into a full-stack software factory.

Uses the same LLMClient as the rest of the Nexus agent (OpenAI-compatible
API configured via API_KEY and BASE_URL environment variables).

Supported project types
───────────────────────
  saas_web        — Full-stack SaaS (Next.js + FastAPI + PostgreSQL + Stripe)
  web_app         — Single-page or multi-page web application
  rest_api        — Standalone REST/GraphQL API service
  mobile_app      — React Native cross-platform mobile app
  cli_tool        — Python or Node CLI tool with packaging
  desktop_app     — Electron or Tauri desktop application
  chrome_ext      — Chrome / Firefox browser extension
  static_site     — Marketing / landing page (HTML+CSS+JS or Astro)
  data_pipeline   — ETL / data engineering pipeline
  discord_bot     — Discord.py or discord.js bot
  telegram_bot    — python-telegram-bot or grammy.js bot
  custom          — Free-form; tech stack inferred from description
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import textwrap
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field as PF

from nexus_agent.Tools.base import Tool, ToolInvokation, ToolKind, ToolResult


# ─────────────────────────────────────────────────────────────────────────────
# PARAMS
# ─────────────────────────────────────────────────────────────────────────────

class SoftwareArchitectParams(BaseModel):
    description: str = PF(
        ...,
        description=(
            "Plain-English description of the software to build. "
            "Be as detailed or as vague as you like — the tool will ask the LLM "
            "to fill in gaps. Examples: "
            "'Build a SaaS project management tool like Linear', "
            "'Create a REST API for a recipe app with auth and image upload', "
            "'Make a Chrome extension that summarises any webpage'."
        )
    )
    project_type: Literal[
        "saas_web", "web_app", "rest_api", "mobile_app", "cli_tool",
        "desktop_app", "chrome_ext", "static_site", "data_pipeline",
        "discord_bot", "telegram_bot", "custom"
    ] = PF(
        "custom",
        description="Type of software project to scaffold."
    )
    project_name: str = PF(
        "my_project",
        description="Slug name for the project directory (no spaces, lowercase)."
    )
    output_dir: str = PF(
        ".",
        description="Parent directory where the project folder will be created."
    )
    tech_stack: str = PF(
        "",
        description=(
            "Preferred technologies (comma-separated). Leave blank to let the AI choose. "
            "Examples: 'Next.js, FastAPI, PostgreSQL', 'React Native, Supabase', "
            "'Vue 3, Express, MongoDB'."
        )
    )
    features: list[str] = PF(
        default_factory=list,
        description=(
            "Explicit features to include. Leave empty to let AI decide. "
            "Examples: ['auth', 'stripe payments', 'dark mode', 'REST API', 'admin dashboard']"
        )
    )
    # Execution control
    scaffold_only: bool = PF(
        False,
        description=(
            "If True, only generate the blueprint + file tree without writing code. "
            "Good for reviewing the plan before committing."
        )
    )
    install_deps: bool = PF(
        True,
        description="Run npm install / pip install after scaffolding."
    )
    run_linter: bool = PF(
        True,
        description="Run prettier / eslint / ruff after code generation."
    )
    generate_tests: bool = PF(
        True,
        description="Include unit and integration test files."
    )
    generate_docs: bool = PF(
        True,
        description="Generate README.md, API docs, and inline JSDoc/docstrings."
    )
    generate_docker: bool = PF(
        True,
        description="Generate Dockerfile + docker-compose.yml."
    )
    generate_ci: bool = PF(
        True,
        description="Generate GitHub Actions CI/CD workflow."
    )
    # Quality
    code_quality: Literal["mvp", "production", "enterprise"] = PF(
        "production",
        description=(
            "'mvp' — fast, minimal; "
            "'production' — error handling, logging, tests, docs; "
            "'enterprise' — full observability, RBAC, audit logs, multi-tenancy."
        )
    )
    max_files: int = PF(
        60,
        ge=5,
        le=200,
        description="Maximum number of source files to generate (default 60)."
    )


# ─────────────────────────────────────────────────────────────────────────────
# INTERNAL DATA STRUCTURES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ProjectFile:
    path: str
    content: str
    description: str = ""


@dataclass
class Blueprint:
    project_name: str
    project_type: str
    description: str
    tech_stack: dict[str, list[str]]
    architecture: str
    features: list[str]
    file_tree: list[ProjectFile]
    env_vars: dict[str, str]
    setup_commands: list[str]
    dev_command: str
    build_command: str
    test_command: str
    api_endpoints: list[dict]
    db_schema: str
    deployment_notes: str
    security_notes: str


# ─────────────────────────────────────────────────────────────────────────────
# TECH-STACK CANONICAL DEFAULTS
# ─────────────────────────────────────────────────────────────────────────────

_STACK_DEFAULTS: dict[str, dict] = {
    "saas_web": {
        "frontend":    ["Next.js 14 (App Router)", "TypeScript", "Tailwind CSS", "shadcn/ui", "React Query", "Zustand"],
        "backend":     ["FastAPI", "Python 3.12", "SQLAlchemy 2", "Alembic", "Celery", "Redis"],
        "database":    ["PostgreSQL 16"],
        "auth":        ["NextAuth.js / Auth.js"],
        "payments":    ["Stripe"],
        "infra":       ["Docker", "docker-compose", "GitHub Actions"],
        "testing":     ["pytest", "Vitest", "Playwright"],
        "monitoring":  ["Sentry", "Loguru"],
    },
    "web_app": {
        "frontend":    ["React 18", "TypeScript", "Vite", "Tailwind CSS", "React Router 6"],
        "backend":     ["Express.js", "TypeScript", "Prisma", "Zod"],
        "database":    ["PostgreSQL"],
        "auth":        ["JWT + refresh tokens"],
        "infra":       ["Docker", "GitHub Actions"],
        "testing":     ["Vitest", "Testing Library", "Supertest"],
    },
    "rest_api": {
        "backend":     ["FastAPI", "Python 3.12", "SQLAlchemy 2", "Alembic", "Pydantic v2"],
        "database":    ["PostgreSQL"],
        "auth":        ["JWT (python-jose)", "OAuth2 password flow"],
        "infra":       ["Docker", "GitHub Actions", "uvicorn"],
        "testing":     ["pytest", "httpx (AsyncClient)", "factory-boy"],
        "docs":        ["OpenAPI / Swagger UI (built-in)"],
    },
    "mobile_app": {
        "frontend":    ["React Native 0.73", "TypeScript", "Expo SDK 50", "NativeWind", "React Navigation"],
        "backend":     ["Supabase (auth + db + storage)"],
        "state":       ["Zustand", "React Query"],
        "testing":     ["Jest", "React Native Testing Library"],
        "infra":       ["Expo EAS Build", "GitHub Actions"],
    },
    "cli_tool": {
        "runtime":     ["Python 3.12"],
        "framework":   ["Click", "Rich", "Typer"],
        "packaging":   ["pyproject.toml", "hatch", "pipx-ready"],
        "testing":     ["pytest", "Click testing utilities"],
    },
    "desktop_app": {
        "framework":   ["Tauri 2 (Rust + WebView)", "React 18", "TypeScript", "Tailwind CSS"],
        "state":       ["Zustand"],
        "testing":     ["Vitest", "Tauri driver"],
        "infra":       ["GitHub Actions", "tauri-action"],
    },
    "chrome_ext": {
        "framework":   ["React 18", "TypeScript", "Vite + CRXJS"],
        "styling":     ["Tailwind CSS"],
        "storage":     ["Chrome Storage API"],
        "testing":     ["Vitest", "Playwright (extension mode)"],
    },
    "static_site": {
        "framework":   ["Astro 4", "TypeScript", "Tailwind CSS"],
        "cms":         ["Markdown / MDX"],
        "infra":       ["Vercel / Netlify", "GitHub Actions"],
    },
    "data_pipeline": {
        "runtime":     ["Python 3.12"],
        "framework":   ["Apache Airflow 2", "Pandas", "Polars", "SQLAlchemy"],
        "infra":       ["Docker", "PostgreSQL", "Redis"],
        "testing":     ["pytest", "Great Expectations"],
    },
    "discord_bot": {
        "framework":   ["discord.py 2", "Python 3.12", "Pydantic"],
        "database":    ["SQLite (aiosqlite) or PostgreSQL (asyncpg)"],
        "infra":       ["Docker", "GitHub Actions"],
        "testing":     ["pytest", "discord.py test utils"],
    },
    "telegram_bot": {
        "framework":   ["python-telegram-bot 21", "Python 3.12"],
        "database":    ["SQLite or PostgreSQL"],
        "infra":       ["Docker", "GitHub Actions"],
        "testing":     ["pytest"],
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# PROMPT TEMPLATES
# ─────────────────────────────────────────────────────────────────────────────

def _blueprint_prompt(p: SoftwareArchitectParams) -> str:
    stack_hint = (
        f"Preferred stack: {p.tech_stack}" if p.tech_stack
        else f"Recommended defaults for {p.project_type}:\n"
             + json.dumps(_STACK_DEFAULTS.get(p.project_type, {}), indent=2)
    )
    features_hint = (
        "Features requested: " + ", ".join(p.features) if p.features
        else "Infer the most important features from the description."
    )
    quality_notes = {
        "mvp":        "Keep it lean — only core happy-path functionality.",
        "production": "Include error handling, input validation, logging, tests, and security best practices.",
        "enterprise": "Add RBAC, multi-tenancy, audit logging, rate limiting, full observability, and horizontal scaling design.",
    }[p.code_quality]

    return f"""You are a world-class software architect. Design a complete, production-ready
{p.project_type} project called "{p.project_name}".

USER REQUEST
============
{p.description}

STACK GUIDANCE
==============
{stack_hint}

FEATURES
========
{features_hint}

QUALITY LEVEL: {p.code_quality.upper()}
{quality_notes}

OPTIONS
=======
- Generate tests:   {p.generate_tests}
- Generate docs:    {p.generate_docs}
- Generate Docker:  {p.generate_docker}
- Generate CI/CD:   {p.generate_ci}
- Max files:        {p.max_files}

OUTPUT FORMAT
=============
Return a single JSON object with EXACTLY these keys.
No markdown fences. No preamble. Only the JSON.

{{
  "project_name": "{p.project_name}",
  "project_type": "{p.project_type}",
  "description": "one-sentence summary",
  "tech_stack": {{
    "frontend": [],
    "backend": [],
    "database": [],
    "auth": [],
    "infra": [],
    "testing": []
  }},
  "architecture": "2-4 paragraph prose description of the overall architecture, data flow, and key design decisions",
  "features": ["list", "of", "concrete", "features"],
  "env_vars": {{
    "DATABASE_URL": "postgresql://user:pass@localhost:5432/dbname",
    "SECRET_KEY": "random-secret-key",
    "...": "..."
  }},
  "setup_commands": ["npm install", "pip install -r requirements.txt", "..."],
  "dev_command": "npm run dev",
  "build_command": "npm run build",
  "test_command": "npm test",
  "api_endpoints": [
    {{"method": "GET", "path": "/api/v1/users", "auth": true, "description": "List users"}}
  ],
  "db_schema": "SQL CREATE TABLE statements or Prisma schema string",
  "deployment_notes": "How to deploy to production",
  "security_notes": "Key security measures implemented",
  "file_tree": [
    {{
      "path": "relative/path/to/file.ext",
      "description": "what this file does",
      "content": "FULL FILE CONTENT HERE — complete, runnable, no placeholders, no TODO comments"
    }}
  ]
}}

CRITICAL RULES FOR file_tree
=============================
1. Every file MUST have complete, working content — no "// TODO", no "...", no stubs.
2. Include ALL files needed to run the app: entry points, configs, components, routes,
   models, migrations, tests, Dockerfile, .env.example, README.md, package.json /
   pyproject.toml / requirements.txt etc.
3. Code must be idiomatic, modern, and follow best practices for the chosen stack.
4. All imports must resolve within the project (no missing modules).
5. Use TypeScript strict mode for JS/TS projects.
6. For Python: use type hints everywhere, async where appropriate.
7. Every API route must have input validation (Zod / Pydantic).
8. Include at least one test file per major module.
9. README must include: project overview, prerequisites, setup steps, env vars table,
   API reference, and deployment guide.
10. Total files must not exceed {p.max_files}.
"""


def _codegen_prompt(blueprint_json: str, file_path: str, file_desc: str, quality: str) -> str:
    return f"""You are an expert software engineer. Generate the COMPLETE source code for
the file described below. This file is part of a larger project whose blueprint is attached.

FILE TO GENERATE
================
Path:        {file_path}
Description: {file_desc}

PROJECT BLUEPRINT (summary)
============================
{blueprint_json[:3000]}

QUALITY: {quality.upper()}

Rules:
- Return ONLY the raw file content. No markdown. No explanation. No fences.
- Code must be complete, runnable, and production-quality.
- All imports must be correct for the project stack.
- Use TypeScript strict mode if .ts/.tsx.
- Use Python type hints and async if .py.
- No TODO comments, no placeholder functions, no stub implementations.
- Follow the architecture described in the blueprint exactly.
"""


# ─────────────────────────────────────────────────────────────────────────────
# MAIN TOOL
# ─────────────────────────────────────────────────────────────────────────────

class SoftwareArchitectTool(Tool):
    name = "software_architect"
    description = (
        "Master software development tool. Turns any natural-language description "
        "into a fully scaffolded, runnable codebase on disk. "
        "Handles SaaS platforms, web apps, REST APIs, mobile apps, CLI tools, "
        "desktop apps, Chrome extensions, static sites, data pipelines, and bots. "
        "Uses the same LLM as the rest of the Nexus agent. "
        "Creates every file, installs dependencies, runs formatters, and produces "
        "README, Dockerfile, CI/CD, tests, and deployment instructions."
    )
    kind = ToolKind.SHELL
    schema = SoftwareArchitectParams

    # ── LLM client (lazy) ────────────────────────────────────────────────────
    def _get_llm_client(self):
        from client.llm_client import LLMClient
        return LLMClient(config=self.config)

    async def _llm(self, prompt: str, max_tokens: int = 8000) -> str:
        client = self._get_llm_client()
        full_content = ""

        async for event in client.chat_completion(
            messages=[{"role": "user", "content": prompt}],
            stream=True,
        ):
            if event.text_delta and event.text_delta.content:
                full_content += event.text_delta.content
            if event.type == "error":
                raise RuntimeError(f"LLM error: {event.error}")

        return full_content

    # ─────────────────────────────────────────────────────────────────────────
    # ENTRY POINT
    # ─────────────────────────────────────────────────────────────────────────

    async def execute(self, invocation: ToolInvokation) -> ToolResult:
        params = SoftwareArchitectParams(**invocation.params)

        params.project_name = re.sub(r"[^a-z0-9_\-]", "_", params.project_name.lower()).strip("_")

        project_root = Path(invocation.cwd) / params.output_dir / params.project_name
        project_root.mkdir(parents=True, exist_ok=True)

        self._print_banner(params)

        # ── PHASE 1: Blueprint ────────────────────────────────────────────────
        print("🧠  Phase 1/4 — Generating architecture blueprint …")
        try:
            raw_blueprint = await self._llm(_blueprint_prompt(params), max_tokens=16000)
            blueprint_data = self._parse_json(raw_blueprint)
        except Exception as exc:
            return ToolResult.error_result(f"Blueprint generation failed: {exc}")

        bp_path = project_root / ".nexus_blueprint.json"
        bp_path.write_text(json.dumps(blueprint_data, indent=2), encoding="utf-8")
        print(f"   ✅  Blueprint saved ({len(blueprint_data.get('file_tree', []))} files planned)")

        if params.scaffold_only:
            report = self._blueprint_report(blueprint_data, project_root)
            return ToolResult.success_result(
                output=report,
                metadata={"project_root": str(project_root), "blueprint": str(bp_path)}
            )

        # ── PHASE 2: Write files ──────────────────────────────────────────────
        print("\n📁  Phase 2/4 — Scaffolding files …")
        files_written, files_failed = await self._scaffold_files(
            blueprint_data, project_root, params
        )
        print(f"   ✅  {files_written} files written, {files_failed} failed")

        # ── PHASE 3: Install dependencies ─────────────────────────────────────
        print("\n📦  Phase 3/4 — Installing dependencies …")
        install_results = []
        if params.install_deps:
            install_results = await self._install_deps(blueprint_data, project_root)
        else:
            print("   ⏭️  Skipped (install_deps=False)")

        # ── PHASE 4: Quality pass ─────────────────────────────────────────────
        print("\n✨  Phase 4/4 — Quality pass …")
        lint_results = []
        if params.run_linter:
            lint_results = await self._run_quality(blueprint_data, project_root)
        else:
            print("   ⏭️  Skipped (run_linter=False)")

        # ── Final report ──────────────────────────────────────────────────────
        report = self._final_report(
            params, blueprint_data, project_root,
            files_written, files_failed,
            install_results, lint_results
        )
        print(f"\n{report}")

        return ToolResult.success_result(
            output=report,
            metadata={
                "project_root":   str(project_root),
                "blueprint":      str(bp_path),
                "files_written":  files_written,
                "files_failed":   files_failed,
                "project_type":   params.project_type,
                "tech_stack":     blueprint_data.get("tech_stack", {}),
                "dev_command":    blueprint_data.get("dev_command", ""),
                "test_command":   blueprint_data.get("test_command", ""),
            }
        )

    # ─────────────────────────────────────────────────────────────────────────
    # FILE SCAFFOLDING
    # ─────────────────────────────────────────────────────────────────────────

    async def _scaffold_files(
        self,
        blueprint: dict,
        root: Path,
        params: SoftwareArchitectParams,
    ) -> tuple[int, int]:
        files_written = 0
        files_failed  = 0
        blueprint_summary = json.dumps({
            k: v for k, v in blueprint.items() if k != "file_tree"
        }, indent=2)

        file_tree: list[dict] = blueprint.get("file_tree", [])

        for i, file_spec in enumerate(file_tree[:params.max_files], 1):
            fpath    = file_spec.get("path", "")
            fcontent = file_spec.get("content", "")
            fdesc    = file_spec.get("description", "")

            if not fpath:
                continue

            target = root / fpath
            target.parent.mkdir(parents=True, exist_ok=True)

            if len(fcontent.strip()) < 80 or "TODO" in fcontent or "placeholder" in fcontent.lower():
                print(f"   🔄  [{i}/{len(file_tree)}] Regenerating {fpath} …")
                try:
                    fcontent = await self._llm(
                        _codegen_prompt(blueprint_summary, fpath, fdesc, params.code_quality),
                        max_tokens=4000
                    )
                    fcontent = self._strip_fences(fcontent)
                except Exception as exc:
                    print(f"   ⚠️   Failed to regenerate {fpath}: {exc}")
                    files_failed += 1
                    continue
            else:
                print(f"   ✍️   [{i}/{len(file_tree)}] {fpath}")

            try:
                target.write_text(fcontent, encoding="utf-8")
                files_written += 1
            except Exception as exc:
                print(f"   ⚠️   Write error {fpath}: {exc}")
                files_failed += 1

        await self._ensure_essentials(blueprint, root, params)

        return files_written, files_failed

    async def _ensure_essentials(self, blueprint: dict, root: Path, params: SoftwareArchitectParams):
        env_path = root / ".env.example"
        if not env_path.exists():
            env_vars = blueprint.get("env_vars", {})
            lines = [f"{k}={v}" for k, v in env_vars.items()]
            env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            print("   ✅  Generated .env.example")

        gi_path = root / ".gitignore"
        if not gi_path.exists():
            gi_path.write_text(_GITIGNORE, encoding="utf-8")
            print("   ✅  Generated .gitignore")

        readme = root / "README.md"
        if not readme.exists() or readme.stat().st_size < 200:
            print("   🔄  Generating README.md …")
            readme_content = await self._llm(
                self._readme_prompt(blueprint, params), max_tokens=3000
            )
            readme.write_text(readme_content, encoding="utf-8")
            print("   ✅  README.md generated")

        if params.generate_docker and not (root / "Dockerfile").exists():
            df_content = await self._llm(self._dockerfile_prompt(blueprint), max_tokens=2000)
            (root / "Dockerfile").write_text(self._strip_fences(df_content), encoding="utf-8")
            dc_content = await self._llm(self._compose_prompt(blueprint), max_tokens=2000)
            (root / "docker-compose.yml").write_text(self._strip_fences(dc_content), encoding="utf-8")
            print("   ✅  Docker files generated")

        if params.generate_ci:
            ci_dir = root / ".github" / "workflows"
            ci_dir.mkdir(parents=True, exist_ok=True)
            ci_file = ci_dir / "ci.yml"
            if not ci_file.exists():
                ci_content = await self._llm(self._ci_prompt(blueprint, params), max_tokens=2000)
                ci_file.write_text(self._strip_fences(ci_content), encoding="utf-8")
                print("   ✅  GitHub Actions CI generated")

    # ─────────────────────────────────────────────────────────────────────────
    # DEPENDENCY INSTALLATION
    # ─────────────────────────────────────────────────────────────────────────

    async def _install_deps(self, blueprint: dict, root: Path) -> list[str]:
        results: list[str] = []

        if (root / "package.json").exists():
            ok, out = await self._run_cmd(["npm", "install", "--legacy-peer-deps"], cwd=root, timeout=180)
            results.append(f"npm install: {'✅' if ok else '⚠️ '} {out[:120]}")

        req = root / "requirements.txt"
        pyproject = root / "pyproject.toml"
        if req.exists():
            ok, out = await self._run_cmd(
                ["pip", "install", "-r", "requirements.txt", "--quiet", "--break-system-packages"],
                cwd=root, timeout=180
            )
            results.append(f"pip install: {'✅' if ok else '⚠️ '} {out[:120]}")
        elif pyproject.exists():
            ok, out = await self._run_cmd(
                ["pip", "install", "-e", ".[dev]", "--quiet", "--break-system-packages"],
                cwd=root, timeout=180
            )
            results.append(f"pip install -e .[dev]: {'✅' if ok else '⚠️ '} {out[:120]}")

        for r in results:
            print(f"   {r}")
        return results

    # ─────────────────────────────────────────────────────────────────────────
    # QUALITY PASS
    # ─────────────────────────────────────────────────────────────────────────

    async def _run_quality(self, blueprint: dict, root: Path) -> list[str]:
        results: list[str] = []

        checks = [
            (["npx", "prettier", "--write", ".", "--ignore-unknown"],
             "package.json", "prettier"),
            (["npx", "eslint", "--fix", ".", "--ext", ".ts,.tsx,.js,.jsx"],
             "package.json", "eslint"),
            (["python3", "-m", "ruff", "check", "--fix", "."],
             "requirements.txt", "ruff"),
            (["python3", "-m", "ruff", "format", "."],
             "requirements.txt", "ruff format"),
            (["python3", "-m", "mypy", ".", "--ignore-missing-imports"],
             "requirements.txt", "mypy"),
        ]

        for cmd, cond_file, label in checks:
            if not (root / cond_file).exists():
                continue
            ok, out = await self._run_cmd(cmd, cwd=root, timeout=60)
            results.append(f"{label}: {'✅' if ok else '⚠️ '} {out[:100]}")
            print(f"   {results[-1]}")

        return results

    # ─────────────────────────────────────────────────────────────────────────
    # SUBPROCESS HELPER
    # ─────────────────────────────────────────────────────────────────────────

    async def _run_cmd(
        self, cmd: list[str], cwd: Path, timeout: int = 60
    ) -> tuple[bool, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            out = stdout.decode("utf-8", errors="replace").strip()
            return proc.returncode == 0, out
        except asyncio.TimeoutError:
            return False, f"Timed out after {timeout}s"
        except FileNotFoundError:
            return False, f"Command not found: {cmd[0]}"
        except Exception as exc:
            return False, str(exc)

    # ─────────────────────────────────────────────────────────────────────────
    # PROMPT HELPERS
    # ─────────────────────────────────────────────────────────────────────────

    def _readme_prompt(self, blueprint: dict, params: SoftwareArchitectParams) -> str:
        return f"""Generate a comprehensive README.md for this project.

Project: {blueprint.get('project_name')}
Description: {blueprint.get('description')}
Tech stack: {json.dumps(blueprint.get('tech_stack', {}), indent=2)}
Features: {json.dumps(blueprint.get('features', []), indent=2)}
Dev command: {blueprint.get('dev_command')}
Build command: {blueprint.get('build_command')}
Test command: {blueprint.get('test_command')}
Env vars: {json.dumps(blueprint.get('env_vars', {}), indent=2)}
API endpoints: {json.dumps(blueprint.get('api_endpoints', [])[:10], indent=2)}
Deployment: {blueprint.get('deployment_notes', '')}

Include: badges, overview, features list, prerequisites, installation steps,
environment variables table, running locally, API reference, deployment guide,
contributing section, and license (MIT).

Return the raw markdown. No fences."""

    def _dockerfile_prompt(self, blueprint: dict) -> str:
        stack = blueprint.get("tech_stack", {})
        return f"""Generate a production-ready multi-stage Dockerfile for this project.

Stack: {json.dumps(stack, indent=2)}
Dev command: {blueprint.get('dev_command')}
Build command: {blueprint.get('build_command')}

Use multi-stage builds. Minimise image size. Run as non-root user.
Include HEALTHCHECK. Return raw Dockerfile content only."""

    def _compose_prompt(self, blueprint: dict) -> str:
        stack = blueprint.get("tech_stack", {})
        env   = blueprint.get("env_vars", {})
        return f"""Generate a docker-compose.yml for local development of this project.

Stack: {json.dumps(stack, indent=2)}
Env vars needed: {list(env.keys())}

Include services for the app, database, cache (if used), and any workers.
Add volume mounts for hot-reload in development.
Return raw YAML only. No fences."""

    def _ci_prompt(self, blueprint: dict, params: SoftwareArchitectParams) -> str:
        return f"""Generate a GitHub Actions CI/CD workflow (ci.yml) for this project.

Project type: {params.project_type}
Stack: {json.dumps(blueprint.get('tech_stack', {}), indent=2)}
Test command: {blueprint.get('test_command')}
Build command: {blueprint.get('build_command')}
Quality: {params.code_quality}

Include: lint, test, build jobs. Add caching for node_modules / pip.
For production quality: add deploy job to render/railway/fly.io or Docker Hub.
Return raw YAML only. No fences."""

    # ─────────────────────────────────────────────────────────────────────────
    # JSON PARSING
    # ─────────────────────────────────────────────────────────────────────────

    def _parse_json(self, raw: str) -> dict:
        raw = raw.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"\s*```$",          "", raw, flags=re.MULTILINE)
        start = raw.find("{")
        end   = raw.rfind("}") + 1
        if start == -1 or end == 0:
            raise ValueError("No JSON object found in LLM response")
        chunk = raw[start:end]
        try:
            return json.loads(chunk)
        except json.JSONDecodeError:
            chunk = self._fix_json(chunk)
            return json.loads(chunk)

    def _fix_json(self, text: str) -> str:
        text = re.sub(r",\s*([\]}])", r"\1", text)
        return text

    def _strip_fences(self, text: str) -> str:
        text = re.sub(r"^```\w*\s*\n?", "", text.strip())
        text = re.sub(r"\n?```\s*$",    "", text)
        return text.strip()

    # ─────────────────────────────────────────────────────────────────────────
    # REPORTING
    # ─────────────────────────────────────────────────────────────────────────

    def _print_banner(self, params: SoftwareArchitectParams):
        print(f"""
╔══════════════════════════════════════════════════════╗
║  🏗️  Software Architect Tool                         ║
╠══════════════════════════════════════════════════════╣
║  Project : {params.project_name:<42}║
║  Type    : {params.project_type:<42}║
║  Quality : {params.code_quality:<42}║
║  Started : {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC'):<42}║
╚══════════════════════════════════════════════════════╝
""")

    def _blueprint_report(self, bp: dict, root: Path) -> str:
        files = bp.get("file_tree", [])
        endpoints = bp.get("api_endpoints", [])
        features  = bp.get("features", [])
        lines = [
            f"# 🏗️  Blueprint: {bp.get('project_name')}",
            f"\n**Description:** {bp.get('description')}",
            f"\n## Architecture\n{bp.get('architecture', '')}",
            f"\n## Features ({len(features)})",
            *[f"  - {f}" for f in features],
            f"\n## Tech Stack",
        ]
        for layer, techs in bp.get("tech_stack", {}).items():
            if techs:
                lines.append(f"  **{layer}:** {', '.join(techs)}")
        lines += [
            f"\n## Planned Files ({len(files)})",
            *[f"  `{f.get('path')}` — {f.get('description', '')}" for f in files],
            f"\n## API Endpoints ({len(endpoints)})",
            *[f"  {e.get('method')} {e.get('path')} — {e.get('description')}" for e in endpoints],
            f"\n## Commands",
            f"  **Dev:**   `{bp.get('dev_command')}`",
            f"  **Build:** `{bp.get('build_command')}`",
            f"  **Test:**  `{bp.get('test_command')}`",
            f"\n## Security\n{bp.get('security_notes', 'N/A')}",
            f"\n## Deployment\n{bp.get('deployment_notes', 'N/A')}",
            f"\n---\n*Blueprint saved to {root / '.nexus_blueprint.json'}*",
            "*Run without scaffold_only=True to generate all code.*",
        ]
        return "\n".join(lines)

    def _final_report(
        self, params, bp, root, written, failed, install, lint
    ) -> str:
        lines = [
            f"✅  {params.project_name} scaffolded successfully!",
            f"",
            f"📁  Location:  {root}",
            f"📊  Files:     {written} written, {failed} failed",
            f"🗂️  Type:      {params.project_type}",
            f"⚙️  Quality:   {params.code_quality}",
            f"",
            f"🚀  Quick Start",
            f"   cd {root}",
            f"   cp .env.example .env   # fill in your values",
        ]
        setup = bp.get("setup_commands", [])
        for cmd in setup[:5]:
            lines.append(f"   {cmd}")
        lines += [
            f"   {bp.get('dev_command', 'npm run dev')}",
            f"",
            f"🧪  Tests",
            f"   {bp.get('test_command', 'npm test')}",
            f"",
            f"🐳  Docker",
            f"   docker-compose up --build",
            f"",
        ]
        if bp.get("api_endpoints"):
            lines.append(f"🔌  API Endpoints ({len(bp['api_endpoints'])})")
            for ep in bp["api_endpoints"][:8]:
                auth = "🔒" if ep.get("auth") else "🌐"
                lines.append(f"   {auth} {ep.get('method','GET'):6} {ep.get('path')}")
            if len(bp["api_endpoints"]) > 8:
                lines.append(f"   … and {len(bp['api_endpoints'])-8} more (see README.md)")
        if install:
            lines += ["", "📦  Dependencies"]
            for r in install:
                lines.append(f"   {r}")
        if lint:
            lines += ["", "✨  Quality"]
            for r in lint:
                lines.append(f"   {r}")
        lines += [
            "",
            "📖  See README.md for full documentation.",
            "🔐  See .env.example for required environment variables.",
        ]
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# STATIC ASSETS
# ─────────────────────────────────────────────────────────────────────────────

_GITIGNORE = """\
# Dependencies
node_modules/
.venv/
venv/
__pycache__/
*.pyc
*.pyo

# Build outputs
dist/
build/
.next/
out/
*.egg-info/

# Environment
.env
.env.local
.env.*.local

# IDE
.vscode/
.idea/
*.swp
*.swo

# OS
.DS_Store
Thumbs.db

# Logs
*.log
logs/

# Test / coverage
coverage/
.coverage
htmlcov/
.pytest_cache/
.mypy_cache/

# Misc
.cache/
.tmp/
tmp/
"""
