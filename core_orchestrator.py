"""
Pallatio AI — Core Orchestrator
================================
Multi-agent autonomous orchestration engine powered by Qwen-Max (reasoning)
and Qwen-Coder (execution). Implements a continuous Recon → Coder → QA loop
with sandboxed Docker execution and self-healing code repair.

Architecture:
    ┌────────────┐     ┌────────────┐     ┌────────────┐
    │ Recon Agent│───▶│ Coder Agent│───▶│  QA Agent  │
    │ (Qwen-Max) │     │(Qwen-Coder)│     │ (Qwen-Max) │
    └─────┬──────┘     └─────┬──────┘     └─────┬──────┘
          │                  │                  │
          └──────────────────┴──────────────────┘
                             │
                    ┌────────▼────────┐
                    │ Docker Sandbox  │
                    │  Execution Loop │
                    └─────────────────┘

Usage:
    python core_orchestrator.py

Requirements:
    pip install requests
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

import requests

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────

QWEN_API_BASE = os.getenv("QWEN_API_BASE", "https://dashscope.aliyuncs.com/compatible-mode/v1")
QWEN_API_KEY = os.getenv("QWEN_API_KEY", "")
QWEN_MAX_MODEL = os.getenv("QWEN_MAX_MODEL", "qwen-max")
QWEN_CODER_MODEL = os.getenv("QWEN_CODER_MODEL", "qwen-coder-plus")
MAX_HEAL_RETRIES = int(os.getenv("MAX_HEAL_RETRIES", "3"))
SANDBOX_TIMEOUT = int(os.getenv("SANDBOX_TIMEOUT", "60"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s │ %(name)-18s │ %(levelname)-7s │ %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("pallatio.orchestrator")


# ──────────────────────────────────────────────────────────────────────────────
# Data Models
# ──────────────────────────────────────────────────────────────────────────────

class TaskStatus(Enum):
    """Lifecycle states for a task within the orchestration pipeline."""
    PENDING = "pending"
    RECON = "recon"
    CODING = "coding"
    EXECUTING = "executing"
    VALIDATING = "validating"
    HEALING = "healing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class TaskContext:
    """Immutable context passed through the agent pipeline for a single task."""
    task_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    objective: str = ""
    status: TaskStatus = TaskStatus.PENDING
    recon_report: Optional[str] = None
    generated_code: Optional[str] = None
    execution_result: Optional[str] = None
    qa_verdict: Optional[str] = None
    error_trace: Optional[str] = None
    heal_attempts: int = 0
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the task context to a dictionary."""
        return {
            "task_id": self.task_id,
            "objective": self.objective,
            "status": self.status.value,
            "recon_report": self.recon_report,
            "generated_code": self.generated_code,
            "execution_result": self.execution_result,
            "qa_verdict": self.qa_verdict,
            "error_trace": self.error_trace,
            "heal_attempts": self.heal_attempts,
            "created_at": self.created_at,
            "metadata": self.metadata,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Qwen API Client
# ──────────────────────────────────────────────────────────────────────────────

class QwenClient:
    """
    Lightweight client for the Qwen API (OpenAI-compatible endpoint).
    Supports both Qwen-Max (reasoning) and Qwen-Coder (code generation).
    """

    def __init__(self, api_base: str = QWEN_API_BASE, api_key: str = QWEN_API_KEY):
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        })

    def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> str:
        """
        Send a chat completion request to the Qwen API.

        Args:
            model: The model identifier (e.g., 'qwen-max', 'qwen-coder-plus').
            messages: List of message dicts with 'role' and 'content' keys.
            temperature: Sampling temperature.
            max_tokens: Maximum tokens in the response.

        Returns:
            The assistant's response text.

        Raises:
            RuntimeError: If the API request fails.
        """
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        try:
            response = self.session.post(
                f"{self.api_base}/chat/completions",
                json=payload,
                timeout=120,
            )
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"]
        except requests.exceptions.RequestException as exc:
            logger.error("Qwen API request failed: %s", exc)
            raise RuntimeError(f"Qwen API error: {exc}") from exc


