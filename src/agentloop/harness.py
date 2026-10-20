"""Harness runtime selection and launch behavior."""

from __future__ import annotations

from dataclasses import dataclass
import shlex

CLAUDE_DEPLOY_FLAGS_DEFAULT = (
    "--allowedTools 'Bash,mcp__linear__*,mcp__linear-server__*,Read,Edit,Write,Glob,Grep,TodoWrite' "
    "--permission-mode dontAsk"
)
CLAUDE_FLAGS_DEFAULT = "--permission-mode auto"
CODEX_FLAGS_DEFAULT = "--ask-for-approval never --sandbox danger-full-access --skip-git-repo-check"

_DEFAULT_BINS = {
    "claude": "claude",
    "codex": "codex",
    "cursor": "agent",
    "copilot": "copilot",
}
_DEFAULT_FLAGS = {
    "claude": CLAUDE_FLAGS_DEFAULT,
    "codex": CODEX_FLAGS_DEFAULT,
    # Cursor needs --force only for the dedicated loop worktree. Copilot's
    # permissions remain operator-supplied; its default does not grant tools.
    "cursor": "--force --output-format text",
    "copilot": "--no-ask-user --output-format text",
}


@dataclass(frozen=True)
class HarnessAdapter:
    name: str
    bin: str
    flags: str

    def _codex_flag_parts(self) -> tuple[list[str], list[str]]:
        """Split Codex flags between top-level options and ``exec`` options.

        ``--ask-for-approval`` is a top-level Codex option, not an ``exec`` subcommand
        option. ``--skip-git-repo-check`` is an ``exec`` option. Keep the public config
        as one flag string, but render known top-level flags before ``exec``.
        """
        top_level_flags = {
            "--ask-for-approval",
            "-a",
            "--sandbox",
            "-s",
        }
        top_level_prefixes = (
            "--ask-for-approval=",
            "--sandbox=",
        )
        boolean_top_level = {
            "--dangerously-bypass-approvals-and-sandbox",
            "--dangerously-bypass-hook-trust",
        }
        top: list[str] = []
        exec_flags: list[str] = []
        tokens = shlex.split(self.flags)
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok in boolean_top_level or tok.startswith(top_level_prefixes):
                top.append(tok)
                i += 1
                continue
            if tok in top_level_flags:
                top.append(tok)
                if i + 1 < len(tokens):
                    top.append(tokens[i + 1])
                    i += 2
                else:
                    i += 1
                continue
            exec_flags.append(tok)
            i += 1
        return top, exec_flags

    def build_capture_argv(self, prompt: str) -> list[str]:
        """Return argv for a synchronous headless capture call."""
        if self.name == "claude":
            return [self.bin, "-p", "--output-format", "text", prompt]
        if self.name == "codex":
            top, exec_flags = self._codex_flag_parts()
            return [self.bin, *top, "exec", *exec_flags, prompt]
        if self.name in {"cursor", "copilot"}:
            return [self.bin, "-p", *shlex.split(self.flags), prompt]
        raise AssertionError(f"unsupported harness adapter: {self.name}")

    def build_headless_cmd(self, prompt: str) -> str:
        """Return the exact single argv element handed to tmux."""
        if self.name == "claude":
            return f"{shlex.quote(self.bin)} -p {self.flags} {shlex.quote(prompt)}"
        if self.name == "codex":
            top, exec_flags = self._codex_flag_parts()
            parts = [self.bin, *top, "exec", *exec_flags, prompt]
            return shlex.join(parts)
        if self.name in {"cursor", "copilot"}:
            return shlex.join([self.bin, "-p", *shlex.split(self.flags), prompt])
        raise AssertionError(f"unsupported harness adapter: {self.name}")

    def dry_run_preview(self, cfg, key: str, cwd, prompt: str) -> str:
        """Return the human-readable DRY-RUN preview line."""
        if self.name == "claude":
            cmd_preview = f"{self.bin} -p {self.flags}"
        elif self.name == "codex":
            top, exec_flags = self._codex_flag_parts()
            cmd_preview = shlex.join([self.bin, *top, "exec", *exec_flags])
        else:
            cmd_preview = shlex.join([self.bin, "-p", *shlex.split(self.flags)])
        return (
            f"DRY-RUN would spawn: tmux new -d -s {key} -c {cwd} "
            f"'{cmd_preview} \"{prompt}\"' "
            f"(+ pipe-pane log -> {cfg.log_dir}/{cfg.log_tag}/{key}-<ts>.log)"
        )


def supported_harnesses() -> tuple[str, ...]:
    return ("claude", "codex", "cursor", "copilot")


def validate_harness(name: str) -> None:
    if name not in supported_harnesses():
        supported = ", ".join(supported_harnesses())
        raise SystemExit(f"unsupported harness '{name}' (supported: {supported})")


def deploy_default_flags(name: str) -> str | None:
    if name == "claude":
        return CLAUDE_DEPLOY_FLAGS_DEFAULT
    return default_flags(name)


def default_bin(name: str) -> str:
    validate_harness(name)
    return _DEFAULT_BINS[name]


def default_flags(name: str) -> str:
    validate_harness(name)
    return _DEFAULT_FLAGS[name]


def resolve(cfg) -> HarnessAdapter:
    validate_harness(cfg.harness)
    return HarnessAdapter(name=cfg.harness, bin=cfg.harness_bin, flags=cfg.harness_flags)
