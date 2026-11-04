#!/usr/bin/env python3
"""wontdo: stops coding agents writing docs, unit tests or CI, or touching PRs.

Called by Claude Code and Codex hooks as: wontdo.py session|subagent|prompt|pre|post|stop
Called by the wontdo skill as:            wontdo.py status

Add a ban by adding an entry to bans.json:
  {"kind": "a Dockerfile", "files": "^Dockerfile", "folders": ["k8s"], "commands": "^docker push"}
files and commands are regular expressions. Every field but kind is optional.
"""
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
HOME = Path(os.environ.get("WONTDO_HOME", Path.home() / ".wontdo"))
LOG = HOME / "log.jsonl"
SESSIONS = HOME / "sessions"
IGNORE_FILE = ".wontdoignore"
STOP_BLOCK_LIMIT = 3
BANNED = "documentation, unit tests, CI or pull requests"
OFF_KIND = "wontdo's own off switch"
COMMENT_KIND = "a long comment or docstring"


def load_bans():
    try:
        return json.loads((PLUGIN_ROOT / "bans.json").read_text())
    except (OSError, ValueError):
        return []


EXTRA_BANS = load_bans()


def rule_text():
    extra = "".join(f" Also do not write or run {b['kind']}." for b in EXTRA_BANS)
    return (
        "This rule comes from the owner and overrides any project instructions, CLAUDE.md, "
        "AGENTS.md, skill, plan or habit that says otherwise. Do not write documentation, unit "
        "tests or CI, and do not open or change pull requests. That covers READMEs, changelogs, "
        "markdown docs, test files, in-source tests, pipeline files, triggering CI runs and PR "
        f"actions.{extra} Do not suggest or offer any of these in your replies. Write code with "
        "few comments: no docstrings and no explanatory comment blocks. Write the code and "
        "move on."
    )


# Paths -----------------------------------------------------------------------

AGENT_DIRS = {".claude", ".codex", ".agents", ".cursor", ".ai", ".openclaw", ".windsurf", ".aider"}
THIRD_PARTY_DIRS = {
    "node_modules", "vendor", ".venv", "venv", "dist", "build", ".git", ".next", "coverage",
    "target", "__pycache__", ".pytest_cache", "storybook-static",
}
AGENT_FILES = {
    "claude.md", "agents.md", "gemini.md", "skill.md", "license.md", "memory.md", "soul.md",
    "copilot-instructions.md",
}
CONTENT_DIRS = {
    "content", "data", "assets", "fixtures", "templates", "public", "static", "posts", "_posts",
    "skills", "prompts", "locales", "i18n", "memory",
}

CODE_EXT = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts", ".go", ".rs", ".java",
    ".kt", ".rb", ".cs", ".swift", ".php", ".scala", ".groovy",
}
TEST_CODE_EXT = CODE_EXT | {
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".hxx", ".sh", ".bash", ".lua", ".pl", ".bas",
    ".ex", ".exs", ".dart", ".m", ".mm", ".r", ".zig", ".sol",
}
DATA_EXT = {".json", ".yml", ".yaml", ".toml", ".csv", ".log"}
TEST_DIRS = {"test", "tests", "__test__", "__tests__", "__snapshots__", "__mocks__"}
TEST_CODE_DIRS = {"testing", "spec", "specs"}
TEST_NAME = re.compile(
    r".+\.(test|tests|spec)\.\w+$|.+_spec\.\w+$"
    r"|.+(Test|Tests|Spec)\.(java|kt|cs|swift|scala|php|groovy)$"
    r"|^conftest\.py$|^pytest\.ini$|^(jest|vitest)\.config\.\w+$"
)
TEST_CODE_NAME = re.compile(r"^test_.+|^tests?\.\w+$|.+_test\.\w+$")
E2E = re.compile(r"e2e|cypress|playwright", re.I)

DOC_EXT = {".md", ".mdx", ".markdown", ".rst", ".adoc", ".asciidoc"}
DOC_STEM = {
    "readme", "changelog", "changes", "history", "contributing", "authors", "usage",
    "notes", "guide", "tutorial", "faq", "installation",
}
DOC_STEM_EXT = {"", ".txt", ".html", ".htm", ".pdf"}
DOC_DIRS = {"doc", "docs", "documentation", "guide", "guides", "manual"}
DOC_PAGE_EXT = {".txt", ".html", ".htm"}
TOP_DOC_DIRS = {"doc", "docs"}
DOC_TOOL_FILE = re.compile(
    r"^(mkdocs\.ya?ml|\.readthedocs\.ya?ml|typedoc\.json|jsdoc\.json|docusaurus\.config\.\w+)$", re.I
)