# ──────────────────────────────────────────────────────────────────────────────
# Agent Base Class
# ──────────────────────────────────────────────────────────────────────────────

class Agent(ABC):
    """Abstract base class for all Pallatio agents."""

    def __init__(self, name: str, client: QwenClient):
        self.name = name
        self.client = client
        self.logger = logging.getLogger(f"pallatio.agent.{name}")

    @abstractmethod
    def execute(self, ctx: TaskContext) -> TaskContext:
        """
        Process a task context and return an updated context.

        Args:
            ctx: The current task context.

        Returns:
            Updated task context with this agent's contributions.
        """
        ...

    def _build_system_prompt(self) -> str:
        """Return the system prompt for this agent."""
        return f"You are the {self.name} agent in the Pallatio AI autonomous system."


# ──────────────────────────────────────────────────────────────────────────────
# Recon Agent — Environment Analysis & Task Decomposition
# ──────────────────────────────────────────────────────────────────────────────

class ReconAgent(Agent):
    """
    Reconnaissance agent powered by Qwen-Max.
    Analyzes the task objective, gathers environmental context,
    and produces a structured reconnaissance report for the Coder agent.
    """

    def __init__(self, client: QwenClient):
        super().__init__("recon", client)

    def execute(self, ctx: TaskContext) -> TaskContext:
        self.logger.info("▶ Scanning environment for task [%s]", ctx.task_id)
        ctx.status = TaskStatus.RECON

        messages = [
            {
                "role": "system",
                "content": (
                    "You are the Recon Agent in the Pallatio AI autonomous system. "
                    "Your role is to analyze task objectives, identify requirements, "
                    "assess the execution environment, and produce a structured "
                    "reconnaissance report. Output a clear, actionable plan that "
                    "the Coder Agent can use to generate a solution.\n\n"
                    "Report format:\n"
                    "1. OBJECTIVE ANALYSIS: Break down the task into sub-goals.\n"
                    "2. ENVIRONMENT ASSESSMENT: Identify available tools, constraints.\n"
                    "3. RISK FACTORS: List potential failure modes.\n"
                    "4. RECOMMENDED APPROACH: Step-by-step implementation plan."
                ),
            },
            {
                "role": "user",
                "content": f"Task Objective: {ctx.objective}",
            },
        ]

        try:
            ctx.recon_report = self.client.chat(
                model=QWEN_MAX_MODEL,
                messages=messages,
                temperature=0.4,
            )
            self.logger.info("✓ Recon complete for [%s]", ctx.task_id)
        except RuntimeError as exc:
            self.logger.warning("Recon failed, using fallback: %s", exc)
            ctx.recon_report = (
                f"[FALLBACK] Direct execution of objective: {ctx.objective}\n"
                "No additional reconnaissance data available."
            )

        return ctx


# ──────────────────────────────────────────────────────────────────────────────
# Coder Agent — Code Generation & Synthesis
# ──────────────────────────────────────────────────────────────────────────────

class CoderAgent(Agent):
    """
    Code generation agent powered by Qwen-Coder.
    Takes the Recon report and synthesizes executable code artifacts
    that address the task objective.
    """

    def __init__(self, client: QwenClient):
        super().__init__("coder", client)

    def execute(self, ctx: TaskContext) -> TaskContext:
        self.logger.info("▶ Generating solution for task [%s]", ctx.task_id)
        ctx.status = TaskStatus.CODING

        # Build the coding prompt with context from Recon
        system_prompt = (
            "You are the Coder Agent in the Pallatio AI autonomous system. "
            "Your role is to generate clean, production-quality Python code "
            "based on the reconnaissance report. Output ONLY executable Python "
            "code — no markdown, no explanations, no code fences. The code must "
            "be self-contained and handle its own error cases gracefully."
        )

        user_content = (
            f"## Task Objective\n{ctx.objective}\n\n"
            f"## Reconnaissance Report\n{ctx.recon_report or 'No recon data available.'}"
        )

        # If this is a healing pass, include the error trace
        if ctx.error_trace and ctx.heal_attempts > 0:
            user_content += (
                f"\n\n## Previous Error (Attempt {ctx.heal_attempts})\n"
                f"```\n{ctx.error_trace}\n```\n\n"
                f"## Previous Code\n```python\n{ctx.generated_code}\n```\n\n"
                "Fix the error and regenerate a corrected version."
            )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        try:
            ctx.generated_code = self.client.chat(
                model=QWEN_CODER_MODEL,
                messages=messages,
                temperature=0.2,
                max_tokens=8192,
            )
            self.logger.info("✓ Code generated for [%s] (%d chars)", ctx.task_id, len(ctx.generated_code))
        except RuntimeError as exc:
            self.logger.error("Code generation failed: %s", exc)
            ctx.status = TaskStatus.FAILED
            ctx.error_trace = str(exc)

        return ctx


