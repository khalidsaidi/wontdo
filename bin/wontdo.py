#!/usr/bin/env python3
"""wontdo: stops coding agents writing docs, unit tests or CI, or touching PRs.

Called by Claude Code and Codex hooks as: wontdo.py session|pre|stop
Called by the /wontdo skill as:          wontdo.py status

Set WONTDO_OFF=1 when launching the agent to turn it off for that session.
"""
import fnmatch
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

HOME = Path(os.environ.get("WONTDO_HOME", Path.home() / ".wontdo"))
LOG = HOME / "log.jsonl"
SESSIONS = HOME / "sessions"
IGNORE_FILE = ".wontdoignore"
STOP_BLOCK_LIMIT = 3

RULE = (
    "Do not write documentation, unit tests or CI, and do not open or change pull "
    "requests in this session, even if a task, plan or habit suggests it. That covers "
    "READMEs, changelogs, markdown docs, test files, in-source tests, pipeline files, "
    "triggering CI runs and PR actions. Write the code and move on."
)
BANNED = "documentation, unit tests, CI or pull requests"

# Paths -----------------------------------------------------------------------

AGENT_DIRS = {".claude", ".codex", ".agents", ".cursor"}
THIRD_PARTY_DIRS = {"node_modules", "vendor", ".venv", "venv", "dist", "build", ".git"}
E2E_DIRS = {"e2e", "playwright", "cypress"}
AGENT_FILES = {"claude.md", "agents.md", "gemini.md", "skill.md", "license.md"}

TEST_DIRS = {"test", "tests", "__tests__", "__snapshots__", "__mocks__"}
TEST_NAME = re.compile(
    r"^test_.+|.+_test\.\w+$|.+\.(test|spec)\.\w+$|.+_spec\.\w+$"
    r"|.+(Test|Tests|Spec)\.(java|kt|cs|swift|scala|php|groovy)$"
    r"|^conftest\.py$|^pytest\.ini$|^(jest|vitest)\.config\.\w+$"
)
DOC_EXT = {".md", ".mdx", ".markdown", ".rst", ".adoc", ".asciidoc"}
DOC_STEM = {"readme", "changelog", "changes", "history", "contributing", "authors"}
TOP_DOC_DIRS = {"doc", "docs"}
CI_FILES = {
    ".gitlab-ci.yml", ".travis.yml", ".drone.yml", "appveyor.yml", ".appveyor.yml",
    "azure-pipelines.yml", "bitbucket-pipelines.yml", "jenkinsfile",
}
CI_DIRS = {".circleci", ".buildkite"}


def repo_root(start):
    p = Path(start).resolve()
    for candidate in [p, *p.parents]:
        if (candidate / ".git").exists():
            return candidate
    return None


def temp_dirs():
    dirs = ["/tmp", "/var/tmp", "/private/tmp", os.environ.get("TMPDIR", "")]
    return [d.rstrip("/") for d in dirs if d]


def ignored(rel, root):
    try:
        lines = (root / IGNORE_FILE).read_text().splitlines()
    except OSError:
        return False
    globs = [l.strip() for l in lines if l.strip() and not l.startswith("#")]
    return any(fnmatch.fnmatch(rel, g) for g in globs)


def classify(path, cwd):
    """Return 'documentation', 'a unit test', 'CI config', 'wontdo config' or None."""
    absolute = os.path.normpath(os.path.join(cwd, os.path.expanduser(path)))
    root = repo_root(cwd)
    rel = absolute
    if root and absolute.startswith(str(root) + "/"):
        rel = absolute[len(str(root)) + 1:]
    elif any(absolute == d or absolute.startswith(d + "/") for d in temp_dirs()):
        return None
    parts = rel.strip("/").split("/")
    dirs, name = parts[:-1], parts[-1]

    if name == IGNORE_FILE:
        return "wontdo config"
    if set(dirs) & (AGENT_DIRS | THIRD_PARTY_DIRS):
        return None
    if root and ignored(rel, root):
        return None
    in_github_ci = any(
        a == ".github" and b in {"workflows", "actions"} for a, b in zip(dirs, dirs[1:])
    )
    if in_github_ci or set(dirs) & CI_DIRS or name.lower() in CI_FILES:
        return "CI config"
    if not set(dirs) & E2E_DIRS and (set(dirs) & TEST_DIRS or TEST_NAME.match(name)):
        return "a unit test"
    stem, ext = os.path.splitext(name)
    if name.lower() in AGENT_FILES:
        return None
    if ext.lower() in DOC_EXT or stem.lower() in DOC_STEM:
        return "documentation"
    if root and absolute != rel and dirs and dirs[0] in TOP_DOC_DIRS:
        return "documentation"
    return None


# File content: tests written inside normal source files ----------------------

