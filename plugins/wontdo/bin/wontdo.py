#!/usr/bin/env python3
"""wontdo: lets the owner ban things agents do, repo by repo.

Called by Claude Code and Codex hooks as: wontdo.py session|subagent|prompt|pre|post|stop
Called by the wontdo skill as:            wontdo.py status

Everything wontdo can forbid is a Ban. A Ban knows its name, what it says to the agent, and which
files, content, shell commands, scripts and MCP tools it covers. Hooks never name a ban: they ask
whether an active ban matches.

Nothing is banned until the owner says so. Bans belong to the repo, not to an agent, so the Claude
and Codex versions of this plugin share them. Commands, with $ instead of / in Codex:
  /wontdo:ban all       /wontdo:ban ci       (stays on in this repo until lifted)
  /wontdo:allow all     /wontdo:allow ci     (lifted in this repo until banned again)
  /wontdo:allow README.md                    (this one file, this session)
  /wontdo:status
The prompt hook applies them, because only the owner's own message reaches it.

Add a new ban by adding an entry to bans.json:
  {"name": "docker", "kind": "a Dockerfile", "files": "^Dockerfile", "folders": ["k8s"], "commands": "^docker push"}
files and commands are regular expressions. name and kind are required.
"""
import collections
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
MAX_CALL_SECONDS = 1800
REPO_BANS = HOME / "repo_bans.json"
OFF_KIND = "wontdo's own off switch"
COMMENT_KIND = "a long comment or docstring"
STATE = None


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
TEST_DIRS = {"test", "tests", "__test__", "__tests__", "__snapshots__", "__mocks__", "unit_tests", "unittests"}
TEST_CODE_DIRS = {"testing", "spec", "specs"}
TEST_NAME = re.compile(
    r".+(Test|Tests)\.(java|kt|cs|swift|scala|php|groovy)$|.+Spec\.(scala|groovy)$"
    r"|^Test[A-Z]\w*\.(java|kt|cs|swift|php)$"
    r"|^conftest\.py$|^pytest\.ini$|^(jest|vitest)\.config\.\w+$"
)
TEST_CODE_NAME = re.compile(r"^test_.+|^tests?\.\w+$|.+[._-](test|tests|spec|unittest)\.\w+$")
E2E_DIR = re.compile(r"(^|[-_.])(e2e|cypress|playwright)($|[-_.])", re.I)
E2E_NAME = re.compile(r"\.e2e[.-]", re.I)

DOC_EXT = {".md", ".mdx", ".markdown", ".rst", ".adoc", ".asciidoc", ".org", ".rdoc"}
DOC_STEM = {
    "readme", "changelog", "changes", "history", "contributing", "authors", "usage",
    "notes", "guide", "tutorial", "faq", "installation", "news", "roadmap", "howto",
}
DOC_STEM_EXT = {"", ".txt"}
PAGE_STEM = re.compile(r"^(readme|changelog)(\.[\w-]+)?\.(txt|html?|pdf)$", re.I)
DOC_DIRS = {"doc", "docs", "documentation", "guide", "guides", "manual"}
DOC_PAGE_EXT = {".txt", ".html", ".htm"}
TOP_DOC_DIRS = {"doc", "docs"}
DOC_TOOL_FILE = re.compile(
    r"^(mkdocs\.ya?ml|\.readthedocs\.ya?ml|typedoc\.json|jsdoc\.json|docusaurus\.config\.\w+)$", re.I
)

CI_FILES = {
    ".gitlab-ci.yml", ".travis.yml", ".drone.yml", "appveyor.yml", ".appveyor.yml",
    "azure-pipelines.yml", "bitbucket-pipelines.yml", "cloudbuild.yml", ".woodpecker.yml",
    ".cirrus.yml", "bitrise.yml", ".pre-commit-config.yml", "circle.yml", "codefresh.yml",
    "action.yml",
}
CI_DIRS = {".circleci", ".buildkite", ".semaphore", ".woodpecker", ".teamcity", ".tekton", ".azuredevops"}
CI_WORKFLOW_PARENTS = {".github", ".gitea", ".forgejo"}

SWITCH_WORDS = re.compile(
    r"wontdo|enabledPlugins|disableAllHooks|allowManagedHooksOnly|hooks\s*=|hooks\.state", re.I
)
OFF_IN_SCRIPT = re.compile(r"\.wontdo\b|\.wontdoignore\b|\.claude/settings|\.codex/config\.toml|disableAllHooks")
OFF_DESTROY = re.compile(
    r"(\brm\s|rmtree|unlink|-delete\b|remove\()[^\n]*(\.wontdo\b|plugins/[^\s'\"]*wontdo)"
    r"|(\.wontdo\b|plugins/[^\s'\"]*wontdo)[^\n]*-delete\b"
)


def repo_root(start):
    try:
        p = Path(start).resolve()
        for candidate in [p, *p.parents]:
            marker = candidate / ".git"
            if marker.is_file() or (marker / "HEAD").exists():
                return candidate
    except OSError:
        pass
    return None


def temp_dirs():
    dirs = ["/tmp", "/var/tmp", "/private/tmp", os.environ.get("TMPDIR", "")]
    return [os.path.realpath(d) for d in dirs if d]


def ignored(rel, root):
    """Honour the ignore file as it was when the session began, so agents can't widen it."""
    text = read_small(root / IGNORE_FILE) if STATE is None else STATE.get("ignores", {}).get(str(root), "")
    globs = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
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
    if os.path.basename(absolute) == IGNORE_FILE:
        return True
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
        if "false" in text.lower() and "wontdo" in read_small(absolute):
            return OFF_KIND
    return None