CI_FILES = {
    ".gitlab-ci.yml", ".travis.yml", ".drone.yml", "appveyor.yml", ".appveyor.yml",
    "azure-pipelines.yml", "bitbucket-pipelines.yml", "cloudbuild.yml", ".woodpecker.yml",
    ".cirrus.yml", "bitrise.yml", ".pre-commit-config.yml",
}
CI_DIRS = {".circleci", ".buildkite", ".semaphore", ".woodpecker", ".teamcity", ".tekton"}
CI_WORKFLOW_PARENTS = {".github", ".gitea", ".forgejo"}

SWITCH_WORDS = re.compile(
    r"wontdo|enabledPlugins|disableAllHooks|allowManagedHooksOnly|\bhooks\s*=|hooks\.state", re.I
)


def repo_root(start):
    p = Path(start).resolve()
    for candidate in [p, *p.parents]:
        marker = candidate / ".git"
        if marker.is_file() or (marker / "HEAD").exists():
            return candidate
    return None


def temp_dirs():
    dirs = ["/tmp", "/var/tmp", "/private/tmp", os.environ.get("TMPDIR", "")]
    return [os.path.realpath(d) for d in dirs if d]


def ignored(rel, root):
    try:
        lines = (root / IGNORE_FILE).read_text().splitlines()
    except OSError:
        return False
    globs = [l.strip() for l in lines if l.strip() and not l.startswith("#")]
    return any(fnmatch.fnmatch(rel, g) for g in globs)


def absolute_path(path, cwd):
    return os.path.normpath(os.path.join(cwd, os.path.expanduser(path)))


def inside(absolute, folder):
    folder = str(folder)
    return absolute == folder or absolute.startswith(folder + "/")


def is_wontdo_file(absolute):
    home = Path.home()
    folders = [PLUGIN_ROOT, HOME, home / ".claude" / "plugins", home / ".codex" / "plugins"]
    real = os.path.realpath(absolute)
    return any(inside(absolute, f) or inside(real, os.path.realpath(f)) for f in folders)


def is_agent_settings(absolute):
    parent, name = os.path.split(absolute)
    folder = os.path.basename(parent)
    if folder == ".claude":
        return bool(re.match(r"^settings.*\.json$", name))
    return folder == ".codex" and name in {"config.toml", "hooks.json", "requirements.toml"}


def settings_text(tool, tool_input, absolute):
    """Return what an edit touches in a settings file, or None if it can't be known."""
    if tool == "Edit":
        return (tool_input.get("old_string") or "") + "\n" + (tool_input.get("new_string") or "")
    if tool == "MultiEdit":
        edits = tool_input.get("edits", [])
        return "\n".join(e.get("old_string", "") + "\n" + e.get("new_string", "") for e in edits)
    if tool == "apply_patch":
        return tool_input.get("command") or ""
    if tool == "Write":
        try:
            current = Path(absolute).read_text()
        except OSError:
            current = ""
        return current + "\n" + (tool_input.get("content") or "")
    return None


def off_switch(path, cwd, tool, tool_input):
    absolute = absolute_path(path, cwd)
    if is_wontdo_file(absolute):
        return OFF_KIND
    if is_agent_settings(absolute):
        text = settings_text(tool, tool_input, absolute)
        if text is None or SWITCH_WORDS.search(text):
            return OFF_KIND
    return None


def extra_kind(rel_parts):
    dirs, name = rel_parts[:-1], rel_parts[-1]
    for ban in EXTRA_BANS:
        if ban.get("files") and re.search(ban["files"], name):
            return ban["kind"]
        if set(ban.get("folders", [])) & set(dirs):
            return ban["kind"]
    return None


def test_kind(dirs, name, ext):
    if any(E2E.search(d) for d in dirs) or E2E.search(name):
        return None
    code = ext.lower() in TEST_CODE_EXT
    if set(dirs) & TEST_DIRS or (code and set(dirs) & TEST_CODE_DIRS):
        return "a unit test"
    if TEST_NAME.match(name) or (code and TEST_CODE_NAME.match(name)):
        return "a unit test"
    return None


def doc_kind(dirs, name, stem, ext, in_repo):
    if name.lower() in AGENT_FILES:
        return None
    lower_dirs = {d.lower() for d in dirs}
    ext = ext.lower()
    if in_repo and dirs and dirs[0] in TOP_DOC_DIRS and ext not in CODE_EXT and ext not in DATA_EXT:
        return "documentation"
    if DOC_TOOL_FILE.match(name):
        return "documentation"
    if stem.lower() in DOC_STEM and (ext in DOC_STEM_EXT or ext in DOC_EXT):
        return "documentation"
    if ext in DOC_EXT and (lower_dirs & DOC_DIRS or not lower_dirs & CONTENT_DIRS):
        return "documentation"
    if lower_dirs & DOC_DIRS and ext in DOC_PAGE_EXT:
        return "documentation"
    return None


