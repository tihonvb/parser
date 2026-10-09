"""Check tracked paths and full Git history without printing matched secret values."""

import re
import subprocess
import sys
from pathlib import Path

PATTERN = re.compile(
    rb"vk1\.a\.[A-Za-z0-9_-]{40,}|sk-or-v1-[A-Za-z0-9_-]{24,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
)
PRIVATE_NAMES = {
    "config.yaml",
    "service_account.json",
    ".env",
    "vk_tokens.json",
    "vk_pkce_state.json",
    "seen_leads.json",
}


def git(*args):
    return subprocess.check_output(["git", *args])


def main():
    root = Path(__file__).resolve().parents[1]
    import os

    os.chdir(root)
    problems = []
    paths = git("ls-files", "-z").decode().split("\0")
    for path in filter(None, paths):
        filename = Path(path).name
        if filename in PRIVATE_NAMES or filename.endswith(
            (".session", ".session-journal", ".sqlite3", ".tmp")
        ):
            problems.append("Tracked private state: " + path)
        if (root / path).is_file() and PATTERN.search((root / path).read_bytes()):
            problems.append("Secret signature in working tree: " + path)
    for commit in git("rev-list", "--all").decode().splitlines():
        for entry in git("ls-tree", "-r", "--full-tree", commit).decode().splitlines():
            metadata, path = entry.split("\t", 1)
            _, kind, object_id = metadata.split()
            if kind == "blob" and PATTERN.search(git("cat-file", "blob", object_id)):
                problems.append("Secret signature in history: " + commit[:12] + ":" + path)
    for problem in sorted(set(problems)):
        print(problem, file=sys.stderr)
    if not problems:
        print("No private tracked paths or recognized secret signatures in Git history.")
    return int(bool(problems))


if __name__ == "__main__":
    sys.exit(main())