def test_kind(dirs, name, ext):
    if any(E2E_DIR.search(d) for d in dirs) or E2E_NAME.search(name):
        return None
    code = ext.lower() in TEST_CODE_EXT
    lower_dirs = {d.lower() for d in dirs}
    if lower_dirs & TEST_DIRS or (code and lower_dirs & TEST_CODE_DIRS):
        return "a unit test"
    if ext == ".t" and "t" in lower_dirs:
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
    if stem.lower() in {"readme", "changelog", "contributing"} and ext in DOC_EXT:
        return "documentation"
    if (stem.lower() in DOC_STEM and ext in DOC_STEM_EXT) or name == "INSTALL" or PAGE_STEM.match(name):
        return "documentation"
    if ext in DOC_EXT and (lower_dirs & DOC_DIRS or not lower_dirs & CONTENT_DIRS):
        return "documentation"
    if lower_dirs & DOC_DIRS and ext in DOC_PAGE_EXT:
        return "documentation"
    return None


def classify(path, cwd):
    """Return the active Ban that covers a path, or None."""
    absolute = os.path.realpath(absolute_path(path, cwd))
    root = repo_root(os.path.dirname(absolute))
    if root:
        rel = absolute[len(str(root)) + 1:]
    elif any(inside(absolute, d) for d in temp_dirs()):
        return None
    else:
        real_cwd = os.path.realpath(cwd)
        rel = absolute[len(real_cwd) + 1:] if inside(absolute, real_cwd) else "/".join(absolute.split("/")[-2:])
    parts = rel.strip("/").split("/")
    dirs, name = parts[:-1], parts[-1]

    if name == IGNORE_FILE:
        return CONFIG if active_bans(root) else None
    if set(dirs) & (AGENT_DIRS | THIRD_PARTY_DIRS):
        return None
    if root and ignored(rel, root):
        return None
    stem, ext = os.path.splitext(name)
    for ban in active_bans(root):
        if ban.file(dirs, name, stem, ext, bool(root)):
            return ban
    return None


# File content ----------------------------------------------------------------

IN_SOURCE_TEST = re.compile(
    r"#\[(cfg\()?test\)?\]|#\[\w+::test\]"
    r"|^\s*(import|from)\s+(pytest|unittest)\b"
    r"|\bclass\s+\w+\((unittest\.)?TestCase\)"
    r"|\bfunc\s+Test[A-Z]\w*\(\w+\s+\*testing\.T\)"
    r"|^\s*@Test\b|^\s*\[(Fact|Theory|Test|TestMethod)\]"
    r"|import\.meta\.vitest|\bDeno\.test\(|\bRSpec\.describe\b"
    r"""|(?<![.\w])(describe|it|test)(\.\w+)?\s*\(\s*['"`][^'"`]*['"`]\s*,\s*(async\s*)?(\(|function)""",
    re.M,
)
PY_TEST_DEF = re.compile(r"^\s*(async\s+)?def\s+test_\w+\s*\(", re.M)
PY_ASSERT = re.compile(r"\bassert\b|\.assert[A-Z]\w*\(|\bpytest\.")
HASH_COMMENT_EXT = {".py", ".rb", ".sh", ".bash", ".pl", ".ex", ".exs", ".r"}
KEEP_COMMENT = re.compile(r"copyright|license|spdx|eslint|noqa|pragma|type:\s*ignore", re.I)
LONG_COMMENT_LINES = 6
LONG_DOCSTRING_LINES = 4


def looks_like_test(path, text):
    if not text or os.path.splitext(path)[1].lower() not in TEST_CODE_EXT:
        return False
    if any(E2E_DIR.search(d) for d in path.split("/")[:-1]) or E2E_NAME.search(path):
        return False
    return bool(IN_SOURCE_TEST.search(text) or (PY_TEST_DEF.search(text) and PY_ASSERT.search(text)))


def content_ban(path, text, root):
    for ban in active_bans(root):
        if ban.content(path, text):
            return ban
    return None


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


def has_long_comment(lines, ext):
    hash_comments = ext in HASH_COMMENT_EXT
    run, in_block = [], False
    for line in lines + [""]:
        stripped = line.strip()
        is_comment = False
        if hash_comments and stripped.startswith("#") and not stripped.startswith("#!"):
            is_comment = True
        elif not hash_comments:
            if stripped.startswith("/*"):
                in_block = True
            if in_block or stripped.startswith("//"):
                is_comment = True
            if "*/" in stripped:
                in_block = False
        if is_comment:
            run.append(line)
            continue
        if len(run) >= LONG_COMMENT_LINES and not KEEP_COMMENT.search("\n".join(run)):
            return True
        run = []
    if ext != ".py":
        return False
    inside_doc, count, before = None, 0, ""
    for line in lines:
        quotes = re.findall(r'"""|\'\'\'', line)
        if inside_doc:
            count += 1
            if inside_doc in quotes:
                if count >= LONG_DOCSTRING_LINES:
                    return True
                inside_doc = None
        elif len(quotes) == 1 and re.match(r'^\s*[rRbBuU]?("""|\'\'\')', line) and (not before or before.endswith(":")):
            inside_doc, count = quotes[0], 1
        if line.strip():
            before = line.strip()
    return False


def comment_kind(path, cwd, tool, tool_input, text, state):
    ext = os.path.splitext(path)[1].lower()
    if not text or ext not in TEST_CODE_EXT or is_allowed(path, cwd, state):
        return None
    absolute = os.path.realpath(absolute_path(path, cwd))
    if DOCS not in active_bans(repo_root(os.path.dirname(absolute))):
        return None
    if set(absolute.split("/")[:-1]) & THIRD_PARTY_DIRS or any(inside(absolute, d) for d in temp_dirs()) and not repo_root(os.path.dirname(absolute)):
        return None
    lines = new_lines(tool, tool_input, absolute_path(path, cwd), text)
    return COMMENT_KIND if has_long_comment(lines, ext) else None


# Shell commands --------------------------------------------------------------

