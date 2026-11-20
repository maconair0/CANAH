# CANAH: from assistant to operations harness

CANAH reviewed itself and listed gaps. Some of its list already existed (roles, an approval queue, the audit
log, an HTTP API and a CLI); it did not know, which is a gap of its own. The real gaps are below.

## The rule

**Domain- and vendor-agnostic by construction.** Every new capability runs on the common contract's verbs
(`health`, `read_alarms`, `read_devices`, `read_links`, `read_services`, `adjust`, `provision`, ...) and on the
normalised topology, never on a vendor's own format. Anything the agentic MCP accepts gets monitoring, history,
runbooks and scopes with no new code. Domain knowledge lives in files (skills, runbooks), not in Python.

## Plan

| # | Gap | What CANAH gets | Status |
|---|---|---|---|
| 1 | It does not know its own harness | `canah_status`: who is asking and what they may approve, the controllers in reach, the twin, the model, what waits for approval, open incidents. The prompt says what the harness already does. | built |
| 2 | Nothing watches the network | **Watcher**: a controller unreachable, a device or link down, a service out of service, a new alarm. These become **incidents**, deduplicated, opened and resolved by themselves. | built |
| 3 | Alerts only for waiting approvals | **Notification channels**: webhook, Slack, Teams and email (SMTP), each with a minimum severity. Sent on an incident opened or resolved, and on a change waiting for approval. | built |
| 4 | No incident response | **Runbooks** (YAML, matched to incidents) in three modes: `notify`, `investigate` (CANAH investigates and writes a report), `propose` (it also rehearses a fix in the twin and proposes it). A fix always waits for a person's approval. | built |
| 5 | No history of the network | **Network history**: the contract reads of every controller, snapshotted on change. `network_changes` and the Incidents page answer "what changed since yesterday". | built |
| 6 | One user sees every controller | **Scopes**: an account may be limited to some controllers (a tenant, a site, a domain). Tools, the topology and the approvals keep to its scope. | built |
| 7 | Intents and scaling are ad hoc | Skills: `intent-to-change` ("10 Gbit/s from A to B": compute the path, rehearse it, propose it) and `service-scaling` (adjust a live service). | built |
| 8 | Undoing a change | From the audit log: **Propose undo** asks CANAH to propose the inverse change, which is approved like any other. | built |
| 9 | Dashboards of live KPIs over time | At every watcher reading, each controller's devices, links and services (how many, how many down) and its open incidents. Charted on the Incidents page under **Health over time**; the agent's tool is `health_over_time`. Traffic counters are not included yet. | built |
| 10 | Auto-apply under policy | Not built, on purpose: in CANAH (as in edgeLM) a fix is proposed, and a person approves it. It could come later as an opt-in: only when the twin says `auto_apply_eligible`, the runbook allows it, and root has switched it on. | not now |
| 11 | Multi-site federation | Several agentic MCPs (one per site) behind one CANAH, with scopes per site. | next |