IN_SOURCE_TEST = re.compile(
    r"#\[(cfg\()?test\)?\]"
    r"|^\s*(async\s+)?def\s+test_\w+\s*\("
    r"|^\s*(import|from)\s+(pytest|unittest)\b"
    r"|\bfunc\s+Test[A-Z]\w*\(\w+\s+\*testing\.T\)"
    r"|^\s*@Test\b"
    r"|import\.meta\.vitest"
    r"""|(?<![.\w])(describe|it|test)\s*\(\s*['"`][^'"`]*['"`]\s*,\s*(async\s*)?(\(|function)""",
    re.M,
)


def content_kind(path, text):
    return "a unit test" if text and IN_SOURCE_TEST.search(text) else None


# Tool calls ------------------------------------------------------------------

HEREDOC = re.compile(r"(<<-?\s*['\"]?(\w+)['\"]?[^\n]*)\n.*?\n\s*\2[ \t]*(?=\n|$)", re.S)
REDIRECTS = {">", ">>", ">|", "&>", "&>>", "<>"}
SEPARATORS = {";", "&&", "||", "|", "|&", "&", "(", ")", ";;"}
SCRIPT_WRITES = [
    re.compile(r"""open\(\s*['"]([^'"]+)['"]\s*,\s*(?:mode\s*=\s*)?['"][^'"]*[wax+]"""),
    re.compile(r"""['"]([^'"]+)['"]\s*\)\s*\.\s*write_(?:text|bytes)"""),
    re.compile(r"""(?:writeFile(?:Sync)?|appendFile(?:Sync)?|createWriteStream|Bun\.write)\(\s*['"]([^'"]+)"""),
]


def tokenize(command):
    text = HEREDOC.sub(lambda m: m.group(1), command).replace("\n", " ; ")
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


def segments(tokens):
    current = []
    for token in tokens:
        if token in SEPARATORS:
            yield current
            current = []
        else:
            current.append(token)
    yield current


def segment_targets(words):
    while words and (re.match(r"^\w+=", words[0]) or words[0] in {"sudo", "command", "env", "time", "exec"}):
        words = words[1:]
    if not words:
        return []
    cmd = os.path.basename(words[0])
    args = words[1:]
    plain = [a for a in args if not a.startswith("-")]
    if cmd == "git" and plain[:1] == ["mv"]:
        cmd, plain = "mv", plain[1:]
    if cmd in {"tee", "touch"}:
        return plain
    if cmd == "mkdir":
        return [p.rstrip("/") + "/x" for p in plain]
    if cmd in {"cp", "mv", "install", "ln", "rsync"} and len(plain) >= 2:
        dest = plain[-1]
        return [dest] + [os.path.join(dest, os.path.basename(s.rstrip("/"))) for s in plain[:-1]]
    if cmd in {"sed", "gsed", "perl", "ruby"} and any(re.match(r"^-\w*i", a) for a in args):
        return plain[1:]
    if cmd == "dd":
        return [a[3:] for a in args if a.startswith("of=")]
    return []


def bash_targets(command):
    targets = [m for pattern in SCRIPT_WRITES for m in pattern.findall(command)]
    try:
        tokens = tokenize(command)
    except ValueError:
        return targets + re.findall(r">>?\s*['\"]?([^\s'\";|&]+)", command)
    for words in segments(tokens):
        kept = []
        i = 0
        while i < len(words):
            if words[i] in REDIRECTS:
                if i + 1 < len(words):
                    targets.append(words[i + 1])
                i += 2
            elif words[i] in {"<", "<<", "<<<", "<<-", ">&", "<&"}:
                i += 2
            else:
                kept.append(words[i])
                i += 1
        targets.extend(segment_targets(kept))
    return [t for t in targets if "$" not in t and not t.startswith("/dev/")]


PR_READ_ONLY = {"view", "list", "diff", "checks", "status", "checkout"}
MCP_READ_ONLY = re.compile(r"^(get|list|search|read|view)", re.I)
MCP_BANNED = re.compile(r"(pull_?request|merge_?request|workflow)", re.I)
API_BANNED = re.compile(r"/pulls\b|/merge_requests\b|/dispatches\b|/rerun\b|\b(createPullRequest|mergePullRequest)\b")
API_WRITES = re.compile(r"(^|\s)(-[fFd]|--field|--raw-field|--input|--data\S*|-X\s*(POST|PUT|PATCH|DELETE)|--method[ =](POST|PUT|PATCH|DELETE)|--request[ =](POST|PUT|PATCH|DELETE))(\s|=|$)|createPullRequest|mergePullRequest")