REDIRECTS = {">", ">>", ">|", "&>", "&>>", "<>"}
SEPARATORS = {";", "&&", "||", "|", "|&", "&", "(", ")", ";;"}
CONTROL_WORDS = {"if", "then", "else", "elif", "do", "while", "until", "{", "}", "!", "fi", "done", "time", "command", "exec", "nohup", "builtin", "setsid"}
VALUE_WRAPPERS = {"sudo", "env", "timeout", "nice", "ionice", "stdbuf", "xargs", "flock", "watch"}
WRAPPER_FLAGS_WITH_VALUE = {"-u", "-g", "-n", "-I", "-P", "-L", "-k", "-s", "-c"}
SCRIPT_WRITES = [
    re.compile(r"""open\(\s*['"]([^'"]+)['"]\s*,\s*(?:mode\s*=\s*)?['"][^'"]*[wax+]"""),
    re.compile(r"""['"]([^'"]+)['"]\s*\)\s*\.\s*write_(?:text|bytes)"""),
    re.compile(r"""(?:writeFile(?:Sync)?|appendFile(?:Sync)?|createWriteStream|Bun\.write)\(\s*['"]([^'"]+)"""),
]
PR_READ_ONLY = {"view", "list", "diff", "checks", "status", "checkout"}
MCP_READ_ONLY = re.compile(r"^(get|list|search|read|view)|_(get|list|search|read|view)$", re.I)
API_HOST = r"(api\.github\.com|\brepos/[^\s/'\"]+/[^\s/'\"]+/|gitlab\.[\w.]+/api|\bprojects/[^\s/'\"]+/)[^\s'\"]*"
GRAPHQL_PR = re.compile(r"\b(create|merge|close|update|reopen)PullRequest\b|enablePullRequestAutoMerge|addPullRequestReview")
POST_WORD = re.compile(r"\.post\(|--post|--json|method\W{1,4}POST|-X\s*POST|[\"']POST[\"']")
GH_COMMANDS = {
    "alias", "api", "attestation", "auth", "browse", "cache", "codespace", "completion",
    "config", "copilot", "extension", "gist", "gpg-key", "help", "issue", "label", "models",
    "org", "pr", "project", "release", "repo", "ruleset", "run", "search", "secret", "ssh-key",
    "status", "variable", "version", "workflow",
}
GH_WRITE = re.compile(r"(^|\s)(-[fF]|--field|--raw-field|--input|-X\s*(POST|PUT|PATCH|DELETE)|--method[ =](POST|PUT|PATCH|DELETE))(\s|=|$)")
CURL_WRITE = re.compile(r"(^|\s)(-\w*d\S*|--data\S*|--json|--form|-F|-\w*X\s*(POST|PUT|PATCH|DELETE)|--request[ =](POST|PUT|PATCH|DELETE)|--post\S*)(\s|=|$)")
OTHER_PR_TOOLS = re.compile(r"^(az repos pr (create|update)|tea (pulls?|pr) (create|merge|close)|gt (submit|ss)\b|git town (propose|ship)|git-town (propose|ship))")
SCRIPT_PR = re.compile(
    r"\bgh\s+(-R\s+\S+\s+|--repo[ =]\S+\s+)?pr\s+(create|merge|close|edit|reopen|ready|comment|review)\b"
    r"|['\"]gh['\"]\s*,\s*['\"]pr['\"]\s*,\s*['\"](create|merge|close|edit|reopen|ready|comment|review)['\"]"
    r"|\bhub\s+pull-request\b|\bglab\s+mr\s+(create|merge|update|close)\b"
    r"|\.pulls\.create\b|\.create_pull\b"
)
SCRIPT_CI = re.compile(
    r"\bgh\s+workflow\s+(run|enable)\b|\bgh\s+run\s+rerun\b|\bglab\s+ci\s+(run|retry|trigger)\b"
)
SHELLS = {"bash", "sh", "zsh", "dash"}
INTERPRETERS = SHELLS | {"python", "python3", "node", "ruby", "perl", "deno", "bun", "tsx", "ts-node", "source", "."}
HEREDOC_START = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?")
SUBSTITUTION = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")
NESTED_RUN = re.compile(r"^\s*(\./|bash |sh |make |npm run |pnpm |yarn |python3? )")
MAX_SCAN_DEPTH = 3


def split_heredocs(command):
    """Return the command without heredoc bodies, and [(header line, body)]."""
    kept, bodies, end, header, body = [], [], None, "", []
    for line in command.split("\n"):
        if end is not None:
            if line.strip() == end:
                bodies.append((header, "\n".join(body)))
                end = None
            else:
                body.append(line)
            continue
        kept.append(line)
        match = HEREDOC_START.search(line)
        if match:
            end, header, body = match.group(1), line, []
    if end is not None:
        bodies.append((header, "\n".join(body)))
    return "\n".join(kept), bodies


def tokenize(command):
    text, _ = split_heredocs(command)
    text = "\n".join(l for l in text.split("\n") if not l.lstrip().startswith("#")).replace("\n", " ; ")
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def strip_wrappers(words):
    words = list(words)
    while words:
        if re.match(r"^\w+=", words[0]) or words[0] in CONTROL_WORDS:
            words.pop(0)
        elif words[0] in VALUE_WRAPPERS:
            words.pop(0)
            while words and (words[0].startswith("-") or re.match(r"^\d+\w?$", words[0])):
                flag = words.pop(0)
                if flag in WRAPPER_FLAGS_WITH_VALUE and words:
                    words.pop(0)
        else:
            break
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
        if dest.endswith("/") or os.path.isdir(dest) or len(plain) > 2:
            return [dest] + [os.path.join(dest, os.path.basename(s.rstrip("/"))) for s in plain[:-1]]
        return [dest]
    if cmd in {"sed", "gsed", "perl", "ruby"} and any(re.match(r"^(-i|--in-place|-[a-zA-Z]*i$)", a) for a in args):
        return plain[1:]
    if cmd in {"curl", "wget"}:
        return [args[i + 1] for i, a in enumerate(args[:-1]) if a in {"-o", "-O", "--output"} and cmd == "curl" and a != "-O" or (cmd == "wget" and a == "-O")]
    if cmd == "dd":
        return [a[3:] for a in args if a.startswith("of=")]
    return []


