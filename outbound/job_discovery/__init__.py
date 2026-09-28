"""Job-discovery package -- Python port of the daily-job-discovery pipeline.

Modules mirror the JS reference implementation preserved at ``job_discovery/``
(repo root) and replace it module by module. Behavior is locked by ported unit
tests plus cross-language golden checks; see ``docs/PORT-JOB-DISCOVERY.md``.

Wire-note: dicts crossing module/state/Sheets boundaries keep the reference
implementation's camelCase key names so serialized output lines up 1:1.
"""
