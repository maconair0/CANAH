# CANAH: plan and status

## Goal

A central agent harness that can be reused in any networked system and is neutral to network domain and controller.

It's a LangChain Deep Agent with:
- memory, MCP, skills and subagents;
- sign-in, with approval for every change;
- a sandbox;
- checks that keep the agent to what its tools returned.

A system brings its own controllers (through an agentic MCP), its documents and any extra tools.

## Principles

- **The harness imports nothing system-specific.** Domains come in through providers, skills, `AGENTS.md`, `canah.yaml`
  and the agentic MCP's manifests.
- **A change is never made without a human.** Writes always pause, can always be rejected, and need admin (or root, for
  `root_tools`). An observer is never offered a write.
- **The model is replaceable:** `canah/models.py`.
- **Tests run without a live model or network.** Live behaviour is measured separately, with checked answers.

## Done

- **Phase 0, housekeeping:** venv, `requirements.txt` and `requirements.lock`, local git.
- **Phase 1, the core:** models, settings, memory (threads and long-term), skills, subagents, approvals, minor MCP
  tools, CLI.
- **Phase 1b:**
  - networks through an agentic MCP (an overview on the main agent, a subagent per controller);
  - documents (hybrid retrieval, a librarian, citations);
  - observer / admin / root sign-in.
- **Phase 3, the sandbox:** a locked-down Docker container started with the agent.
- **Robustness:**
  - grounding of values and names;
  - a follow-through check;
  - large results kept whole, with a shape summary;
  - passages marked with what they don't mention;
  - a cap on reply length and a timeout per model call;
  - middleware and interrupts configurable on the agent.
- **Clean-up (19 November 2026):** the edgeLM copy and everything lab- or domain-specific were removed. That includes
  the optical lab, its topology, the German network, edgeLM's adapter and its document index. They remain in git
  history (up to commit `57e6767`) and in the edgeLM repo itself.

The tests are listed in the README.

## Next

**Phase 4: the service. Done.** `canah/service.py`, `python -m canah serve`.
- [x] HTTP API: sign-in (sessions with expiry), threads kept per user, a message and its reply, the same as a stream
  (token, tool_call, tool_result, reply), pending, decide (also streamed). One run per thread; a busy thread gets 409.
- [x] A small web page (`canah/static/index.html`): sign in or continue as observer, the streamed conversation, and
  approve or reject a waiting change (approve offered only at the level it needs).
- [x] Tests: 11 (sign-in, streaming, approvals, 403 above your level, per-user threads, bad input).
- [x] Live: the demo network through the server. A read, a change paused, approved over HTTP, then run.
- [x] The web page in a real browser (`tests_canah/browser_check.py`, headless Chromium): it loads as observer, a
  question streams its tool calls and answer, admin sign-in works, and a change waits, is approved in the page and its
  result comes back. Passed 2026-11-19.

**Phase 5: testing across domains. Done** (`testbed/`).
- [x] A test bed of network domains (packet, optical, data-centre fabric) behind an agentic-MCP-compatible emulator
  (`testbed/emulator.py`), with its own `AGENTS.md`, skills and design rules.
- [x] Per domain: checked live cases (read, large read, change with approval, observer refusal, state with rules).
- [x] Across domains: chains, impact of a fault, a tenant's path out, a build under a cross-domain rule,
  `follow_relations`.
- [x] A score per domain and in total, kept with the model and the harness commit (`testbed/results/`). Last:
  packet 9/10, optical 8/10, fabric 4/6, cross 4/10, 25/36 (qwen3 30B, local). The misses are the model's
  planning: unrequested extra changes, cross-domain chains left half-followed, a time budget run out.

**Phase 5b: live controllers, with a digital twin.**
- [x] netwins (a separate project) plugs in as a provider and middleware: a twin mirrored from each packet
  controller, every write tried in it first, `TwinGate` holding what the twin does not support. Checked end to end
  on netwins' SDN lab (see netwins' README).
- [ ] The live controllers (ONOS, Ryu, TeraFlowSDN, TransportPCE) through the agentic MCP on :8400: blocked, because
  `AGENTIC_MCP_TOKEN` is empty in `.env`. The service lists controllers and tools without it, but every call needs
  it. With it set: `~/netwins/integrations/live_controllers.py` (read-only mirror of each), then
  `~/netwins/integrations/canah_live_controllers.py` (CANAH with the twin; every write is checked and then rejected
  at approval, so nothing changes on the lab).

**Later:** a page in an existing console; pushing CANAH to a remote, when wanted.