def walk_command(command, cwd):
    """Yield (working folder, words) for each simple command, following cd and subshells."""
    here, stack, current = cwd, [], []
    for token in tokenize(command) + [";"]:
        if token not in SEPARATORS:
            current.append(token)
            continue
        if current:
            bare = strip_wrappers(current)
            if bare[:1] == ["cd"] and len(bare) > 1 and "$" not in bare[1]:
                here = absolute_path(bare[1], here)
            else:
                yield here, current
            current = []
        if token == "(":
            stack.append(here)
        elif token == ")" and stack:
            here = stack.pop()


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
                if len(word) < 400 and (word.startswith(("/", "~", "../", "./")) or "/" in word):
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


def make_recipe(makefile, target, seen=None):
    """Return the recipe lines of a make target and the targets it depends on."""
    seen = seen if seen is not None else set()
    recipes, deps, first = {}, {}, None
    current = None
    for line in read_small(makefile).splitlines():
        head = re.match(r"^([\w./%-]+)\s*:(?!=)(.*)$", line)
        if head and not line.startswith("\t"):
            current = head.group(1)
            deps[current] = head.group(2).split()
            recipes.setdefault(current, [])
            if first is None and not current.startswith("."):
                first = current
        elif line.startswith("\t") and current:
            recipes[current].append(line.strip().lstrip("@-+"))
    target = target or first
    found, queue = [], [target]
    while queue:
        name = queue.pop()
        if name in seen or name not in recipes:
            continue
        seen.add(name)
        found.extend(recipes[name])
        queue.extend(deps[name])
    return found


def scan_script_text(text, here):
    """Return (what, kind, ban name) if script text plainly does something an active ban covers."""
    active = active_bans(repo_root(here))
    lines = [l for l in text.splitlines()[:20000] if not l.lstrip().startswith(("#", "//"))]
    for number, line in enumerate(lines):
        if OFF_IN_SCRIPT.search(line):
            return "a script that edits wontdo or agent settings", OFF_KIND, None
        nearby = None
        for ban in active:
            if nearby is None:
                nearby = "\n".join(lines[max(0, number - 3):number + 4])
            if ban.script(line, nearby):
                return f"a script doing {ban.kind}", ban.kind, ban.name
    return None


def script_action(cmd, words, args, plain, here, depth):
    """Read a script, make target or npm script before it runs."""
    if depth >= MAX_SCAN_DEPTH:
        return None
    if cmd in INTERPRETERS:
        for flag in ("-c", "-e"):
            if flag in args[:-1]:
                return scan_script_text(args[args.index(flag) + 1], here)
    file_path = None
    if cmd in INTERPRETERS and "-m" not in args:
        file_path = next((absolute_path(a, here) for a in plain if os.path.isfile(absolute_path(a, here))), None)
    elif "/" in words[0]:
        file_path = absolute_path(words[0], here)
    if file_path:
        text = read_small(file_path)
        own = is_wontdo_file(file_path) or "wontdo: stops coding agents" in text
        if own:
            rest = [a for a in plain if absolute_path(a, here) != file_path]
            return None if rest == ["status"] and len(args) == len(plain) else (os.path.basename(file_path), OFF_KIND, None)
        found = scan_script_text(text, here)
        if found:
            return found
        for line in text.splitlines():
            if NESTED_RUN.match(line):
                found = banned_action(line, here, depth + 1)
                if found:
                    return found
        return None
    if cmd in {"make", "gmake"}:
        folder, makefile, targets, skip = here, "Makefile", [], None
        for arg in args:
            if skip:
                if skip == "-C":
                    folder = absolute_path(arg, here)
                else:
                    makefile = arg
                skip = None
            elif arg in {"-C", "-f", "--file", "--directory"}:
                skip = "-C" if arg in {"-C", "--directory"} else "-f"
            elif not arg.startswith("-") and "=" not in arg:
                targets.append(arg)
        for target in targets or [None]:
            for line in make_recipe(os.path.join(folder, makefile), target):
                found = banned_action(line, folder, depth + 1)
                if found:
                    return f"make {target or ''}: {found[0]}", found[1], found[2]
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
                return f"{cmd} run {name}: {found[0]}", found[1], found[2]
    return None


def banned_words(words, here, depth):
    """Return (what, kind, ban name) if one shell command is covered by an active ban."""
    original = words
    words = strip_wrappers(words)
    if not words:
        return None
    cmd, args = os.path.basename(words[0]), words[1:]
    if cmd in SHELLS:
        for i, arg in enumerate(args[:-1]):
            if re.match(r"^-[a-zA-Z]*c[a-zA-Z]*$", arg):
                return banned_action(args[i + 1], here, depth)
    if cmd == "eval":
        return banned_action(" ".join(args), here, depth)
    if cmd == "find" and {"-exec", "-execdir"} & set(args):
        start = next(i for i, a in enumerate(args) if a in {"-exec", "-execdir"})
        found = banned_words(args[start + 1:], here, depth)
        if found:
            return found

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
    if cmd == "gh" and {"--help", "-h"} & set(args):
        return None
    if cmd in {"claude", "codex"}:
        moved = any(re.match(r"^(HOME|CLAUDE_CONFIG_DIR|CODEX_HOME)=", w) for w in original)
        if moved or "--settings" in args:
            return label, OFF_KIND, None
    if cmd in {"cp", "ln", "rsync"} and any(is_wontdo_file(absolute_path(a, here)) for a in plain[:-1]):
        return label, OFF_KIND, None
    if cmd in {"rm", "mv", "chmod", "chown", "truncate", "unlink", "shred"}:
        if any(is_wontdo_file(absolute_path(a, here)) or is_agent_settings(absolute_path(a, here)) for a in plain):
            return label, OFF_KIND, None
    if cmd in {"claude", "codex"} and "plugin" in plain:
        removing = {"disable", "uninstall", "remove", "rm"} & set(plain)
        if removing and ("wontdo" in joined or "--all" in args or "-a" in args):
            return label, OFF_KIND, None
    if cmd == "claude" and "--bare" in args:
        return label, OFF_KIND, None
    if cmd == "codex" and re.search(r"(--disable[ =](codex_)?hooks|features\.(codex_)?hooks\s*=\s*false|allow_managed_hooks_only)", joined):
        return label, OFF_KIND, None

    shell = Shell(cmd, args, plain, head, sub, joined, full, label)
    for ban in active_bans(repo_root(here)):
        hit = ban.command(shell)
        if hit:
            return hit, ban.kind, ban.name
    return script_action(cmd, words, args, plain, here, depth)


