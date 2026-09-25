# Answer providers

Status: spec, developer preview. Nothing here is implemented yet.

Today "Ask Jev" answers have one backend, OpenRouter, called from two places in
`siri.py`: the spoken answer (`ask_llm`) and reminder wording (`prepare_reminder`, `reminder=True`).
This spec adds Apple on-device and agent-session backends behind one boundary.

## Scope

- Settings > Answers: Off, OpenRouter, Apple on-device, Codex session, Claude session.
- Jev classification, Hey Jev's own actions, confirmation gates and the Fish voice
  are unchanged and stay separate settings. Choosing Apple does not make the app offline.
- OpenRouter and Apple only answer. Codex and Claude sessions are full agents that
  may act through their own tools, with their permission prompts shown to the user.
  Hey Jev never turns any provider's text into one of its own actions.

## Boundary (`answers.py`)

```python
def available(provider) -> (bool, reason)          # plain-language reason when False
def ask(provider, prompt, context, rid, deadline, cancel) -> Result
Result = {status: finished|cancelled|unavailable|failed, text, provider, model, latency_ms}
```

- `context` is bounded (last N exchanges, capped characters). Screen or window text
  is included only when the user turns on an explicit context option.
- One deadline and cancel token per request, same as engine steps. Voice Stop cancels.
- Late output after Stop or a provider change is discarded (generation counter).
- Changing provider or workspace, or "New conversation", starts a fresh conversation.
- No cross-provider fallback. Unavailable means a spoken and logged reason, not a
  silent switch to a cloud provider.
- v1 speaks the final answer only (no early partial speech); full text shows in the app.
  Stop drops late chunks and any queued speech.
- Diagnostics: `answer` stage in `requests.jsonl` with provider, status, latency.

Reminder wording goes through the same boundary for OpenRouter and Apple. For agent
providers, and whenever the backend is unavailable, it stays deterministic (the
current fallback text). A timer label never starts an agent session.

## Apple on-device

- Swift helper `heyjev-fm` using `SystemLanguageModel.default` (on-device model only)
  and `LanguageModelSession`. JSON over stdio; one warm process per app run.
- Requires macOS 26+. Availability is checked at runtime (device, Apple Intelligence
  on, model downloaded, language) and the reason is shown when unavailable. Older
  Macs keep working with this choice disabled. "On-device" covers answer generation
  only; Jev decisions and Fish audio may still be remote.
- Optional at build and install time: if the helper can't build (older SDK/OS), the
  app still builds, installs and runs with other providers.
- Gives access to Apple's model only, not Siri's tools or personal context.
- Adopt only if the probe (10 fixed questions, latency p50/p95, quality vs the
  current OpenRouter model) shows usable short answers.

## Codex / Claude sessions

- One small ACP client (JSON-RPC over stdio) driving the public adapters
  `codex-acp` and `claude-agent-acp`. Detect installed binaries; never auto-install.
  If ACP fails the capability probe, switching that provider to Codex app-server or
  Claude's programmatic interface is an explicit implementation change, never a
  runtime switch.
- Auth is the user's own supported login. Hey Jev never reads or copies tokens.
  Probe each installed adapter's advertised auth methods and real login behaviour;
  show actionable login and quota errors. Never switch to API-key billing implicitly.
- A local process does not mean local inference or free use; the UI says so.
- Lazily started, one session per app run, killed on quit. A crashed process is
  restarted once for the next question; the failed question is never replayed.
  Never attaches to any existing Buzz, Codex or Claude session.
- Deadline default 45 s; "thinking" cue while waiting.

### Full sessions (Johnny, 2026-09-25)

Johnny chose full sessions: each agent runs with its own harness exactly as it does
in a terminal or in Buzz, with the user's tools, hooks, MCP servers, instructions
and memory loaded. Hey Jev is only the client.

- Settings per agent: working folder (default: home), on/off. Nothing else is
  overridden; the agent's own config and permission mode apply.
- Permission requests (`session/request_permission`) appear in the existing
  confirmation pop-down with the agent's own options; voice "yes"/"no" works as for
  engine confirmations. No reply within the confirmation window = the offered reject
  option. Hey Jev never auto-approves.
- Visible activity: the status window shows the agent's current tool call and plan
  while it works.
- Long jobs: Hey Jev says a short "working on it", keeps listening, and speaks a
  one or two sentence summary when the turn ends; the full reply stays in the app.
  Stop cancels the turn (`session/cancel`), then kills the adapter if it doesn't settle.
- One session per agent per app run; "New conversation" starts a fresh one. Never
  attaches to an existing Buzz, Codex or Claude session.
- Reminder labels never go to an agent.

## Slices

1. `answers.py` boundary; move both OpenRouter call sites behind it. No behaviour change.
2. ACP client + fake-adapter tests (scripted JSON-RPC over pipes), then Claude and
   Codex live as full sessions with visible permission prompts.
3. Apple helper + provider, if the probe passes.
4. Settings rows, availability reasons, New conversation.

## Review gates

Per backend: availability reason, one real answer, follow-up then reset, missing
login or model, cancel within deadline with late output dropped, tool denial proven,
settings persist across restart, Jev action confirmation unchanged. Full test suite
after each code slice (Teach Jev sheet tests isolated first). Live trials run in
Johnny's app session; no GUI launches from agent shells.

## Sources

- [Apple SystemLanguageModel](https://developer.apple.com/documentation/foundationmodels/systemlanguagemodel)
- [ACP supported agents](https://agentclientprotocol.com/get-started/agents)
- [codex-acp](https://github.com/agentclientprotocol/codex-acp)
- [claude-agent-acp](https://github.com/agentclientprotocol/claude-agent-acp)
- [Codex app-server](https://developers.openai.com/codex/app-server/)
- [Claude programmatic sessions](https://code.claude.com/docs/en/headless)
