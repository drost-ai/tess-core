# Samples

Complete programs built on Tess Core, kept in their own repository:
[github.com/drost-ai/tess-core-samples](https://github.com/drost-ai/tess-core-samples). Each
one is short enough to read from top to bottom, runs against your own model in a few
commands, and is meant to be copied and changed.

| Sample | What it is | Shows |
|---|---|---|
| [coding-agent-minimal](https://github.com/drost-ai/tess-core-samples/tree/main/coding-agent-minimal) | A terminal coding agent in one file, about 290 lines. You type a request; it reads and edits your project, runs commands and delegates to sub-agents. | Multi-turn sessions, live event display, sub-agents that report back while you type, resuming a conversation |

## Run one

```sh
git clone https://github.com/drost-ai/tess-core-samples.git
cd tess-core-samples/coding-agent-minimal
uv sync
cp .env.example .env        # keep the line your model needs
uv run agent.py --cwd ~/path/to/your/project
```

Every sample has its own folder and its own `pyproject.toml`, so `uv sync` in that folder is
the whole setup. Each reads its models from a `providers.yaml` beside it; choose one with
`--provider NAME`. See [Providers](providers.md) for what a profile contains.

## What they have in common

The agent in every sample is the three calls from [Getting started](getting-started.md):
start a runtime, open a session, run a turn. The rest of each file is what makes it that
sample: how it shows the agent's work, what it lets the agent do, and how you talk to it.
Reading one beside the [embedding guide](embedding.md) is the quickest way to see how a real
host is put together.
