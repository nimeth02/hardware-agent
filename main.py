# main.py — CLI entry point for the spec-analysing agent
#
# Usage:
#   python main.py path/to/spec.txt
#   python main.py            (reads the spec from stdin)

import json
import sys
from pathlib import Path

from agents.spec_analyser_agent.spec_analyzer_agent import run_spec_analysis


def main() -> None:
    if len(sys.argv) > 1:
        spec_text = Path(sys.argv[1]).read_text(encoding="utf-8")
    else:
        spec_text = sys.stdin.read()

    if not spec_text.strip():
        print("No spec text provided.", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(run_spec_analysis(spec_text), indent=2))


if __name__ == "__main__":
    main()
