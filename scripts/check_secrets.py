"""Stop a commit that would put a secret into git (run by .githooks/pre-commit).

    python scripts/check_secrets.py            check the staged changes
    python scripts/check_secrets.py --all      check every tracked file

Secrets live in Secrets Manager (AWS) or .env (local), never in git: see docs/DEPLOY_AWS.md.
A false alarm can be silenced on that line with the comment  # secret-check: ignore
"""

from __future__ import annotations

import re
import subprocess
import sys

BLOCKED_FILES = re.compile(r"(^|/)(\.env(\.(?!example$)[^/]*)?|[^/]*\.pem|[^/]*\.key|id_rsa[^/]*)$")
PATTERNS = {
    "AWS access key": re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    "Anthropic API key": re.compile(r"\bsk-ant-[0-9A-Za-z_\-]{20,}"),
    "OpenAI-style API key": re.compile(r"\bsk-(proj-)?[0-9A-Za-z]{32,}"),
    "GitHub token": re.compile(r"\bgh[pousr]_[0-9A-Za-z]{36,}\b"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    # a literal value written into a password/secret/key/token setting: PASSWORD = "...", "api_key": "..."
    "password or secret value": re.compile(
        r"(?i)\b\w*(password|passwd|secret|api_?key|access_key|token)['\"]?\s*[:=]\s*(['\"])[^'\"\s]{12,}\2"),
}
# In settings files (.env style) values aren't quoted: KEY=value
ENV_FILE = re.compile(r"(^|/)[^/]*\.(env|ini|cfg|properties)$")
ENV_VALUE = re.compile(r"(?i)^\s*\w*(PASSWORD|SECRET|API_?KEY|ACCESS_KEY|TOKEN)\w*\s*=\s*[^\s#$<{]{8,}")


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True, encoding="utf-8",
                          errors="replace").stdout


def changes(check_all: bool) -> list[tuple[str, int, str]]:
    """(file, line number, text) of every line to check."""
    if check_all:
        out = []
        for f in git("ls-files").splitlines():
            try:
                lines = git("show", f"HEAD:{f}").splitlines()
            except subprocess.CalledProcessError:
                continue
            out += [(f, n, line) for n, line in enumerate(lines, 1)]
        return out
    out, path, n = [], "", 0
    for line in git("diff", "--cached", "-U0", "--no-color").splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else ""
        elif line.startswith("@@"):
            n = int(re.search(r"\+(\d+)", line).group(1)) - 1
        elif line.startswith("+") and path:
            n += 1
            out.append((path, n, line[1:]))
    return out


def main() -> int:
    check_all = "--all" in sys.argv
    files = (git("ls-files") if check_all else git("diff", "--cached", "--name-only", "--diff-filter=AM")).splitlines()
    problems = [f"{f}: this kind of file holds secrets and must not be committed" for f in files
                if BLOCKED_FILES.search(f)]
    for f, n, text in changes(check_all):
        if "secret-check: ignore" in text or f.endswith(".env.example"):
            continue
        for name, rx in PATTERNS.items():
            if rx.search(text):
                problems.append(f"{f}:{n}: looks like a {name}")
        if ENV_FILE.search(f) and ENV_VALUE.search(text):
            problems.append(f"{f}:{n}: looks like a password or secret value")
    if problems:
        print("Commit stopped: possible secrets (they belong in .env or Secrets Manager, see docs/DEPLOY_AWS.md):")
        print("\n".join(f"  {p}" for p in problems))   # where, never the value itself
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