def banned_words(words):
    """Return (what, kind) if one shell command is a PR or CI action."""
    while words and (re.match(r"^\w+=", words[0]) or words[0] in {"sudo", "command", "env", "time", "exec", "xargs"}):
        words = words[1:]
    if not words:
        return None
    cmd, args = os.path.basename(words[0]), words[1:]
    if cmd in {"sh", "bash", "zsh", "dash"} and "-c" in args[:-1]:
        return banned_action(args[args.index("-c") + 1])
    if cmd == "eval":
        return banned_action(" ".join(args))

    plain, skip = [], False
    for arg in args:
        if skip:
            skip = False
        elif arg in {"-R", "--repo", "--hostname"}:
            skip = True
        elif not arg.startswith("-"):
            plain.append(arg)
    head, sub = (plain + ["", ""])[:2]
    label = " ".join([cmd, head, sub]).strip()
    joined = " ".join(args)

    if cmd == "gh" and head == "pr" and sub not in PR_READ_ONLY:
        return label, "a pull request action"
    if cmd == "glab" and head == "mr" and sub not in PR_READ_ONLY:
        return label, "a merge request action"
    if cmd == "hub" and head == "pull-request":
        return label, "a pull request action"
    if cmd == "gh" and ((head == "workflow" and sub in {"run", "enable"}) or (head == "run" and sub == "rerun")):
        return label, "a CI action"
    if cmd == "glab" and head == "ci" and sub in {"run", "retry", "trigger"}:
        return label, "a CI action"
    if ((cmd == "gh" and head == "api") or cmd in {"curl", "http", "https"}) and (
        API_BANNED.search(joined) and API_WRITES.search(joined)
    ):
        return f"{cmd} write to a pull request or CI endpoint", "a pull request or CI action"
    return None


def banned_action(command):
    try:
        tokens = tokenize(command)
    except ValueError:
        match = re.search(r"\b(gh\s+pr\s+(?!view|list|diff|checks|status|checkout)\w+|gh\s+workflow\s+run|hub\s+pull-request)", command)
        return (match.group(1), "a pull request or CI action") if match else None
    for words in segments(tokens):
        found = banned_words(words)
        if found:
            return found
    return None


def banned_tool(tool, tool_input):
    if tool == "Bash":
        return banned_action(tool_input.get("command") or "")
    if tool.startswith("mcp__"):
        name = tool.rsplit("__", 1)[-1]
        if MCP_BANNED.search(name) and not MCP_READ_ONLY.match(name):
            return name, "a pull request or CI action"
    return None


def apply_patch_files(patch):
    """Return {path: added text} from a Codex apply_patch payload."""
    files, current = {}, None
    for line in patch.splitlines():
        m = re.match(r"^\*\*\* (?:Add File|Update File|Move to): (.+)$", line)
        if m:
            current = m.group(1).strip()
            files.setdefault(current, "")
        elif current and line.startswith("+"):
            files[current] += line[1:] + "\n"
    return files


def writes_in(tool, tool_input):
    """Return [(path, new text or None)] for a tool call."""
    if tool == "apply_patch":
        return list(apply_patch_files(tool_input.get("command") or "").items())
    if tool == "Bash":
        return [(t, None) for t in bash_targets(tool_input.get("command") or "")]
    if tool == "Write":
        return [(tool_input.get("file_path"), tool_input.get("content"))]
    if tool == "Edit":
        return [(tool_input.get("file_path"), tool_input.get("new_string"))]
    if tool == "MultiEdit":
        added = "\n".join(e.get("new_string", "") for e in tool_input.get("edits", []))
        return [(tool_input.get("file_path"), added)]
    if tool == "NotebookEdit":
        return [(tool_input.get("notebook_path"), tool_input.get("new_source"))]
    if tool.startswith("mcp__"):
        path = tool_input.get("path") or tool_input.get("file_path")
        return [(path, tool_input.get("content"))]
    return []


# Session state: what was already dirty before the agent touched anything -----


def git(root, *args):
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=30)
    return done.stdout if done.returncode == 0 else ""


def file_hash(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def dirty_guarded(root):
    """Return {relative path: hash} for changed or new guarded files in a repo."""
    out = git(root, "status", "--porcelain=v1", "-z", "-uall", "--no-renames")
    found = {}
    for entry in filter(None, out.split("\0")):
        status, rel = entry[:2], entry[3:]
        if "D" in status:
            continue
        if classify(str(root / rel), str(root)):
            found[rel] = file_hash(root / rel)
    return found


def load_state(session):
    try:
        return json.loads((SESSIONS / f"{session}.json").read_text())
    except (OSError, ValueError):
        return {"baselines": {}, "stop_blocks": 0}


def save_state(session, state):
    SESSIONS.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - 30 * 86400
    for old in SESSIONS.glob("*.json"):
        if old.stat().st_mtime < cutoff:
            old.unlink(missing_ok=True)
    (SESSIONS / f"{session}.json").write_text(json.dumps(state))


def ensure_baseline(payload):
    root = repo_root(payload.get("cwd") or ".")
    if not root:
        return None, None
    session = re.sub(r"[^\w.-]", "_", str(payload.get("session_id") or "unknown"))
    state = load_state(session)
    if str(root) not in state["baselines"]:
        state["baselines"][str(root)] = dirty_guarded(root)
        save_state(session, state)
    return session, state


def changed_since_baseline(root, baseline, current):
    return sorted(rel for rel, digest in current.items() if baseline.get(rel) != digest)


# Hook handlers ---------------------------------------------------------------


def record(payload, layer, kind, path):
    HOME.mkdir(parents=True, exist_ok=True)
    entry = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "agent": "codex" if "turn_id" in payload else "claude",
        "layer": layer,
        "kind": kind,
        "path": path,
        "cwd": payload.get("cwd"),
    }
    with LOG.open("a") as handle:
        handle.write(json.dumps(entry) + "\n")


