"""Enable ``python -m agentloop`` as the ``agent-loop`` console script."""

from agentloop.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