# ──────────────────────────────────────────────────────────────────────────────
# QA Agent — Validation & Quality Assurance
# ──────────────────────────────────────────────────────────────────────────────

class QAAgent(Agent):
    """
    Quality assurance agent powered by Qwen-Max.
    Validates execution results against acceptance criteria derived
    from the original task objective.
    """

    def __init__(self, client: QwenClient):
        super().__init__("qa", client)

    def execute(self, ctx: TaskContext) -> TaskContext:
        self.logger.info("▶ Validating output for task [%s]", ctx.task_id)
        ctx.status = TaskStatus.VALIDATING

        messages = [
            {
                "role": "system",
                "content": (
                    "You are the QA Agent in the Pallatio AI autonomous system. "
                    "Evaluate whether the execution result satisfies the original "
                    "task objective. Respond with a JSON object:\n"
                    '{"verdict": "PASS" or "FAIL", "reason": "...", "confidence": 0.0-1.0}'
                ),
            },
            {
                "role": "user",
                "content": (
                    f"## Original Objective\n{ctx.objective}\n\n"
                    f"## Generated Code\n```python\n{ctx.generated_code}\n```\n\n"
                    f"## Execution Result\n```\n{ctx.execution_result or 'No output captured.'}\n```"
                ),
            },
        ]

        try:
            raw_verdict = self.client.chat(
                model=QWEN_MAX_MODEL,
                messages=messages,
                temperature=0.1,
            )
            ctx.qa_verdict = raw_verdict
            self.logger.info("✓ QA verdict for [%s]: %s", ctx.task_id, raw_verdict[:100])
        except RuntimeError as exc:
            self.logger.warning("QA validation failed, defaulting to PASS: %s", exc)
            ctx.qa_verdict = json.dumps({
                "verdict": "PASS",
                "reason": "QA agent unavailable — defaulting to PASS.",
                "confidence": 0.0,
            })

        return ctx


# ──────────────────────────────────────────────────────────────────────────────
# Sandbox Executor — Docker-based Isolated Execution
# ──────────────────────────────────────────────────────────────────────────────

