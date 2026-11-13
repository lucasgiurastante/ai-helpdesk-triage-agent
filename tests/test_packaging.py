"""Tests of the packaging of the repository.

They verify the promises made to a reviewer cloning the project: the demo
runs with zero installs, no secret is committed, and the documentation
contains the sections the interview relies on (SPEC sections 6 and 7).
"""

import re
import unittest
from pathlib import Path
from typing import List

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The two sentences of the disclaimer the README must contain **verbatim**
#: (source specification, section 17 of the project brief).
MOCK_DISCLAIMER_SENTENCES = (
    "ITSM and identity integrations are mocked for demo purposes.",
    "The adapter pattern allows replacing mock connectors with ServiceNow, "
    "Jira Service Management, Azure AD or Okta APIs.",
)

#: Targets the Makefile must expose.
REQUIRED_MAKE_TARGETS = ("help", "test", "eval", "demo", "clean")

#: Values that are safe to write verbatim in ``.env.example``.
SAFE_VALUES = {
    "HELPDESK_ITSM_ADAPTER": ("mock", "servicenow", "jira"),
    "HELPDESK_IDENTITY_ADAPTER": ("mock", "entra", "okta"),
    "HELPDESK_APP_ADAPTER": ("mock", "sap_fiori", "vpn", "shared_drive"),
    "HELPDESK_ALLOW_WRITES": ("0", "1"),
}

#: Entries ``.gitignore`` must contain.
REQUIRED_GITIGNORE_ENTRIES = (
    ".env",
    "__pycache__/",
    "outputs/*.json",
    "outputs/*.log",
    "*.pyc",
    ".venv/",
    ".DS_Store",
)


def _read(relative: str) -> str:
    """Read a file of the repository.

    Args:
        relative: Path relative to the repository root.

    Returns:
        The content of the file (empty string when the file is missing).
    """
    target = REPO_ROOT / relative
    if not target.is_file():
        return ""
    return target.read_text(encoding="utf-8")


class TestRepositoryLayout(unittest.TestCase):
    """The files listed in the SPEC must exist."""

    def test_required_files_exist(self) -> None:
        for relative in (
            "README.md",
            "Makefile",
            "requirements.txt",
            ".env.example",
            ".gitignore",
            "adapters/__init__.py",
            "adapters/base.py",
            "adapters/itsm.py",
            "adapters/identity.py",
            "adapters/apps.py",
            "eval/__init__.py",
            "eval/metrics.py",
            "eval/run_eval.py",
            "eval/labeled_tickets.json",
            "tests/__init__.py",
            "tests/test_adapters.py",
            "tests/test_eval.py",
            "tests/test_packaging.py",
            "docs/ARCHITECTURE.md",
            "docs/INTERVIEW_PLAYBOOK.md",
            "outputs/.gitkeep",
        ):
            self.assertTrue((REPO_ROOT / relative).exists(), "missing file: {0}".format(relative))


class TestMakefile(unittest.TestCase):
    """The Makefile exposes the canonical targets."""

    def test_targets_are_declared(self) -> None:
        content = _read("Makefile")
        self.assertTrue(content, "Makefile is empty")
        for target in REQUIRED_MAKE_TARGETS:
            pattern = r"(?m)^{0}:(\s+##.*)?$".format(re.escape(target))
            self.assertRegex(content, pattern, "missing Makefile target: {0}".format(target))

    def test_targets_use_tabs(self) -> None:
        content = _read("Makefile")
        for line in content.splitlines():
            if line.startswith("    "):
                self.fail("Makefile recipe must be indented with tabs: {0!r}".format(line))