def banned_action(command, cwd, depth=0):
    if "WONTDO_OFF" in command:
        return "WONTDO_OFF", OFF_KIND, None
    if re.search(r"wontdo:(allow|ban)\b", command) and re.search(r"\b(claude|codex)\b", command):
        return "a child agent told to change wontdo", OFF_KIND, None
    if OFF_DESTROY.search(command):
        return "a command that removes wontdo", OFF_KIND, None
    text, bodies = split_heredocs(command)
    for header, body in bodies:
        before = header[:HEREDOC_START.search(header).start()]
        words = before.replace(";", " ").replace("|", " ").split()
        if any(os.path.basename(w) in INTERPRETERS for w in words):
            found = scan_script_text(body, cwd)
            if found:
                return found
    if re.search(r"\|\s*(ba|z|da)?sh\b", text):
        found = scan_script_text(text, cwd)
        if found:
            return found
    for match in SUBSTITUTION.finditer(text):
        if depth < MAX_SCAN_DEPTH:
            found = banned_action(match.group(1) or match.group(2) or "", cwd, depth + 1)
            if found:
                return found
    try:
        for here, words in walk_command(command, cwd):
            found = banned_words(words, here, depth)
            if found:
                return found
    except ValueError:
        return scan_script_text(command, cwd)
    return None


def banned_tool(tool, tool_input, cwd):
    if tool == "Bash":
        return banned_action(tool_input.get("command") or "", cwd)
    if tool.startswith("mcp__"):
        name = tool.rsplit("__", 1)[-1]
        if not MCP_READ_ONLY.search(name):
            for ban in active_bans(repo_root(cwd)):
                if ban.mcp(name):
                    return name, ban.kind, ban.name
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
        paths = [tool_input.get(k) for k in ("path", "file_path", "filepath", "destination", "target")]
        return [(path, tool_input.get("content")) for path in paths if isinstance(path, str)]
    return []


# Bans ------------------------------------------------------------------------

Shell = collections.namedtuple("Shell", "cmd args plain head sub joined full label")


class Ban:
    """One thing agents won't do. Subclasses say what it covers; hooks only use this interface."""

    name = ""
    kind = ""
    rule = ""
    removals_ok = False

    def file(self, dirs, name, stem, ext, in_repo):
        return False

    def content(self, path, text):
        return False

    def command(self, shell):
        return None

    def script(self, line, nearby):
        return False

    def mcp(self, name):
        return False


class Docs(Ban):
    name, kind = "docs", "documentation"
    rule = "write documentation, such as READMEs, changelogs and markdown docs"

    def file(self, dirs, name, stem, ext, in_repo):
        return bool(doc_kind(dirs, name, stem, ext, in_repo))


class Tests(Ban):
    name, kind = "tests", "a unit test"
    rule = "write unit tests, such as test files and in-source tests (deleting existing tests when asked is fine)"
    removals_ok = True

    def file(self, dirs, name, stem, ext, in_repo):
        return bool(test_kind(dirs, name, ext))

    def content(self, path, text):
        return looks_like_test(path, text)


def is_api_write(shell, tail):
    """Is this shell command a write to a hosted git API path ending in tail?"""
    if shell.cmd in {"gh", "glab", "hub"} and shell.head == "api":
        return bool(re.search(tail, shell.joined) and GH_WRITE.search(shell.joined))
    if shell.cmd in {"curl", "wget", "http", "https"}:
        return bool(re.search(API_HOST + tail, shell.joined) and (CURL_WRITE.search(shell.joined) or POST_WORD.search(shell.joined)))
    return False


class Ci(Ban):
    name, kind = "ci", "a CI change"
    rule = "write CI config or trigger CI runs"
    API_TAIL = r"(/dispatches\b|/rerun\b)"

    def file(self, dirs, name, stem, ext, in_repo):
        pairs = list(zip(dirs, dirs[1:]))
        in_workflows = any(a in CI_WORKFLOW_PARENTS and b in {"workflows", "actions"} for a, b in pairs)
        in_gitlab_ci = any(a == ".gitlab" and b == "ci" for a, b in pairs)
        ci_name = re.sub(r"\.yaml$", ".yml", name.lower())
        return bool(
            in_workflows or in_gitlab_ci or set(dirs) & CI_DIRS or ci_name in CI_FILES
            or re.match(r"^(jenkinsfile(\.|$)|azure-pipelines-)", ci_name)
        )

    def command(self, s):
        if s.cmd == "gh" and ((s.head == "workflow" and s.sub in {"run", "enable"}) or (s.head == "run" and s.sub == "rerun")):
            return s.label
        if s.cmd == "glab" and s.head == "ci" and s.sub in {"run", "retry", "trigger"}:
            return s.label
        if s.cmd == "git" and s.head == "config" and SCRIPT_CI.search(s.joined):
            return s.label
        return f"{s.cmd} write to a CI endpoint" if is_api_write(s, self.API_TAIL) else None

    def script(self, line, nearby):
        if SCRIPT_CI.search(line):
            return True
        return bool(re.search(API_HOST + self.API_TAIL, line) and POST_WORD.search(nearby))

    def mcp(self, name):
        return bool(re.search(r"workflow", name, re.I))


