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
- [ ] The web page hasn't been tried in a browser yet.

**Phase 5: testing across domains.**
- [ ] A test bed of network domains, each a set of fake or emulated controllers behind an agentic MCP, with its own
  manifests and skills. For example, a packet domain, an optical domain and a wireless or data-centre domain.
- [ ] Per domain: checked live cases (read, change with approval, refusal as observer, large results, documents).
- [ ] Across domains: connections that span two or more domains, where the main agent combines subagents' answers.
- [ ] A score per domain and for cross-domain work, kept with the model and the harness version.

**Later:** a page in an existing console; pushing CANAH to a remote, when wanted.
