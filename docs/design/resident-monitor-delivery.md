# Resident subprocess monitor delivery

Resident children are native sessions owned by their existing subprocess, not
host channel/session mappings. Host monitor intake resolves persisted child
ownership before submitting host session work. Background child IDs—including
stopped children and orphan child sessions—never enter host `pending` or cause
`options_for(child_id)` calls. No replacement worker is launched.

The existing durable monitor outbox is the transport; no second mailbox queue
or synthetic host mapping is introduced. The resident polls it only at the
boundary between ordinary turns. Work arriving during an active turn waits.

## Delivery modes and destination

* **Notification:** deterministic task evidence, persisted together with an
  idempotent receipt in the child's session. It uses canonical synthetic
  `monitor_notification` provenance (`actor=task`, no instruction authority),
  not an assistant recollection or a genuine user request. It does not invoke
  a provider, enqueue an auto-waking mailbox message, or prolong idle lifetime.
  The evidence is available to the next independently authorized turn.
* **Explicit continuation:** only an outbox envelope registered in continuation
  mode can start a turn. It executes in the existing resident loop, using
  matching persisted child authority, current permissions, existing runtime
  bindings, and configured background turn limits. A strict local admission gate
  composes optional configured ceilings with the inherited execution budget;
  it prevents parallel tool-batch overshoot without resetting inherited counters
  or introducing default caps. Configured turn timeout cancels blocked providers.
  Evidence retains the normal
  monitor envelope/instruction distinction. Provisional events are suppressed.

Both modes acknowledge **local resident-session delivery**, explicitly recorded
as `destination=resident_session`. This is not a receipt that the parent or user
received a message. An authorized continuation can use normal `SendMessage`
when parent communication is in scope; delivery itself adds no parent wake or
external fanout.

## Durability, stop, and crash semantics

The owner adapter reuses `deliver_monitor` for claims, renewal, total deadlines,
checkpointing, publication fencing, retries, and cleanup. An already-started
continuation without a checkpoint is never automatically executed again after
lease expiry: it remains visibly `monitor.execution_uncertain`. Authored results
can be retried without re-running the model. Receipt IDs are retained for the
session lifetime; replay after an ambiguous acknowledgement does not append
notification context twice.

A definitely stopped/failed/completed owner causes a retained failed delivery
with `monitor.owner_unavailable`, preserving authored and uncertain execution
history without fabricating a successful receipt. Idle expiry retires the
current pending outbox, and host intake fails later observations for that
terminal owner. An unknown/missing owner record defers visibly rather than
assuming authority to run it. An owner process crash is ultimately classified
by the existing task supervisor; no PID-only heuristic is used to resurrect it.

Stop/mute revoke monitor claims via the existing controller. Owner stop is also
observed while a continuation is running. Cancellation-resistant cleanup blocks
all subsequent resident turns until it settles; the supervisor's existing
process containment/hard-stop path is the escape hatch. This deliberately favors
no overlapping execution over continued service from a quarantined worker.

## Limitations

There is no exactly-once guarantee for arbitrary external tool effects. A crash
after tool execution but before checkpoint remains uncertain and requires
operator reconciliation. Session receipt history grows with delivered monitor
items. Unknown ownership cannot be repaired automatically. Passive notices may
remain unread if no later authorized turn occurs. Neither a local receipt nor
a task exit code is proof of independent verification or delivery to a human.

## Disposable verification

`python -m pytest -q tests/test_resident_monitors.py tests/test_session_monitors.py tests/test_monitor_delivery.py tests/test_package30_monitor_transitions.py tests/test_resident_subagent.py`

Owner tests cover idle and running notifications with zero model calls, actual
resident-loop turn serialization, durable replay/reload deduplication, stopped
and unknown owners, stop cancellation, crash no-repeat, and real engine turns
with a scripted provider. Real turns verify evidence provenance, strict parallel
tool/provider ceilings, preservation of inherited counters, and timeout
cancellation without a false receipt. No external provider or live service is
used.