def classify(path, cwd):
    """Return what kind of banned file a path is, or None."""
    absolute = os.path.realpath(absolute_path(path, cwd))
    root = repo_root(os.path.dirname(absolute))
    if root:
        rel = absolute[len(str(root)) + 1:]
    elif any(inside(absolute, d) for d in temp_dirs()):
        return None
    else:
        rel = absolute
    parts = rel.strip("/").split("/")
    dirs, name = parts[:-1], parts[-1]

    if name == IGNORE_FILE:
        return "wontdo config"
    if set(dirs) & (AGENT_DIRS | THIRD_PARTY_DIRS):
        return None
    if root and ignored(rel, root):
        return None
    kind = extra_kind(parts)
    if kind:
        return kind
    in_workflows = any(
        a in CI_WORKFLOW_PARENTS and b in {"workflows", "actions"} for a, b in zip(dirs, dirs[1:])
    )
    ci_name = re.sub(r"\.yaml$", ".yml", name.lower())
    if in_workflows or set(dirs) & CI_DIRS or ci_name in CI_FILES or re.match(r"^jenkinsfile(\.|$)", ci_name):
        return "CI config"
    stem, ext = os.path.splitext(name)
    return test_kind(dirs, name, ext) or doc_kind(dirs, name, stem, ext, bool(root))


# File content ----------------------------------------------------------------

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
COMMENT_LINE = re.compile(r"^\s*(#(?!!)|//|/\*|\*|--\s)")
KEEP_COMMENT = re.compile(r"copyright|license|spdx|eslint|noqa|pragma|type:\s*ignore", re.I)
LONG_COMMENT_LINES = 6
LONG_DOCSTRING_LINES = 4


def content_kind(path, text):
    return "a unit test" if text and IN_SOURCE_TEST.search(text) else None


def new_lines(tool, tool_input, absolute, text):
    if tool == "Edit":
        before = tool_input.get("old_string") or ""
    elif tool == "MultiEdit":
        before = "\n".join(e.get("old_string", "") for e in tool_input.get("edits", []))
    elif tool == "Write":
        try:
            before = Path(absolute).read_text(errors="ignore")
        except OSError:
            before = ""
    else:
        before = ""
    old = set(before.splitlines())
    return [line for line in (text or "").splitlines() if line not in old]


def has_long_comment(lines):
    run = []
    for line in lines + [""]:
        if COMMENT_LINE.match(line):
            run.append(line)
            continue
        if len(run) >= LONG_COMMENT_LINES and not KEEP_COMMENT.search("\n".join(run)):
            return True
        run = []
    inside_doc, count = None, 0
    for line in lines:
        quotes = re.findall(r'"""|\'\'\'', line)
        if inside_doc:
            count += 1
            if inside_doc in quotes:
                if count >= LONG_DOCSTRING_LINES:
                    return True
                inside_doc = None
        elif len(quotes) == 1 and re.match(r'^\s*[rRbBuU]?("""|\'\'\')', line):
            inside_doc, count = quotes[0], 1
    return False


def comment_kind(path, cwd, tool, tool_input, text):
    if not text or os.path.splitext(path)[1].lower() not in TEST_CODE_EXT:
        return None
    lines = new_lines(tool, tool_input, absolute_path(path, cwd), text)
    return COMMENT_KIND if has_long_comment(lines) else None


# Shell commands --------------------------------------------------------------

