<p align="center"><img src="docs/logo.png" alt="CANAH: Controller Agnostic Network Agent Harness" width="640"></p>

**CANAH (Controller Agnostic Network Agent Harness)** is a reusable central agent for networked systems, built on LangChain Deep Agents. It knows no particular network domain or controller: a system plugs in its controllers (through an [agentic MCP](https://github.com/maconair0/agentic-mcp)), its documents and any extra tools. The harness provides the rest:
- models, memory, skills and subagents;
- sign-in, with approval for every change, and an audit log of every decision;
- a sandbox to compute in;
- an HTTP service with a web console;
- checks that keep the agent to what its tools returned.

## Start and stop

```bash
cp .env.example .env     # AGENTIC_MCP_URL / AGENTIC_MCP_TOKEN, CANAH_ADMIN_PASSWORD, CANAH_ROOT_PASSWORD (optional)
./start.sh               # the service and its web page on http://127.0.0.1:8350 (creates .venv on first start)
./start.sh --testbed     # with three emulated domains (packet, optical, fabric) to work against
./start.sh --status      # what is running
./teardown.sh            # stop it all; --purge also deletes the state in canah_data (asks first)
```

Open the printed address in a browser for the console (light and dark, in the style of edgeLM's central
console):
- **Sign-in page.** Sign in as admin or root, or continue as an observer (read only).
- **Topology.** CANAH with every controller the agentic MCP holds. Click a controller to open its devices and
  links, read through the common contract's verbs; click CANAH to open its subagents, which light up while they
  work (`/activity`), each joined to the controller it drives. Arcs show relations across domains; a controller
  that cannot be read is drawn red, with why. Zoom, pan, drag, fit, open or fold all; it refreshes live.
- **SDN controllers.** Each controller's domain, contract verdict and gaps, and which of its own tools serve each
  contract verb. **Onboard** a new one through the agentic MCP (admin): its URL and kind, an API document, and the
  *names* of the variables holding its credentials on the agentic MCP's host (never the secrets). A kind it does
  not know can be added from a manifest or drafted from a user guide. **Reload CANAH** gives every controller held
  now its subagent; **Remove** (root) unseats one.
- **Chat**, in the style of LangGraph's agent chat UI: thread history on the left; every tool call (subagents'
  included) as a card of its arguments with its result beneath; a change as an interrupt to **accept, edit** (the
  arguments as JSON) **or reject**, offered at the level it needs; a switch to hide tool calls. **Attach files**
  (button, drag and drop, or paste; up to 25 MB): kept per user, read by the agent as `/uploads/<thread>/<file>`,
  with PDFs read to text, and copied into the sandbox when there is one.
- **Approvals & audit.** What waits in your conversations; the audit log and 24-hour metrics (admin).
- **Documents & memory.** The **second brain**: the documents the agent cites as a graph (a node per document,
  edges by meaning, named clusters), as in edgeLM; the corpus with upload (admin); long-term memories (admin).
- **Profile & access.** What each level may do. **About & shortcuts.** Keys: `g` then `t c h a k`, `[` `]`, `/`, `n`.

**`start.sh`**
- creates `.venv` and installs `requirements.txt` on first use;
- reads `.env`; a variable already set in the shell wins;
- checks the model answers (and is pulled, for Ollama; has its key, for a hosted one);
- checks Docker and builds the sandbox image if it's missing;
- starts the service in the background and waits until it answers.

It won't start a second copy. Options: `--port`, `--host`, `--tls-cert/--tls-key`, `--no-sandbox`, `--foreground`, `--testbed-port`.

**`teardown.sh`** stops what `start.sh` started, through pid files: SIGTERM first, SIGKILL only after 20 s. It also removes sandbox containers whose agent has stopped.

Logs are in `canah_data/logs`, pid files in `canah_data/run`.

**From the terminal:**

```bash
.venv/bin/python -m canah chat                  # sign in as admin or root, or continue as observer
.venv/bin/python -m canah ask "which routers are down?" --as observer
.venv/bin/python -m canah info                  # what the agent was built from
.venv/bin/python -m canah user add NAME --level admin|root    # accounts; also: user list, user remove NAME
.venv/bin/python -m canah audit [N]             # the last N decisions and changes
.venv/bin/python -m canah metrics [HOURS]       # requests, times, tokens, tool calls, checks that sent answers back
.venv/bin/python -m canah memories [USER]       # long-term memories; memories forget USER PATH
.venv/bin/python -m canah docs add FILE...      # add documents to the corpus and index them
```

To try it without any network, use the demo system: `CANAH_PROVIDERS=examples.demo_network:provider ./start.sh`.

## The model

[canah/models.py](canah/models.py) has one section per provider. Exactly one is active (local Ollama, by default) and the rest are commented out: comment out the active one and uncomment the one you want. The sections cover Anthropic, OpenAI, other Ollama models, OpenAI-compatible servers (vLLM, LM Studio, OpenRouter), Gemini, Bedrock and Groq.

There are three roles: `model` (the main agent), `strong_model` (for a subagent that asks for it) and `fast_model`. An optional `fallback_model` takes over if the main one fails. `CANAH_MODEL=provider:model` in the environment overrides the active section without editing the file. Every line caps the reply length and sets a timeout.

## What's in it

| File | What it does |
|---|---|
| `canah/models.py` | which model; one place |
| `canah/config.py` | settings: `canah.yaml`, then the environment (see Settings below) |
| `canah/agent.py` | `build_agent()`: the pieces into `create_deep_agent`; `CanahAgent.ask / stream / pending / decide`. Takes `middleware` and `interrupt_on` directly too |
| `canah/tools.py` | the `ToolProvider` contract: `tools()`, `writes()`, and optionally `subagents()`, `main_tools()`, `shared_tools()`, `middleware()`, `instructions()` |
| `canah/network.py` | networks through an agentic MCP service: `network_controllers` and `network_health` on the main agent; one subagent per controller, holding that controller's tools; `follow_relations`, which follows dependency chains across domains in code, for any controller that publishes `list_relations` |
| `canah/knowledge/` | documents: a quick cited search (any subagent may use it), a `librarian` subagent, passages marked with the question's names they don't mention, a citation check; hybrid retrieval (dense + BM25, cross-encoder rerank) in `rag/` |
| `canah/identity.py` | observer / admin / root: accounts (environment, or `users.json` with PBKDF2 hashes), sign-in with lockout; the model is offered only the tools the caller may use |
| `canah/approval.py` | every write pauses for a decision: approve, edit or reject |
| `canah/memory.py` | per-thread state (SQLite checkpointer); long-term `/memories/` per user (SQLite store), with listing, deletion and optional expiry |
| `canah/backends.py` | the files the agent sees: scratch, `/memories/`, skills and `AGENTS.md` read-only, kept large results |
| `canah/sandbox/` | where `execute` runs: a locked-down Docker container started with the agent |
| `canah/grounding.py` | checks on the agent's answers (see below) |
| `canah/safety.py` | per request: token counting and budget, screening tool output, recording and re-reading changes, guarding memory writes |
| `canah/large_results.py` | a large tool result is kept whole; the model gets a preview, the path, and a summary of its label fields |
| `canah/ops.py` | the audit log and request metrics (`canah_data/ops.sqlite`) |
| `canah/service.py`, `canah/static/` | the HTTP API and the web console (`index.html`, `app.js`, `app.css`, logos, fonts) |
| `canah/topology.py` | each controller's devices and links, read through the contract's verbs, for the console |
| `canah/subagents.py` | subagents from code or YAML; tools by name or pattern |
| `canah/mcp.py` | minor extra tools over MCP (`mcp.json`): read-only unless allowed, and writes then need approval |
| `canah/skills/` | `approvals`, `incident-triage`, `research` (SKILL.md, loaded on demand) |
| `canah/context/AGENTS.md` | standing instructions loaded at every start |
| `canah/cli.py` | the terminal commands above |
| `testbed/` | three emulated domains behind an agentic-MCP-compatible service, checked cases, and a runner (see Testing) |
| `examples/demo_network.py` | a tiny pretend network, to try the agent with nothing else running |

**To serve a system:**
1. Point the agentic MCP at its controllers (or write a `ToolProvider`, and list it under `providers:` in `canah.yaml`).
2. Add its skills and an `AGENTS.md`.
3. Index its documents.

The harness imports nothing system-specific.

## Who may do what

| Signed in as | Sees write tools | May approve |
|---|---|---|
| observer (no password) | no | nothing; may only reject |
| admin | yes | every write, and every paused tool, except `root_tools` |
| root | yes | everything |

**Approvals:**
- A write through the agentic MCP is gated twice: here, by the approval; and there, by its own write gate, which accepts a call only with the approver's name. That name is sent only after an approval here.
- A change waiting longer than `approval_ttl_s` (default 1 h) can only be rejected.
- `notify_url` gets a JSON message whenever a change starts waiting.
- Every change proposed, every decision (who, level, approve/edit/reject, message) and every result is in the audit log.

## Checks on what the agent says

Each check sends an answer back once, with the reason, for the model to correct itself:

| Check | What it catches |
|---|---|
| grounding | values, counts and names in an answer (decimals, long numbers, ids, labelled or quoted values) that no tool returned and the operator didn't say |
| citation | a `[source p.N]` citation of a source no tool returned |
| follow-through | an answer that announces its next step, says it lacks data its tools can get, or (for someone who may approve) asks "shall I proceed?" in text instead of making the change for approval |
| look first | a substantial answer given without looking anything up |

In every subagent too:
- **Screening:** text from a tool that reads like instructions to the model is fenced as data.
- **Read-back:** after an approved change, the agent is told to read the object back before reporting success.
- **Budgets:** each request has a time budget (`max_seconds`) and an optional token budget, across the main agent and all subagents.

## The sandbox

A Docker container starts with the agent, so `execute` is there from the first message. It's removed when the agent exits; files in `/workspace` are kept in a volume.

The container:
- has no network;
- has a read-only root filesystem and runs as uid 1000;
- drops all capabilities;
- has limits on processes, memory and CPU;
- uses gVisor if Docker has it.

Each container is labelled with the process that owns it, and one left behind by a process that's no longer running is removed at the next start. If Docker isn't usable, the agent starts without the sandbox and says why. `--no-sandbox`, or `sandbox: none`, turns it off on purpose.

## The HTTP API

| Endpoint | What it does |
|---|---|
| `POST /session`, `GET /session`, `DELETE /session` | sign in (a bearer token), who am I, sign out; sessions are kept across restarts |
| `POST /threads` | a new thread |
| `POST /threads/{id}/messages` | a message; returns the reply, what's pending, the tool calls |
| `POST /threads/{id}/stream` | the same as server-sent events: token, tool_call, tool_result, reply |
| `GET /threads/{id}/pending` | what the thread waits on, and whether you may approve it |
| `POST /threads/{id}/decide` | approve / edit / reject (also streamed) |
| `GET /audit`, `GET /metrics` | the audit log; request metrics (admin) |
| `GET /memories`, `DELETE /memories/{user}?path=` | long-term memories (admin) |
| `GET /documents`, `POST /documents` | the corpus; add a document (admin) |
| `GET /threads/{id}/history` | the conversation so far, for reloading it |
| `GET /network`, `GET /topology[?refresh=1]` | the controllers and their health; their devices, links and cross-domain relations |
| `POST /threads/{id}/files` | attach a file to a conversation (`{"name", "content_base64"}`) |
| `GET /activity`, `GET /knowledge/mesh` | what runs now and which subagents are at work; the documents as a graph |
| `GET /network/kinds`, `POST /network/controllers`, `DELETE /network/controllers/{key}` | onboard (admin) or remove (root) a controller through the agentic MCP |
| `POST /network/manifests`, `POST /network/guides`, `POST /admin/reload` | add a controller kind (admin); rebuild the agent so new controllers get subagents (admin) |
| `GET /info`, `GET /health`, `GET /` | what the agent is built from; liveness; the web console |

No token means an observer. Threads belong to the user who made them, and a busy thread gets 409. Use `--tls-cert/--tls-key` when serving beyond localhost.

LangSmith tracing needs no code: set `LANGSMITH_TRACING=true` and `LANGSMITH_API_KEY`.

## Settings

These go in `canah.yaml`, or in the environment as `CANAH_<NAME>` for the common ones.

| Setting | Default | What it does |
|---|---|---|
| `providers` | network, knowledge | the systems served, as `package.module:factory` |
| `sandbox` | `docker` | `docker` or `none` |
| `root_tools` | delete, remove, reboot, restart | writes only root may approve (glob patterns) |
| `interrupt_on` | none | more tool calls that pause for approval: `{pattern: true \| {allowed_decisions, description}}` |
| `middleware` | none | extra LangChain middleware for every agent, as `package.module:Class` |
| `max_seconds` | 600 | time budget per request |
| `max_tokens_per_request` | 0 (none) | token budget per request |
| `approval_ttl_s` | 3600 | how long an approval stays open |
| `notify_url` | none | webhook for changes waiting |
| `memory_ttl_days` | 0 (kept) | expiry of long-term memories |
| `knowledge_collection` | `corpus` | which document collection to search |
| `large_result_chars` | 12000 | above this, a tool result is kept whole and previewed |
| `look_first`, `audit` | on | the look-first check; the audit log and metrics |
| `skills_dirs`, `memory_files`, `subagents_file`, `mcp_config` | the harness's | skills, `AGENTS.md` files, subagents YAML, minor MCP tools |

## Testing

```bash
.venv/bin/python -m unittest discover -s tests_canah -t .   # 117 tests: scripted model, fake services, about 30 s
.venv/bin/python -m testbed.run --rounds 2                   # live: the real model on the emulated domains
.venv/bin/python -m tests_canah.browser_check                # the web page in headless Chromium (needs playwright)
```

**`testbed/`** emulates three network domains, each a controller with its own state and tools:
- **packet:** routers, links, VLAN services;
- **optical:** ROADMs, transponders, lightpaths with OSNR;
- **fabric:** a data-centre leaf-spine with tenants.

They meet at declared hand-offs: a packet link rides an optical lightpath, and a tenant leaves through a border leaf to a packet service. All three sit behind an agentic-MCP-compatible service with its write gate. The runner asks 18 checked questions:
- **per domain:** read, a large read, a change with approval, an observer refusal, live state combined with the design rules;
- **across domains:** chains, impact of a fault, a tenant's path out, a build under a cross-domain rule, an observer refusal.

Each case is checked against the answer and against the domain's state afterwards. Results go to `testbed/results/`, with a score per domain, the model and the harness commit. The domain-specific material (the emulator, its `AGENTS.md`, skills and design rules) lives only in `testbed/`.

## License

MIT. See [LICENSE](LICENSE).