class Prs(Ban):
    name, kind = "prs", "a pull request action"
    rule = "open or change pull requests"
    API_TAIL = r"(pulls\b|/merge_requests\b)"

    def command(self, s):
        if OTHER_PR_TOOLS.match(s.full):
            return s.label
        if s.cmd == "git" and ("merge_request.create" in s.joined or (s.head == "config" and SCRIPT_PR.search(s.joined))):
            return s.label
        if s.cmd == "gh" and s.head and s.head not in GH_COMMANDS:
            return s.label
        if s.cmd == "gh" and ((s.head == "alias" and s.sub in {"set", "import"}) or (s.head == "extension" and s.sub in {"install", "exec", "create"})):
            return s.label
        if s.cmd == "gh" and s.head == "pr" and s.sub not in PR_READ_ONLY:
            return s.label
        if s.cmd == "glab" and s.head == "mr" and s.sub not in PR_READ_ONLY:
            return s.label
        if s.cmd == "hub" and s.head == "pull-request":
            return s.label
        if s.cmd in {"gh", "glab", "hub"} and s.head == "api" and GRAPHQL_PR.search(s.joined):
            return f"{s.cmd} write to a pull request endpoint"
        return f"{s.cmd} write to a pull request endpoint" if is_api_write(s, self.API_TAIL) else None

    def script(self, line, nearby):
        if SCRIPT_PR.search(line) or GRAPHQL_PR.search(line):
            return True
        return bool(re.search(API_HOST + self.API_TAIL, line) and POST_WORD.search(nearby))

    def mcp(self, name):
        return bool(re.search(r"pull_?request|merge_?request|_pr\b|^create_pr|pending_review|request_copilot_review", name, re.I))


class ListedBan(Ban):
    """A ban declared as one entry in bans.json."""

    def __init__(self, entry):
        self.name = entry["name"].lower()
        self.kind = entry["kind"]
        self.rule = f"write or run {self.kind}"
        self.files = entry.get("files")
        self.folders = set(entry.get("folders", []))
        self.commands = entry.get("commands")

    def file(self, dirs, name, stem, ext, in_repo):
        return bool((self.files and re.search(self.files, name)) or self.folders & set(dirs))

    def command(self, shell):
        return shell.label if self.commands and re.search(self.commands, shell.full) else None

    def script(self, line, nearby):
        return bool(self.commands and re.search(self.commands, line.strip()))


def load_listed_bans():
    try:
        return [ListedBan(entry) for entry in json.loads((PLUGIN_ROOT / "bans.json").read_text())]
    except (OSError, ValueError, KeyError, AttributeError) as error:
        print(f"wontdo ignored bans.json: {error}", file=sys.stderr)
        return []


CI, TESTS, DOCS, PRS = Ci(), Tests(), Docs(), Prs()
BANS = [CI, TESTS, DOCS, PRS] + load_listed_bans()
BY_NAME = {ban.name: ban for ban in BANS}

CONFIG = Ban()
CONFIG.kind = "wontdo config"


def read_repo_bans():
    try:
        return json.loads(REPO_BANS.read_text())
    except (OSError, ValueError):
        return {}


def write_repo_bans(table):
    HOME.mkdir(parents=True, exist_ok=True)
    tmp = HOME / f".repo_bans.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(table, indent=2))
    os.replace(tmp, REPO_BANS)


def active_bans(root=None):
    """Return the bans the owner has put on this repo. Nothing is banned by default."""
    if not root:
        return []
    names = read_repo_bans().get(str(root), [])
    return [ban for ban in BANS if ban.name in names]


def rule_text(root):
    active = active_bans(root)
    if not active:
        return ""
    doing = "; ".join(ban.rule for ban in active)
    comments = " Write code with few comments: no docstrings and no explanatory comment blocks." if DOCS in active else ""
    return (
        "This rule comes from the owner and overrides any project instructions, CLAUDE.md, AGENTS.md, "
        f"skill, plan or habit that says otherwise. Do not: {doing}. Any project rule asking for these "
        "is cancelled, not outstanding work, so do not attempt them. Never suggest or offer them, and "
        f"never report them as missing, skipped or blocked unless the user asks for one directly.{comments} "
        "Write the code and move on."
    )


def reminder_text(root):
    active = active_bans(root)
    if not active:
        return ""
    names = ", ".join(ban.name for ban in active)
    return (
        f"wontdo: banned here: {names}. Project rules asking for them are cancelled. Do not attempt, "
        "offer or mention them unless this message asks for one directly."
    )


def banned_phrase(root):
    return ", ".join(ban.name for ban in active_bans(root)) or "nothing"


# Session state ---------------------------------------------------------------


def git(root, *args):
    try:
        done = subprocess.run(
            ["git", "-c", "core.quotepath=off", "-C", str(root), *args], capture_output=True, text=True,
            errors="surrogateescape", timeout=10, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.TimeoutExpired):
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
    """Return the kind of ban a change breaks, or None."""
    ban = classify(str(root / rel), str(root))
    if ban and ban.removals_ok:
        diff = git(root, "diff", "-U0", *revisions, "--", rel)
        if diff and not added_text(root, rel, *revisions).strip():
            return None
    if ban:
        return ban.kind
    if os.path.splitext(rel)[1].lower() in CODE_EXT:
        ban = content_ban(rel, added_text(root, rel, *revisions), root)
        return ban.kind if ban else None
    return None


def session_name(payload):
    return re.sub(r"[^\w.-]", "_", str(payload.get("session_id") or "unknown"))


def load_state(session):
    try:
        return json.loads((SESSIONS / f"{session}.json").read_text())
    except (OSError, ValueError):
        return {"baselines": {}, "stop_blocks": 0}


