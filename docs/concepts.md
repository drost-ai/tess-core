# Concepts

Seven words carry most of Tess Core. Learn them once and the rest of the documentation
reads easily.

## Runtime

A **runtime** is the running engine inside your process. You start one with
`start_runtime(...)` and stop it with `runtime.stop()`. It owns the session manager, the
scheduler, the connection to the task daemon and the shared MCP servers.

There is one daemon-owning runtime per process, and one host process per runtime home: a
second process that tries to serve the same home is refused, so two programs can never
write the same conversation.

## Runtime home

The **runtime home** is the directory where everything durable lives: sessions, tasks,
schedules, monitors, settings and logs. You choose it. Nothing is stored anywhere else,
and there is no database. Copy the directory and you have copied the agent.

## Session

A **session** is one conversation: an ordered history of messages, a model, a permission
mode, a working directory and a set of tools. Sessions have a lifecycle the runtime
manages:

```
            open()                 idle for a while              something arrives
 (nothing) ───────▶  open  ───────────────────────▶  closed  ───────────────────────▶  open
                      │                                 │
                      └──────────── end() ──────────────┴──────▶  ended (for good)
```

- **Open**: loaded in memory, able to run turns.
- **Closed**: unloaded, still on disk, resumable by id. The runtime closes idle sessions by
  itself and reopens them when work arrives for them (a sub-agent's result, a scheduled
  job, a monitor delivery).
- **Ended**: finished deliberately. It runs nothing further.

Your application decides how a session is configured each time it opens, through the
`session_options` function you pass to `start_runtime`.

## Turn

A **turn** is one unit of work in a session: a prompt goes in, the agent loop runs until
the model stops, a result comes out. Inside a turn the model may call tools many times.

Turns in a session run one at a time, in order. If you start a turn while another is
running, it queues. Each turn has an **origin** that says where it came from: `user`,
`scheduled`, `child_message`, `monitor`, or `worker` for host-authored work. Origins matter
because only a user turn carries a person's intent.

## Tool

A **tool** is something the model can do: read a file, run a command, start a sub-agent,
call your API. Tess Core ships 27 and you can add your own. Every tool call passes through
the permission system before it runs.

## Authority

**Authority** is what a session is allowed to do. It combines:

- a **permission mode** (how much to ask),
- **permission rules** (explicit allow, ask and deny entries),
- **allowed roots** (the directories file tools may touch),
- for a sub-agent, the authority its parent pinned at launch, which it can never widen.

Authority is evaluated on every tool call. The model cannot talk its way past it, because
it is enforced in the runtime, not in the prompt.

## Provider

A **provider profile** names a model endpoint: its kind (Anthropic or OpenAI-compatible),
its default model, its API mode, and where its credential comes from. A session is bound
to one profile when it is created and keeps it when reopened.

## How they fit

```
runtime home (files)
└── runtime (in your process)
    ├── session ── turn ── tool calls ──▶ authority ──▶ effect
    │              └── provider request ──▶ model
    ├── session …
    ├── scheduler ──▶ starts turns at the right time
    └── task daemon ──▶ runs background work, wakes sessions when it finishes
```

Next: the [embedding guide](embedding.md) turns these concepts into code.