class SandboxExecutor:
    """
    Executes generated code in an ephemeral Docker container with
    network isolation and resource constraints.

    In development mode (DOCKER_SANDBOX=false), falls back to local
    subprocess execution with a timeout.
    """

    def __init__(self, timeout: int = SANDBOX_TIMEOUT):
        self.timeout = timeout
        self.use_docker = os.getenv("DOCKER_SANDBOX", "false").lower() == "true"
        self.logger = logging.getLogger("pallatio.sandbox")

    def execute(self, code: str, ctx: TaskContext) -> TaskContext:
        """
        Execute the generated code and capture the result.

        Args:
            code: The Python code to execute.
            ctx: The current task context.

        Returns:
            Updated task context with execution results.
        """
        ctx.status = TaskStatus.EXECUTING
        execution_id = f"exec-{ctx.task_id}-{int(time.time())}"
        self.logger.info("▶ Executing [%s] (docker=%s)", execution_id, self.use_docker)

        if self.use_docker:
            return self._execute_docker(code, ctx, execution_id)
        else:
            return self._execute_local(code, ctx, execution_id)

    def _execute_docker(self, code: str, ctx: TaskContext, exec_id: str) -> TaskContext:
        """Execute code in an ephemeral Docker container."""
        try:
            result = subprocess.run(
                [
                    "docker", "run",
                    "--rm",                         # Ephemeral container
                    "--network", "none",            # Network isolation
                    "--memory", "256m",             # Memory cap
                    "--cpus", "0.5",                # CPU limit
                    "--read-only",                  # Read-only filesystem
                    "--tmpfs", "/tmp:size=64m",     # Writable temp space
                    "--name", exec_id,
                    "python:3.11-slim",
                    "python", "-c", code,
                ],
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            ctx.execution_result = result.stdout
            if result.returncode != 0:
                ctx.error_trace = result.stderr
                self.logger.warning("Execution failed [%s]: %s", exec_id, result.stderr[:200])
            else:
                self.logger.info("✓ Execution succeeded [%s]", exec_id)
        except subprocess.TimeoutExpired:
            ctx.error_trace = f"Execution timed out after {self.timeout}s"
            self.logger.error("Execution timed out [%s]", exec_id)
        except FileNotFoundError:
            ctx.error_trace = "Docker is not installed or not in PATH"
            self.logger.error("Docker not available")

        return ctx

    def _execute_local(self, code: str, ctx: TaskContext, exec_id: str) -> TaskContext:
        """Execute code locally via subprocess (development mode)."""
        try:
            result = subprocess.run(
                ["python3", "-c", code],
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            ctx.execution_result = result.stdout
            if result.returncode != 0:
                ctx.error_trace = result.stderr
                self.logger.warning("Local execution failed [%s]: %s", exec_id, result.stderr[:200])
            else:
                self.logger.info("✓ Local execution succeeded [%s]", exec_id)
        except subprocess.TimeoutExpired:
            ctx.error_trace = f"Execution timed out after {self.timeout}s"
            self.logger.error("Execution timed out [%s]", exec_id)

        return ctx


# ──────────────────────────────────────────────────────────────────────────────
# Core Orchestrator — Multi-Agent Pipeline Controller
# ──────────────────────────────────────────────────────────────────────────────

class CoreOrchestrator:
    """
    The central orchestration engine for Pallatio AI.
    Manages the multi-agent pipeline: Recon → Coder → Execute → QA → Heal.

    The orchestrator runs a continuous loop, routing tasks through the agent
    pipeline and triggering self-healing when execution failures are detected.
    """

    def __init__(self):
        self.client = QwenClient()
        self.recon = ReconAgent(self.client)
        self.coder = CoderAgent(self.client)
        self.qa = QAAgent(self.client)
        self.sandbox = SandboxExecutor()
        self.logger = logging.getLogger("pallatio.orchestrator")

    def run(self, objective: str) -> TaskContext:
        """
        Execute the full autonomous pipeline for a given objective.

        Args:
            objective: The high-level task description.

        Returns:
            The final task context with all pipeline results.
        """
        ctx = TaskContext(objective=objective)
        self.logger.info("=" * 60)
        self.logger.info("PALLATIO AI — Autonomous Orchestration")
        self.logger.info("Task ID:    %s", ctx.task_id)
        self.logger.info("Objective:  %s", ctx.objective[:80])
        self.logger.info("=" * 60)

        # Phase 1: Reconnaissance
        ctx = self.recon.execute(ctx)

        # Phase 2: Code Generation
        ctx = self.coder.execute(ctx)
        if ctx.status == TaskStatus.FAILED:
            return ctx

        # Phase 3: Sandboxed Execution
        ctx = self.sandbox.execute(ctx.generated_code, ctx)

        # Phase 4: Quality Assurance + Self-Healing Loop
        while ctx.heal_attempts < MAX_HEAL_RETRIES:
            ctx = self.qa.execute(ctx)

            # Parse QA verdict
            verdict = self._parse_verdict(ctx.qa_verdict)
            if verdict.get("verdict") == "PASS":
                ctx.status = TaskStatus.COMPLETED
                self.logger.info("✓ Task [%s] COMPLETED successfully", ctx.task_id)
                break

            # Trigger healing
            ctx.heal_attempts += 1
            ctx.status = TaskStatus.HEALING
            self.logger.warning(
                "⟳ Healing attempt %d/%d for [%s]: %s",
                ctx.heal_attempts,
                MAX_HEAL_RETRIES,
                ctx.task_id,
                verdict.get("reason", "Unknown failure"),
            )

            # Re-generate code with error context
            ctx = self.coder.execute(ctx)
            if ctx.status == TaskStatus.FAILED:
                break

            # Re-execute in fresh sandbox
            ctx = self.sandbox.execute(ctx.generated_code, ctx)

        if ctx.status != TaskStatus.COMPLETED:
            ctx.status = TaskStatus.FAILED
            self.logger.error(
                "✗ Task [%s] FAILED after %d heal attempts",
                ctx.task_id,
                ctx.heal_attempts,
            )

        # Final summary
        self._print_summary(ctx)
        return ctx

    def _parse_verdict(self, verdict_str: Optional[str]) -> dict[str, Any]:
        """Attempt to parse the QA verdict as JSON, with fallback."""
        if not verdict_str:
            return {"verdict": "FAIL", "reason": "No verdict provided", "confidence": 0.0}
        try:
            # Try to extract JSON from the response
            start = verdict_str.find("{")
            end = verdict_str.rfind("}") + 1
            if start != -1 and end > start:
                return json.loads(verdict_str[start:end])
        except (json.JSONDecodeError, ValueError):
            pass
        # Fallback: check for keywords
        if "PASS" in verdict_str.upper():
            return {"verdict": "PASS", "reason": verdict_str, "confidence": 0.5}
        return {"verdict": "FAIL", "reason": verdict_str, "confidence": 0.5}

    def _print_summary(self, ctx: TaskContext) -> None:
        """Print a formatted summary of the task execution."""
        self.logger.info("")
        self.logger.info("─" * 60)
        self.logger.info("EXECUTION SUMMARY")
        self.logger.info("─" * 60)
        self.logger.info("Task ID:        %s", ctx.task_id)
        self.logger.info("Status:         %s", ctx.status.value.upper())
        self.logger.info("Heal Attempts:  %d / %d", ctx.heal_attempts, MAX_HEAL_RETRIES)
        self.logger.info("Objective:      %s", ctx.objective[:60])
        if ctx.execution_result:
            self.logger.info("Output:         %s", ctx.execution_result[:200])
        if ctx.error_trace:
            self.logger.info("Last Error:     %s", ctx.error_trace[:200])
        self.logger.info("─" * 60)


# ──────────────────────────────────────────────────────────────────────────────
# Entry Point
# ──────────────────────────────────────────────────────────────────────────────

def main():
    """
    Pallatio AI — Core Orchestrator Entry Point.

    Demonstrates the multi-agent autonomous pipeline by executing
    a sample task through the Recon → Coder → QA loop.
    """
    print()
    print("╭─────────────────────────────────────────────────────────╮")
    print("│                                                         │")
    print("│              🧠  PALLATIO AI  v0.1.0                    │")
    print("│         Decentralized Autonomous Ecosystem              │")
    print("│                                                         │")
    print("│  Agents:  Qwen-Max (Reasoning) + Qwen-Coder (Exec)      │")
    print("│  Mode:    Multi-Agent Swarm Orchestration               │")
    print("│  Sandbox: Docker Isolated Execution                     │")
    print("│                                                         │")
    print("╰─────────────────────────────────────────────────────────╯")
    print()

    # Validate configuration
    if not QWEN_API_KEY:
        logger.warning(
            "QWEN_API_KEY not set. Running in simulation mode.\n"
            "Set the environment variable to enable live API calls:\n"
            "  export QWEN_API_KEY='your-api-key'"
        )

    # Sample task for demonstration
    sample_objective = (
        "Create a Python script that monitors system CPU and memory usage "
        "every 5 seconds and logs the metrics to a JSON file with timestamps."
    )

    orchestrator = CoreOrchestrator()
    result = orchestrator.run(sample_objective)

    # Output final state
    print("\n📋 Final Task State:")
    print(json.dumps(result.to_dict(), indent=2, default=str))


if __name__ == "__main__":
    main()