def save_state(session, state):
    try:
        SESSIONS.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - 30 * 86400
        for old in SESSIONS.glob("*"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink(missing_ok=True)
            except OSError:
                pass
        tmp = SESSIONS / f".{session}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(state))
        os.replace(tmp, SESSIONS / f"{session}.json")
    except OSError as error:
        print(f"wontdo could not save state: {error}", file=sys.stderr)


def use_state(state):
    global STATE
    STATE = state


def ensure_baseline(state, session, roots):
    """Remember what was already changed in each repo that has bans, so it isn't blamed on the agent."""
    changed = False
    for root in roots:
        if root and active_bans(root) and str(root) not in state["baselines"]:
            state["baselines"][str(root)] = dirty_files(root)
            state.setdefault("heads", {})[str(root)] = git(root, "rev-parse", "-q", "--verify", "HEAD").strip()
            state.setdefault("started", {})[str(root)] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            state.setdefault("ignores", {})[str(root)] = read_small(root / IGNORE_FILE)
            changed = True
    if changed:
        save_state(session, state)
    use_state(state)


def note_call(session, tool_use_id, event):
    try:
        SESSIONS.mkdir(parents=True, exist_ok=True)
        with (SESSIONS / f"{session}.calls").open("a") as handle:
            handle.write(f"{time.time():.3f} {event} {tool_use_id}\n")
    except OSError:
        pass


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
    windows.extend((s, s + MAX_CALL_SECONDS) for s in starts.values())
    return windows


def close_open_calls(session):
    """A command still open when the agent finishes has ended, or its result was lost."""
    try:
        lines = (SESSIONS / f"{session}.calls").read_text().splitlines()
    except OSError:
        return
    open_calls = {}
    for line in lines:
        parts = line.split(" ", 2)
        if len(parts) == 3:
            if parts[1] == "start":
                open_calls[parts[2]] = True
            else:
                open_calls.pop(parts[2], None)
    for call in open_calls:
        note_call(session, call, "end")


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
    if not current or head == current:
        return {}
    span = f"{head}..HEAD" if head else "HEAD"
    start = head or "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
    touched, stamp = set(), 0
    for line in git(root, "log", span, "--no-merges", "--name-only", "--format=@%ct").splitlines():
        if line.startswith("@"):
            stamp = int(line[1:])
        elif line and in_windows(stamp, windows):
            touched.add(line)
    found = {}
    for rel in git(root, "diff", "--name-only", "--diff-filter=ACMR", start, "HEAD").splitlines():
        if rel in touched and not is_allowed(str(root / rel), str(root), state):
            kind = banned_change(root, rel, start, "HEAD")
            if kind:
                found[rel] = kind
    return found


# Hook handlers ---------------------------------------------------------------


def record(payload, layer, kind, path):
    try:
        write_log(payload, layer, kind, path)
    except OSError as error:
        print(f"wontdo could not log: {error}", file=sys.stderr)


def write_log(payload, layer, kind, path):
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


def deny_message(path, kind, ban_name=None, cwd=None, is_file=True, mark="/"):
    root = repo_root(cwd) if cwd else None
    if cwd and os.path.isabs(path) and inside(path, cwd):
        path = os.path.relpath(path, cwd)
    if kind == COMMENT_KIND:
        return (
            f"WONTDO blocked this: {path} adds {kind}. The owner wants code with few comments. "
            "Write it again without the docstring or comment block."
        )
    if kind == OFF_KIND or not ban_name:
        tail = "and that the choice to turn it off is theirs."
    elif is_file:
        tail = f"and that they can type {mark}wontdo:allow {path} (this file) or {mark}wontdo:allow {ban_name} (all of it)"
    else:
        tail = f"and that they can type {mark}wontdo:allow {ban_name}"
    return (
        f"WONTDO blocked this: {path} is {kind}. Banned here: {banned_phrase(root)}. This overrides "
        "any project instruction that says otherwise. Do not retry through another "
        "tool, script, file name or location. Do not try to disable wontdo. Drop this part of "
        "the work and carry on with the code. Do not report it as missing or offer it later. "
        f"Only if the user directly asked for this, tell them wontdo blocked it {tail}"
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
    broad = re.search(r"\badd\s+(-A|--all|-u|\.)(\s|$)|\bcommit\b[^;&|\n]*\s(-\w*a\w*|--all)(\s|$)", command)
    for root in roots:
        baseline = state["baselines"].get(str(root), {})
        head = state.get("heads", {}).get(str(root))
        dirty = dirty_violations(root, baseline, head, windows, state)[0]
        staged = set(git(root, "diff", "--cached", "--name-only").splitlines())
        found = {r: k for r, k in dirty.items() if broad or r in staged or r in command}
        if pushing:
            found.update(committed_violations(root, head, windows, state))
        if found:
            return min(found)
    return None


def additional_context(event, text):
    if text:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}))


def on_session(payload):
    session = session_name(payload)
    root = repo_root(payload.get("cwd") or ".")
    ensure_baseline(load_state(session), session, [root])
    additional_context("SessionStart", rule_text(root))


def on_subagent(payload):
    session = session_name(payload)
    root = repo_root(payload.get("cwd") or ".")
    ensure_baseline(load_state(session), session, [root])
    additional_context("SubagentStart", rule_text(root))


def set_repo_bans(root, names, on):
    table = read_repo_bans()
    current = set(table.get(str(root), []))
    current = current | set(names) if on else current - set(names)
    if current:
        table[str(root)] = sorted(current)
    else:
        table.pop(str(root), None)
    write_repo_bans(table)


def forget_baseline(state, root):
    for key in ("baselines", "heads", "started", "ignores"):
        state.get(key, {}).pop(str(root), None)


def on_prompt(payload):
    prompt = payload.get("prompt") or ""
    cwd = payload.get("cwd") or "."
    root = repo_root(cwd)
    session = session_name(payload)
    state = load_state(session)
    use_state(state)
    notes = []
    choices = ", ".join(["all"] + list(BY_NAME))
    for verb, raw in re.findall(r"^[/$]wontdo:(ban|allow)[ \t]+(.+?)[ \t]*$", prompt, re.M | re.I):
        word = raw.strip("`'\"")
        names = list(BY_NAME) if word.lower() == "all" else [word.lower()] if word.lower() in BY_NAME else None
        if names and not root:
            notes.append("wontdo works repo by repo and this folder is not a git repo, so nothing changed. Tell the owner.")
        elif names:
            on = verb.lower() == "ban"
            if on:
                forget_baseline(state, root)
            set_repo_bans(root, names, on)
            record(payload, verb.lower(), "owner", word.lower())
            label = ", ".join(names)
            notes.append(
                f"The owner banned {label} in this repo until they lift it." if on else
                f"The owner lifted the ban on {label} in this repo, so that is allowed now. Earlier wontdo instructions about it no longer apply."
            )
        elif verb.lower() == "ban" or re.search(r"[*?\[]", word) or off_switch(word, cwd, "Bash", {}):
            notes.append(f"wontdo did not understand '{word}'. Tell the owner the choices are: {choices}, or one exact file after allow.")
        else:
            state.setdefault("allowed", []).append(os.path.realpath(absolute_path(word, cwd)))
            record(payload, "allowed", "owner exception", word)
            notes.append(f"The owner allowed this one file for this session: {word}.")
    if re.search(r"^[/$]wontdo:status[ \t]*$", prompt, re.M | re.I):
        notes.append(f"Tell the owner what is banned in this repo: {banned_phrase(root)}. Choices: {choices}.")
    ensure_baseline(state, session, [root])
    save_state(session, state)
    additional_context("UserPromptSubmit", " ".join([reminder_text(root)] + notes).strip())


