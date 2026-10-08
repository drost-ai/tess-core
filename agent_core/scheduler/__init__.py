"""The runtime's scheduler: durable jobs that fire later, in the host process.

``rules`` (local times in IANA zones, resolved to UTC), ``jobs`` (the record
and how it changes), ``store`` (records under the runtime home),
``operations`` (create and change jobs: the tools' and the host's one path),
``firing`` (a turn job's agent turn or notice) and ``timer`` (the runtime's
one timer and the host's API, ``runtime.scheduler``).
"""
