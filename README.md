# wontdo

Ban what agents do, repo by repo.

wontdo is a plugin for Claude Code and Codex. You tell it what agents may not do in a repo: write docs, write unit tests, touch CI, open pull requests, or make branches. Its hooks then block those actions before they run. Nothing is banned until you say so.

## Install

```bash
npx @khalidsaidi/wontdo
```

This adds the wontdo marketplace and installs the plugin in whichever of Claude Code and Codex is on your PATH. Use `--claude` or `--codex` to pick one. The hooks need `python3`.

Or install it by hand:

```bash
claude plugin marketplace add khalidsaidi/wontdo && claude plugin install wontdo@wontdo
codex plugin marketplace add khalidsaidi/wontdo && codex plugin add wontdo@wontdo
```

## Use it

In Claude Code type these as `/wontdo:...`, in Codex as `$wontdo:...`.

| Command | What it does |
|---|---|
| `/wontdo:ban docs` | Ban a thing in this repo until you lift it |
| `/wontdo:allow docs` | Lift that ban in this repo |
| `/wontdo:allow README.md` | Allow one exact file, for this session only |
| `/wontdo:status` | Show what is banned in each repo and what was blocked |

`all` bans or lifts everything at once. Only you can run these: the agent can't ban or lift anything itself.

## What it can ban

- `docs`: documentation, such as READMEs, changelogs and markdown docs.
- `tests`: unit tests, such as test files and in-source tests. Deleting existing tests when asked is still allowed.
- `ci`: CI config, and triggering CI runs (`gh workflow run`, `gh run rerun`, the CI APIs).
- `prs`: opening or changing pull requests, through `gh`, `glab`, `hub`, the APIs or MCP tools.
- `branches`: creating, renaming or deleting branches (`git branch`, `checkout -b`, `switch -c`, `push --delete`, the APIs or MCP tools), and starting worktrees on new branches, including Claude Code's `EnterWorktree` and worktree agents. Listing branches, switching to existing ones and pushing are still allowed.

Bans belong to the repo, not the agent, so Claude Code and Codex share them.

## License

MIT