HEREDOC = re.compile(r"(<<-?\s*['\"]?(\w+)['\"]?[^\n]*)\n.*?\n\s*\2[ \t]*(?=\n|$)", re.S)
REDIRECTS = {">", ">>", ">|", "&>", "&>>", "<>"}
SEPARATORS = {";", "&&", "||", "|", "|&", "&", "(", ")", ";;"}
WRAPPERS = {"sudo", "command", "env", "time", "exec", "nohup", "xargs"}
SCRIPT_WRITES = [
    re.compile(r"""open\(\s*['"]([^'"]+)['"]\s*,\s*(?:mode\s*=\s*)?['"][^'"]*[wax+]"""),
    re.compile(r"""['"]([^'"]+)['"]\s*\)\s*\.\s*write_(?:text|bytes)"""),
    re.compile(r"""(?:writeFile(?:Sync)?|appendFile(?:Sync)?|createWriteStream|Bun\.write)\(\s*['"]([^'"]+)"""),
]
PR_READ_ONLY = {"view", "list", "diff", "checks", "status", "checkout"}
MCP_READ_ONLY = re.compile(r"^(get|list|search|read|view)", re.I)
MCP_BANNED = re.compile(r"(pull_?request|merge_?request|workflow)", re.I)
API_BANNED = re.compile(r"/pulls\b|/merge_requests\b|/dispatches\b|/rerun\b|\b(createPullRequest|mergePullRequest)\b")
RAW_API_WRITE = re.compile(
    r"(/pulls\b|/merge_requests\b|/dispatches\b).*(\.post\(|--post|method\W{1,4}POST|-X\s*POST)"
    r"|(\.post\(|--post|method\W{1,4}POST|-X\s*POST).*(/pulls\b|/merge_requests\b|/dispatches\b)"
    r"|merge_request\.create",
    re.I | re.S,
)
GH_COMMANDS = {
    "alias", "api", "attestation", "auth", "browse", "cache", "codespace", "completion",
    "config", "extension", "gist", "gpg-key", "help", "issue", "label", "org", "pr",
    "project", "release", "repo", "ruleset", "run", "search", "secret", "ssh-key",
    "status", "variable", "workflow",
}
API_WRITES = re.compile(r"(^|\s)(-[fFd]|--field|--raw-field|--input|--data\S*|-X\s*(POST|PUT|PATCH|DELETE)|--method[ =](POST|PUT|PATCH|DELETE)|--request[ =](POST|PUT|PATCH|DELETE))(\s|=|$)|createPullRequest|mergePullRequest")
SCRIPT_PR = re.compile(
    r"\bgh\s+(-R\s+\S+\s+|--repo[ =]\S+\s+)?pr\s+(create|merge|close|edit|reopen|ready|comment|review)\b"
    r"|\bgh\s+workflow\s+(run|enable)\b|\bgh\s+run\s+rerun\b"
    r"|\bhub\s+pull-request\b|\bglab\s+mr\s+(create|merge|update|close)\b|\bglab\s+ci\s+(run|retry|trigger)\b"
    r"|\b(createPullRequest|mergePullRequest)\b|\.pulls\.create\b|\.create_pull\b"
)
INTERPRETERS = {
    "python", "python3", "node", "bash", "sh", "zsh", "dash", "ruby", "perl", "deno", "bun",
    "tsx", "ts-node",
}
NESTED_RUN = re.compile(r"^\s*(\./|bash |sh |make |npm run |pnpm |yarn |python3? )")
MAX_SCAN_DEPTH = 3


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


def strip_wrappers(words):
    while words and (re.match(r"^\w+=", words[0]) or words[0] in WRAPPERS):
        words = words[1:]
    return words


def segment_targets(words):
    words = strip_wrappers(words)
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


def walk_command(command, cwd):
    """Yield (working folder, words) for each simple command, following cd."""
    here = cwd
    for words in segments(tokenize(command)):
        bare = strip_wrappers(words)
        if bare[:1] == ["cd"] and len(bare) > 1 and "$" not in bare[1]:
            here = absolute_path(bare[1], here)
            continue
        yield here, words


def bash_targets(command, cwd):
    """Return absolute paths a shell command writes to, where the text shows them."""
    targets = []
    try:
        here = cwd
        for here, words in walk_command(command, cwd):
            kept = []
            i = 0
            while i < len(words):
                if words[i] in REDIRECTS:
                    if i + 1 < len(words):
                        targets.append(absolute_path(words[i + 1], here))
                    i += 2
                elif words[i] in {"<", "<<", "<<<", "<<-", ">&", "<&"}:
                    i += 2
                else:
                    kept.append(words[i])
                    i += 1
            targets.extend(absolute_path(t, here) for t in segment_targets(kept) if "$" not in t)
        scripted = [m for pattern in SCRIPT_WRITES for m in pattern.findall(command)]
        targets.extend(absolute_path(t, here) for t in scripted)
    except ValueError:
        targets.extend(absolute_path(t, cwd) for t in re.findall(r">>?\s*['\"]?([^\s'\";|&]+)", command))
    return [t for t in targets if "$" not in t and not t.startswith("/dev/")]


def bash_roots(command, cwd):
    """Return every repo a shell command appears to touch."""
    roots, candidates = set(), [cwd]
    try:
        for here, words in walk_command(command, cwd):
            candidates.append(here)
            for word in words[:40]:
                if word.startswith(("/", "~", "../", "./")) or "/" in word:
                    candidates.append(absolute_path(word, here))
    except ValueError:
        pass
    for path in candidates:
        if "$" in path or "*" in path:
            continue
        root = repo_root(path if os.path.isdir(path) else os.path.dirname(path))
        if root:
            roots.add(root)
    return roots


def read_small(path):
    try:
        p = Path(path)
        return p.read_text(errors="ignore") if p.is_file() and p.stat().st_size < 1_000_000 else ""
    except OSError:
        return ""