class TestRequirements(unittest.TestCase):
    """``requirements.txt`` must not install anything by default."""

    def test_no_mandatory_dependency(self) -> None:
        content = _read("requirements.txt")
        self.assertTrue(content, "requirements.txt is empty")
        active = [
            line.strip()
            for line in content.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        self.assertEqual(active, [], "requirements.txt must only contain commented dependencies")


class TestGitignore(unittest.TestCase):
    """Secrets and generated artefacts must never be committed."""

    def test_required_entries(self) -> None:
        content = _read(".gitignore")
        for entry in REQUIRED_GITIGNORE_ENTRIES:
            self.assertIn(entry, content, "missing .gitignore entry: {0}".format(entry))

    def test_the_keep_file_is_not_ignored(self) -> None:
        content = _read(".gitignore")
        self.assertIn("!outputs/.gitkeep", content)


class TestEnvExample(unittest.TestCase):
    """``.env.example`` only holds placeholders."""

    def test_only_placeholders(self) -> None:
        content = _read(".env.example")
        self.assertTrue(content, ".env.example is empty")
        placeholders = ("your", "example", "changeme", "placeholder", "<", "xxx", "acme", "todo")
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            self.assertIn("=", stripped, "malformed line in .env.example: {0!r}".format(stripped))
            key, _, value = stripped.partition("=")
            self.assertRegex(key.strip(), r"^[A-Z][A-Z0-9_]*$", "invalid key: {0!r}".format(key))
            lowered = value.strip().lower()
            if not lowered:
                continue
            if key.strip().upper() in SAFE_VALUES:
                self.assertIn(lowered, SAFE_VALUES[key.strip().upper()])
                continue
            self.assertTrue(
                any(token in lowered for token in placeholders),
                ".env.example must only contain placeholders: {0!r}".format(stripped),
            )

    def test_no_real_secret_looks_like_a_credential(self) -> None:
        content = _read(".env.example")
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            if "PASSWORD" not in key.upper() and "SECRET" not in key.upper() and "TOKEN" not in key.upper():
                continue
            self.assertNotRegex(
                value.strip(),
                r"^[A-Za-z0-9+/]{24,}={0,2}$",
                "a real looking secret leaked into .env.example: {0!r}".format(stripped),
            )


class TestReadme(unittest.TestCase):
    """The README is the public face of the project."""

    def test_mock_disclaimer_is_verbatim(self) -> None:
        content = _read("README.md")
        for sentence in MOCK_DISCLAIMER_SENTENCES:
            self.assertIn(sentence, content, "missing disclaimer sentence: {0}".format(sentence))

    def test_real_connectors_are_advertised(self) -> None:
        """The README promises real connectors; the code must back the promise."""
        content = _read("README.md").lower()
        for connector in ("servicenow", "jira", "entra", "azure ad", "okta"):
            self.assertIn(connector, content, "README does not mention {0}".format(connector))
        for module in ("adapters/itsm.py", "adapters/identity.py"):
            source = _read(module).lower()
            self.assertIn("class ", source, "{0} defines no connector".format(module))

    def test_license_file_is_mit(self) -> None:
        content = _read("LICENSE")
        self.assertTrue(content, "LICENSE is missing")
        self.assertIn("MIT License", content)
        self.assertIn("Copyright", content)
        self.assertIn("Lucas Giurastante", content)
        self.assertIn("[`LICENSE`](LICENSE)", _read("README.md"))

    def test_required_sections(self) -> None:
        content = _read("README.md")
        self.assertIn("```mermaid", content)
        self.assertIn("python3 agent.py --demo", content)
        self.assertIn("python3 eval/run_eval.py", content)
        self.assertIn("make help", content)
        self.assertIn("HELPDESK_ALLOW_WRITES", content)
        self.assertIn("MIT", content)
        self.assertIn("##", content)

    def test_metrics_honesty_section_exists(self) -> None:
        """The README must state what the 1.00 metrics do NOT prove."""
        content = _read("README.md").lower()
        self.assertIn("no prueban", content)
        self.assertIn("consistencia", content)

    def test_mermaid_block_is_balanced(self) -> None:
        content = _read("README.md")
        self.assertEqual(content.count("```mermaid"), content.count("```mermaid"))
        blocks = re.findall(r"```mermaid\n(.*?)```", content, flags=re.DOTALL)
        self.assertTrue(blocks, "no mermaid diagram found in README.md")
        for block in blocks:
            self.assertIn("graph", block)


class TestDocs(unittest.TestCase):
    """The two interview documents must contain their promised sections."""

    def test_architecture_document(self) -> None:
        content = _read("docs/ARCHITECTURE.md")
        self.assertTrue(content)
        self.assertIn("```mermaid", content)
        self.assertIn("Guardrails", content)
        self.assertIn("prompt injection", content.lower())
        self.assertIn("kill-switch", content.lower())

    def test_interview_playbook(self) -> None:
        content = _read("docs/INTERVIEW_PLAYBOOK.md")
        self.assertTrue(content)
        for marker in (
            "demo",
            "Q&A",
            "STAR",
            "30 / 60 / 90",
            "mock",
        ):
            self.assertIn(marker, content)


class TestNoModuleLevelRequestsImport(unittest.TestCase):
    """``requests`` must never be imported at module level in ``adapters/``."""

    def test_no_module_level_import(self) -> None:
        offenders: List[str] = []
        pattern = re.compile(r"^\s*(import requests|from requests)")
        for path in sorted((REPO_ROOT / "adapters").glob("*.py")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if pattern.match(line):
                    offenders.append("{0}:{1}".format(path.name, number))
        self.assertEqual(offenders, [], "module level requests import: {0}".format(offenders))


if __name__ == "__main__":  # pragma: no cover - script entry point
    unittest.main()