def deny_message(path, kind):
    return (
        f"WONTDO blocked this: {path} is {kind}. Agents may not do {BANNED} here. "
        "Do not retry through another tool, script, file name or location. "
        "Drop this part of the work and carry on with the code. If the user "
        "explicitly asked for it, tell them wontdo blocked it; only they can turn it off."
    )


def deny(reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))


def commit_violation(command, root, baseline):
    if not re.search(r"\bgit\b[^;&|\n]*\bcommit\b", command):
        return None
    args = ["diff", "--name-only", "-z", "--diff-filter=ACMR"]
    everything = re.search(r"\bcommit\b[^;&|\n]*\s(-\w*a\w*|--all)\b", command)
    out = git(root, *args, "HEAD" if everything else "--cached")
    for rel in filter(None, out.split("\0")):
        if classify(str(root / rel), str(root)) and baseline.get(rel) != file_hash(root / rel):
            return rel
    return None


def on_session(payload):
    ensure_baseline(payload)
    print(json.dumps({
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": RULE}
    }))


def on_pre(payload):
    tool = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    cwd = payload.get("cwd") or "."
    session, state = ensure_baseline(payload)

    for path, text in writes_in(tool, tool_input):
        if not path:
            continue
        kind = classify(path, cwd) or content_kind(path, text)
        if kind:
            record(payload, "pre", kind, path)
            deny(deny_message(path, kind))
            return

    action = banned_tool(tool, tool_input)
    if action:
        record(payload, "pre", action[1], action[0])
        deny(deny_message(*action))
        return

    root = repo_root(cwd)
    if tool == "Bash" and root and state:
        baseline = state["baselines"].get(str(root), {})
        rel = commit_violation(tool_input.get("command") or "", root, baseline)
        if rel:
            record(payload, "commit", "commit", rel)
            deny(deny_message(rel, "a banned file staged for commit"))


def on_stop(payload):
    root = repo_root(payload.get("cwd") or ".")
    if not root:
        return
    session, state = ensure_baseline(payload)
    baseline = state["baselines"].get(str(root), {})
    changed = changed_since_baseline(root, baseline, dirty_guarded(root))
    if not changed:
        if state.get("stop_blocks"):
            state["stop_blocks"] = 0
            save_state(session, state)
        return

    listing = ", ".join(changed)
    if state.get("stop_blocks", 0) >= STOP_BLOCK_LIMIT:
        record(payload, "gave-up", "unremoved", listing)
        print(json.dumps({"systemMessage": f"WONTDO: agent left these behind, review them: {listing}"}))
        return

    state["stop_blocks"] = state.get("stop_blocks", 0) + 1
    save_state(session, state)
    for rel in changed:
        record(payload, "stop", classify(str(root / rel), str(root)), rel)
    print(json.dumps({
        "decision": "block",
        "reason": (
            f"WONTDO: this session changed files it may not write: {listing}. "
            f"Agents may not do {BANNED}. Undo only the changes you made: delete files you "
            "created and reverse your own edits by hand. Do not use git restore, git checkout "
            "or git stash, because these files may hold the user's uncommitted work. If you "
            "did not make a change, leave it alone and say so. Then finish."
        ),
    }))


def status():
    print("wontdo is", "OFF for this session (WONTDO_OFF)" if os.environ.get("WONTDO_OFF") else "ON")
    try:
        lines = LOG.read_text().splitlines()
    except OSError:
        lines = []
    print(f"{len(lines)} blocked so far")
    for line in lines[-20:]:
        e = json.loads(line)
        print(f"{e['at']}  {e['agent']:<6} {e['layer']:<7} {e['path']}  ({e['cwd']})")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "status":
        return status()
    if os.environ.get("WONTDO_OFF"):
        return
    handler = {"session": on_session, "pre": on_pre, "stop": on_stop}.get(mode)
    if handler:
        handler(json.load(sys.stdin))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # A bug here must never stop the agent working.
        print(f"wontdo error: {error}", file=sys.stderr)
        sys.exit(1)