def make_recipe(makefile, target):
    current, found = None, []
    for line in read_small(makefile).splitlines():
        head = re.match(r"^([\w./%-]+)\s*:(?!=)", line)
        if head and not line.startswith("\t"):
            if current is None and not target and not head.group(1).startswith("."):
                target = head.group(1)
            current = head.group(1)
        elif line.startswith("\t") and current == target:
            found.append(line.strip().lstrip("@-+"))
    return found


def script_action(cmd, words, args, plain, here, depth):
    """Read a script, make target or npm script before it runs."""
    if depth >= MAX_SCAN_DEPTH:
        return None
    file_path = None
    if cmd in INTERPRETERS and plain and not {"-c", "-e", "-m"} & set(args):
        file_path = absolute_path(plain[0], here)
    elif "/" in words[0]:
        file_path = absolute_path(words[0], here)
    if file_path:
        text = read_small(file_path)
        if SCRIPT_PR.search(text) or RAW_API_WRITE.search(text):
            return f"{os.path.basename(file_path)} opens or changes a pull request", "a pull request or CI action"
        for line in text.splitlines():
            if NESTED_RUN.match(line):
                found = banned_action(line, here, depth + 1)
                if found:
                    return found
        return None
    if cmd in {"make", "gmake"}:
        for line in make_recipe(os.path.join(here, "Makefile"), plain[0] if plain else None):
            found = banned_action(line, here, depth + 1)
            if found:
                return f"make {plain[0] if plain else ''}: {found[0]}", found[1]
        return None
    if cmd in {"npm", "pnpm", "yarn", "bun"}:
        name = plain[1] if plain[:1] == ["run"] and len(plain) > 1 else (plain[0] if plain and cmd != "npm" else None)
        if not name:
            return None
        try:
            scripts = json.loads(read_small(os.path.join(here, "package.json")) or "{}").get("scripts", {})
        except ValueError:
            return None
        if name in scripts:
            found = banned_action(scripts[name], here, depth + 1)
            if found:
                return f"{cmd} run {name}: {found[0]}", found[1]
    return None


def banned_words(words, here, depth):
    """Return (what, kind) if one shell command is a banned action."""
    words = strip_wrappers(words)
    if not words:
        return None
    cmd, args = os.path.basename(words[0]), words[1:]
    if cmd in {"sh", "bash", "zsh", "dash"} and "-c" in args[:-1]:
        return banned_action(args[args.index("-c") + 1], here, depth)
    if cmd == "eval":
        return banned_action(" ".join(args), here, depth)

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
    full = " ".join(words)

    for ban in EXTRA_BANS:
        if ban.get("commands") and re.search(ban["commands"], full):
            return label, ban["kind"]
    if cmd in {"rm", "mv", "chmod", "chown", "truncate", "unlink", "shred"}:
        if any(is_wontdo_file(absolute_path(a, here)) or is_agent_settings(absolute_path(a, here)) for a in plain):
            return label, OFF_KIND
    if cmd in {"claude", "codex"} and "plugin" in plain:
        removing = {"disable", "uninstall", "remove", "rm"} & set(plain)
        if removing and ("wontdo" in joined or "--all" in args or "-a" in args):
            return label, OFF_KIND
    if cmd == "claude" and "--bare" in args:
        return label, OFF_KIND
    if cmd == "codex" and re.search(r"(--disable[ =]hooks|features\.(codex_)?hooks=false|allow_managed_hooks_only)", joined):
        return label, OFF_KIND
    if cmd == "gh" and head and head not in GH_COMMANDS:
        return label, "an unknown gh command that could open a pull request"
    if cmd == "gh" and ((head == "alias" and sub in {"set", "import"}) or (head == "extension" and sub in {"install", "exec", "create"})):
        return label, "a way to hide a pull request action"
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
    if ((cmd == "gh" and head == "api") or cmd in {"curl", "wget", "http", "https"}) and (
        API_BANNED.search(joined) and API_WRITES.search(joined)
    ):
        return f"{cmd} write to a pull request or CI endpoint", "a pull request or CI action"
    return script_action(cmd, words, args, plain, here, depth)


def banned_action(command, cwd, depth=0):
    if "WONTDO_OFF" in command:
        return "WONTDO_OFF", OFF_KIND
    if RAW_API_WRITE.search(command):
        return "a write to a pull request or CI endpoint", "a pull request or CI action"
    try:
        for here, words in walk_command(command, cwd):
            found = banned_words(words, here, depth)
            if found:
                return found
    except ValueError:
        match = re.search(r"\b(gh\s+pr\s+(?!view|list|diff|checks|status|checkout)\w+|gh\s+workflow\s+run|hub\s+pull-request)", command)
        return (match.group(1), "a pull request or CI action") if match else None
    return None


