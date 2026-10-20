"""agentloop — a Linear-driven tmux orchestration loop.

A Python port of the agent-loop bash scripts (dispatch/debug/deploy/declutter +
agent-loop-lib). Behaviour-preserving: it still spawns detached ``tmux`` sessions
running the configured coding harness, uses ``tmux has-session`` as the
distributed lock, and drives Linear workflow state as the queue/lock/notifier.
"""

__version__ = "0.1.0"