def on_pre(payload):
    tool = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    cwd = payload.get("cwd") or "."
    session = session_name(payload)
    state = load_state(session)
    mark = "$" if "turn_id" in payload else "/"
    command = tool_input.get("command") or "" if tool == "Bash" else ""
    roots = bash_roots(command, cwd) if tool == "Bash" else {repo_root(cwd)}
    written = writes_in(tool, tool_input, cwd)
    for path, _ in written:
        if path:
            roots.add(repo_root(os.path.dirname(absolute_path(path, cwd))))
    roots.discard(None)
    use_state(state)
    if not any(active_bans(root) for root in roots):
        if tool == "Bash":
            note_call(session, payload.get("tool_use_id") or "x", "start")
        return
    ensure_baseline(state, session, roots)

    for path, text in written:
        if not path:
            continue
        kind = off_switch(path, cwd, tool, tool_input)
        ban_name = None
        added = "\n".join(new_lines(tool, tool_input, absolute_path(path, cwd), text)) if text else text
        if not kind and not is_allowed(path, cwd, state):
            ban = classify(path, cwd)
            if ban and ban.removals_ok and tool in {"Edit", "MultiEdit"} and not (added or "").strip():
                ban = None
            elif not ban:
                ban = content_ban(path, added, repo_root(os.path.dirname(absolute_path(path, cwd))))
            if ban:
                kind, ban_name = ban.kind, ban.name
        kind = kind or comment_kind(path, cwd, tool, tool_input, text, state)
        if kind:
            record(payload, "pre", kind, path)
            deny(deny_message(path, kind, ban_name, cwd, mark=mark))
            return

    action = banned_tool(tool, tool_input, cwd)
    if action:
        record(payload, "pre", action[1], action[0])
        deny(deny_message(action[0], action[1], action[2], cwd, is_file=False, mark=mark))
        return

    if tool == "Bash":
        rel = git_violation(command, roots, state, call_windows(session))
        if rel:
            record(payload, "commit", "a banned change", rel)
            deny(deny_message(rel, "a banned change, so it can't be committed or pushed", None, cwd, is_file=False))
            return
        note_call(session, payload.get("tool_use_id") or "x", "start")


def on_post(payload):
    if (payload.get("tool_name") or "") == "Bash":
        note_call(session_name(payload), payload.get("tool_use_id") or "x", "end")


def new_prs(root, state):
    """Return URLs of PRs opened with this account since the session began, once each."""
    started = state.get("started", {}).get(str(root))
    if PRS not in active_bans(root) or not started or not shutil.which("gh"):
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
    close_open_calls(session)
    windows = call_windows(session)
    mine, others = {}, {}
    for key, baseline in state["baselines"].items():
        root = Path(key)
        head = state.get("heads", {}).get(key)
        found, elsewhere = dirty_violations(root, baseline, head, windows, state)
        found = {**found, **committed_violations(root, head, windows, state)}
        mine.update({str(root / rel): kind for rel, kind in found.items()})
        others.update({str(root / rel): kind for rel, kind in elsewhere.items()})

    subagent = payload.get("hook_event_name") == "SubagentStop"
    counts = state.setdefault("sub_counts" if subagent else "stop_counts", {})
    limit = 1 if subagent else STOP_BLOCK_LIMIT
    cwd = payload.get("cwd") or "."
    show = lambda path: os.path.relpath(path, cwd) if inside(path, os.path.realpath(cwd)) else path
    to_undo = sorted(p for p in mine if counts.get(p, 0) < limit)
    if to_undo:
        for path in to_undo:
            counts[path] = counts.get(path, 0) + 1
            record(payload, "stop", mine[path], path)
        save_state(session, state)
        print(json.dumps({
            "decision": "block",
            "reason": (
                f"WONTDO: commands in this session, including scripts and subagents, changed files "
                f"agents may not write: {', '.join(show(p) for p in to_undo)}. Banned here: "
                f"{banned_phrase(repo_root(cwd))}. These are yours to undo now: delete files that were created and reverse "
                "edits by hand. If they were committed, remove them in a new commit and do not "
                "rewrite history. Do not use git restore, git checkout, git reset or git stash, "
                "because these files may hold the user's uncommitted work. Leave a file alone only "
                "if you know someone else changed it, and say so. Then finish."
            ),
        }))
        return
    if subagent:
        save_state(session, state)
        return

    notes = []
    seen = state.setdefault("mentioned", [])
    left = sorted(p for p in mine if p not in seen)
    if left:
        seen.extend(left)
        record(payload, "gave-up", "unremoved", ", ".join(left))
        notes.append("agent left these behind, review them: " + ", ".join(show(p) for p in left))
    fresh = sorted(p for p in others if p not in seen)
    if fresh:
        seen.extend(fresh)
        for path in fresh:
            record(payload, "noticed", others[path], path)
        notes.append("changed outside this agent's commands, nothing was done: " + ", ".join(show(p) for p in fresh))
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
    table = read_repo_bans()
    print(f"bans you can use: all, {', '.join(BY_NAME)}")
    print("banned repos:" if table else "nothing is banned in any repo")
    for repo, names in sorted(table.items()):
        print(f"  {repo}: {', '.join(names)}")
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