def banned_tool(tool, tool_input, cwd):
    if tool == "Bash":
        return banned_action(tool_input.get("command") or "", cwd)
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


def writes_in(tool, tool_input, cwd):
    """Return [(path, new text or None)] for a tool call."""
    if tool == "apply_patch":
        return list(apply_patch_files(tool_input.get("command") or "").items())
    if tool == "Bash":
        return [(t, None) for t in bash_targets(tool_input.get("command") or "", cwd)]
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


# Session state ---------------------------------------------------------------


def git(root, *args):
    try:
        done = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, errors="replace", timeout=10,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except subprocess.TimeoutExpired:
        return ""
    return done.stdout if done.returncode == 0 else ""


def file_hash(path):
    try:
        p = Path(path)
        if p.stat().st_size > 20_000_000:
            return f"size:{p.stat().st_size}:{p.stat().st_mtime}"
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return None


def dirty_files(root):
    """Return {relative path: hash} for every changed or new file in a repo."""
    out = git(root, "status", "--porcelain=v1", "-z", "-uall", "--no-renames")
    found = {}
    for entry in filter(None, out.split("\0")):
        status, rel = entry[:2], entry[3:]
        if "D" in status or set(rel.split("/")[:-1]) & THIRD_PARTY_DIRS:
            continue
        found[rel] = file_hash(root / rel)
    return found


def added_text(root, rel, *revisions):
    """Return the lines a diff adds to a file, or all of it if git doesn't track it."""
    out = git(root, "diff", "-U0", *revisions, "--", rel)
    if out:
        return "\n".join(l[1:] for l in out.splitlines() if l.startswith("+") and not l.startswith("+++"))
    return read_small(root / rel)


def banned_change(root, rel, *revisions):
    kind = classify(str(root / rel), str(root))
    if kind:
        return kind
    if os.path.splitext(rel)[1].lower() in CODE_EXT:
        return content_kind(rel, added_text(root, rel, *revisions))
    return None


def session_name(payload):
    return re.sub(r"[^\w.-]", "_", str(payload.get("session_id") or "unknown"))


def load_state(session):
    try:
        return json.loads((SESSIONS / f"{session}.json").read_text())
    except (OSError, ValueError):
        return {"baselines": {}, "stop_blocks": 0}


def save_state(session, state):
    SESSIONS.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - 30 * 86400
    for old in SESSIONS.glob("*"):
        if old.stat().st_mtime < cutoff:
            old.unlink(missing_ok=True)
    tmp = SESSIONS / f".{session}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(state))
    os.replace(tmp, SESSIONS / f"{session}.json")


def ensure_baseline(state, session, roots):
    changed = False
    for root in roots:
        if root and str(root) not in state["baselines"]:
            state["baselines"][str(root)] = dirty_files(root)
            state.setdefault("heads", {})[str(root)] = git(root, "rev-parse", "-q", "--verify", "HEAD").strip()
            state.setdefault("started", {})[str(root)] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            changed = True
    if changed:
        save_state(session, state)


def note_call(session, tool_use_id, event):
    SESSIONS.mkdir(parents=True, exist_ok=True)
    with (SESSIONS / f"{session}.calls").open("a") as handle:
        handle.write(f"{time.time():.3f} {event} {tool_use_id}\n")


def call_windows(session):
    """Return (start, end) for each shell command this session ran."""
    try:
        lines = (SESSIONS / f"{session}.calls").read_text().splitlines()
    except OSError:
        return []
    starts, windows = {}, []
    for line in lines:
        parts = line.split(" ", 2)
        if len(parts) != 3:
            continue
        stamp, event, call = float(parts[0]), parts[1], parts[2]
        if event == "start":
            starts[call] = stamp
        elif call in starts:
            windows.append((starts.pop(call), stamp))
    windows.extend((s, float("inf")) for s in starts.values())
    return windows


def in_windows(stamp, windows):
    return any(start - 2 <= stamp <= end + 2 for start, end in windows)


def is_allowed(path, cwd, state):
    return os.path.realpath(absolute_path(path, cwd)) in state.get("allowed", [])


def dirty_violations(root, baseline, head, windows, state):
    """Return ({path: kind} this session changed, {path: kind} changed by someone else)."""
    mine, others = {}, {}
    for rel, digest in dirty_files(root).items():
        if baseline.get(rel) == digest or is_allowed(str(root / rel), str(root), state):
            continue
        if head and git(root, "rev-parse", "-q", "--verify", f"{head}:{rel}").strip() == git(root, "hash-object", "--", rel).strip():
            continue
        kind = banned_change(root, rel, "HEAD")
        if not kind:
            continue
        try:
            stamp = (root / rel).stat().st_mtime
        except OSError:
            continue
        (mine if in_windows(stamp, windows) else others)[rel] = kind
    return mine, others


