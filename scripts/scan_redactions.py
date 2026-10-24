"""Developer helper: show every line redact() would mask in real repositories.

Used to check the redactor for false positives (masking things that are not secrets).

Example:
    python scripts/scan_redactions.py https://github.com/psf/requests
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auditor.redact import redact  # noqa: E402
from auditor.workspace import RepoWorkspace, parse_github_url  # noqa: E402
from tools import RepoTools  # noqa: E402


def main() -> int:
    for url in sys.argv[1:]:
        with RepoWorkspace(parse_github_url(url).clone_url) as ws:
            repo_tools = RepoTools(ws.root, ws.run_git)
            n_files = n_masked = 0
            for rel in repo_tools.tracked_files():
                text = repo_tools._read_text(rel)
                if text is None:
                    continue
                n_files += 1
                for n, line in enumerate(text.splitlines(), start=1):
                    out = redact(line)
                    if out != line:
                        n_masked += 1
                        print(f"  {rel}:{n}: {out.strip()[:150]}")
            print(f"{url}: {n_files} files scanned, {n_masked} lines masked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
