"""Developer helper: clone a repo and call one inspection tool by hand.

Example:
    python scripts/try_tool.py https://github.com/pallets/itsdangerous list_files '{"max_depth": 1}'
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auditor.redact import sanitize  # noqa: E402
from auditor.workspace import RepoWorkspace, parse_github_url  # noqa: E402
from tools import RepoTools  # noqa: E402


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    url, tool_name = sys.argv[1], sys.argv[2]
    kwargs = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
    repo = parse_github_url(url)
    with RepoWorkspace(repo.clone_url) as ws:
        repo_tools = RepoTools(ws.root, ws.run_git)
        print(sanitize(getattr(repo_tools, tool_name)(**kwargs)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