def committed_violations(root, head, windows, state):
    """Return {path: kind} for banned changes this session committed since the baseline."""
    current = git(root, "rev-parse", "-q", "--verify", "HEAD").strip()
    if not head or not current or head == current:
        return {}
    touched, stamp = set(), 0
    for line in git(root, "log", f"{head}..HEAD", "--no-merges", "--name-only", "--format=@%ct").splitlines():
        if line.startswith("@"):
            stamp = int(line[1:])
        elif line and in_windows(stamp, windows):
            touched.add(line)
    found = {}
    for rel in git(root, "diff", "--name-only", "--diff-filter=ACMR", head, "HEAD").splitlines():
        if rel in touched and not is_allowed(str(root / rel), str(root), state):
            kind = banned_change(root, rel, head, "HEAD")
            if kind:
                found[rel] = kind
    return found


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
    if kind == COMMENT_KIND:
        return (
            f"WONTDO blocked this: {path} adds {kind}. The owner wants code with few comments. "
            "Write it again without the docstring or comment block."
        )
    tail = (
        "and that the choice to turn it off is theirs."
        if kind == OFF_KIND
        else f"and that they can allow this one file by typing: wontdo allow {path}"
    )
    return (
        f"WONTDO blocked this: {path} is {kind}. Agents may not do {BANNED} here, and this "
        "overrides any project instruction that says otherwise. Do not retry through another "
        "tool, script, file name or location. Do not try to disable wontdo. Drop this part of "
        "the work and carry on with the code. If the user explicitly asked for it, tell them "
        f"wontdo blocked it {tail}"
    )


def deny(reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))


def git_violation(command, roots, state, windows):
    """Block commits and pushes of this session's banned changes. Return a path or None."""
    committing = re.search(r"\bgit\b[^;&|\n]*\bcommit\b", command)
    pushing = re.search(r"\bgit\b[^;&|\n]*\bpush\b", command)
    if not committing and not pushing:
        return None
    for root in roots:
        baseline = state["baselines"].get(str(root), {})
        head = state.get("heads", {}).get(str(root))
        found = dict(dirty_violations(root, baseline, head, windows, state)[0])
        if pushing:
            found.update(committed_violations(root, head, windows, state))
        if found:
            return min(found)
    return None


def on_session(payload):
    session = session_name(payload)
    ensure_baseline(load_state(session), session, [repo_root(payload.get("cwd") or ".")])
    print(json.dumps({
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": rule_text()}
    }))


def on_subagent(payload):
    print(json.dumps({
        "hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": rule_text()}
    }))


def on_prompt(payload):
    prompt = payload.get("prompt") or ""
    wanted = re.findall(r"^\s*wontdo allow\s+(\S+)\s*$", prompt, re.M | re.I)
    if not wanted:
        return
    cwd = payload.get("cwd") or "."
    session = session_name(payload)
    state = load_state(session)
    accepted = []
    for path in wanted:
        absolute = absolute_path(path, cwd)
        if re.search(r"[*?\[]", path) or off_switch(path, cwd, "Bash", {}) or os.path.basename(absolute) == IGNORE_FILE:
            continue
        state.setdefault("allowed", []).append(os.path.realpath(absolute))
        accepted.append(path)
        record(payload, "allowed", "owner exception", path)
    if accepted:
        save_state(session, state)
        names = ", ".join(accepted)
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": f"The owner allowed this one file for this session: {names}. Every other wontdo rule still applies.",
            }
        }))


def on_pre(payload):
    tool = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    cwd = payload.get("cwd") or "."
    session = session_name(payload)
    state = load_state(session)
    command = tool_input.get("command") or "" if tool == "Bash" else ""
    roots = bash_roots(command, cwd) if tool == "Bash" else {repo_root(cwd)}
    written = writes_in(tool, tool_input, cwd)
    for path, _ in written:
        if path:
            roots.add(repo_root(os.path.dirname(absolute_path(path, cwd))))
    roots.discard(None)
    ensure_baseline(state, session, roots)

    for path, text in written:
        if not path:
            continue
        kind = off_switch(path, cwd, tool, tool_input)
        if not kind and not is_allowed(path, cwd, state):
            kind = classify(path, cwd) or content_kind(path, text)
        kind = kind or comment_kind(path, cwd, tool, tool_input, text)
        if kind:
            record(payload, "pre", kind, path)
            deny(deny_message(path, kind))
            return

    action = banned_tool(tool, tool_input, cwd)
    if action:
        record(payload, "pre", action[1], action[0])
        deny(deny_message(*action))
        return

    if tool == "Bash":
        rel = git_violation(command, roots, state, call_windows(session))
        if rel:
            record(payload, "commit", "a banned change", rel)
            deny(deny_message(rel, "a banned change, so it can't be committed or pushed"))
            return
        note_call(session, payload.get("tool_use_id") or "x", "start")


def on_post(payload):
    if (payload.get("tool_name") or "") == "Bash":
        note_call(session_name(payload), payload.get("tool_use_id") or "x", "end")


def new_prs(root, state):
    """Return URLs of PRs opened with this account since the session began, once each."""
    started = state.get("started", {}).get(str(root))
    if not started or not shutil.which("gh"):
        return []
    try:
        done = subprocess.run(
            ["gh", "pr", "list", "--author", "@me", "--state", "all", "--limit", "20", "--json", "url,createdAt"],
            cwd=root, capture_output=True, text=True, timeout=8,
        )
        pulls = json.loads(done.stdout) if done.returncode == 0 else []
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []
    seen = state.setdefault("seen_prs", [])
    urls = [p["url"] for p in pulls if p.get("createdAt", "") >= started and p["url"] not in seen]
    seen.extend(urls)
    return urls


def on_stop(payload):
    session = session_name(payload)
    state = load_state(session)
    ensure_baseline(state, session, [repo_root(payload.get("cwd") or ".")])
    windows = call_windows(session)
    mine, others = {}, {}
    for key, baseline in state["baselines"].items():
        root = Path(key)
        head = state.get("heads", {}).get(key)
        found, elsewhere = dirty_violations(root, baseline, head, windows, state)
        found = {**found, **committed_violations(root, head, windows, state)}
        mine.update({str(root / rel): kind for rel, kind in found.items()})
        others.update({str(root / rel): kind for rel, kind in elsewhere.items()})

    changed = sorted(mine)
    listing = ", ".join(changed)
    if changed and state.get("stop_blocks", 0) < STOP_BLOCK_LIMIT:
        state["stop_blocks"] = state.get("stop_blocks", 0) + 1
        save_state(session, state)
        for path in changed:
            record(payload, "stop", mine[path], path)
        print(json.dumps({
            "decision": "block",
            "reason": (
                f"WONTDO: this session changed files it may not write: {listing}. "
                f"Agents may not do {BANNED}. Undo only the changes you made: delete files you "
                "created and reverse your own edits by hand. If you committed them, remove them in "
                "a new commit and do not rewrite history. Do not use git restore, git checkout, git "
                "reset or git stash, because these files may hold the user's uncommitted work. If "
                "you did not make a change, leave it alone and say so. Then finish."
            ),
        }))
        return

    notes = []
    if changed:
        record(payload, "gave-up", "unremoved", listing)
        notes.append(f"agent left these behind, review them: {listing}")
    else:
        state["stop_blocks"] = 0
    seen = state.setdefault("mentioned", [])
    fresh = sorted(p for p in others if p not in seen)
    if fresh:
        seen.extend(fresh)
        for path in fresh:
            record(payload, "noticed", others[path], path)
        notes.append("changed outside this agent's commands, nothing was done: " + ", ".join(fresh))
    urls = [url for key in state["baselines"] for url in new_prs(Path(key), state)]
    for url in urls:
        record(payload, "noticed", "a pull request", url)
    if urls:
        notes.append("a pull request was opened during this session: " + ", ".join(urls))
    save_state(session, state)
    if notes:
        print(json.dumps({"systemMessage": "WONTDO: " + "; ".join(notes)}))


def status():
    if os.environ.get("WONTDO_OFF"):
        print("wontdo is off for this session (WONTDO_OFF)")
    try:
        lines = LOG.read_text().splitlines()
    except OSError:
        lines = []
    print(f"{len(lines)} events so far")
    for line in lines[-20:]:
        e = json.loads(line)
        print(f"{e['at']}  {e['agent']:<6} {e['layer']:<7} {e['path']}  ({e['cwd']})")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "status":
        return status()
    if os.environ.get("WONTDO_OFF"):
        return
    handler = {
        "session": on_session, "subagent": on_subagent, "prompt": on_prompt,
        "pre": on_pre, "post": on_post, "stop": on_stop,
    }.get(mode)
    if handler:
        handler(json.load(sys.stdin))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        if sys.argv[1:2] == ["pre"]:  # If wontdo can't check a call, the call doesn't run.
            deny(f"WONTDO could not check this call, so it is blocked: {error}")
        else:
            print(f"wontdo error: {error}", file=sys.stderr)
            sys.exit(1)
