# Answer providers

Status: spec, developer preview. Nothing here is implemented yet.

Today "Ask Jev" answers have one backend, OpenRouter, called from two places in
`siri.py`: the spoken answer (`ask_llm`) and reminder wording (`reminder=True`).
This spec adds Apple on-device and agent-session backends behind one boundary.

## Scope

- Settings > Answers: Off, OpenRouter, Apple on-device, Codex session, Claude session.
- Answers only. Jev classification, action execution, confirmation gates and the
  Fish voice are unchanged and stay separate settings. Choosing Apple does not make
  the app offline.
- No provider result ever becomes an action.

## Boundary (`answers.py`)

```python
def available(provider) -> (bool, reason)          # plain-language reason when False
def ask(provider, prompt, context, rid, deadline, cancel) -> Result
Result = {status: finished|cancelled|unavailable|failed, text, provider, model, latency_ms}
```

- `context` is bounded (last N exchanges, capped characters).
- One deadline and cancel token per request, same as engine steps. Voice Stop cancels.
- Late output after Stop or a provider change is discarded (generation counter).
- Changing provider, or "New conversation", starts a fresh conversation.
- No cross-provider fallback. Unavailable means a spoken and logged reason, not a
  silent switch to a cloud provider.
- Speak a short answer; show the full text in the app.
- Diagnostics: `answer` stage in `requests.jsonl` with provider, status, latency.

Reminder wording goes through the same boundary for OpenRouter and Apple. For agent
providers, and whenever the backend is unavailable, it stays deterministic (the
current fallback text). A timer label never starts an agent session.

## Apple on-device

- Swift helper `heyjev-fm` using `SystemLanguageModel.default` (on-device model only)
  and `LanguageModelSession`. JSON over stdio; one warm process per app run.
- Requires macOS 26+. Availability is checked at runtime (device, Apple Intelligence
  on, model downloaded, language) and the reason is shown when unavailable. Older
  Macs keep working with this choice disabled.
- Built by `setup.py`, shipped inside the bundle.
- Gives access to Apple's model only, not Siri's tools or personal context.
- Adopt only if the probe (10 fixed questions, latency p50/p95, quality vs the
  current OpenRouter model) shows usable short answers.

## Codex / Claude sessions

- One small ACP client (JSON-RPC over stdio) driving the public adapters
  `codex-acp` and `claude-agent-acp`. Detect installed binaries; never auto-install.
  If ACP blocks a required capability, fall back to Codex app-server or Claude's
  programmatic interface for that provider.
- Auth is the user's own supported login. Hey Jev never reads or copies tokens.
- A local process does not mean local inference or free use; the UI says so.
- Lazily started, one session per app run, killed on quit, restarted once on crash.
  Never attaches to any existing Buzz, Codex or Claude session.
- Deadline default 45 s; "thinking" cue while waiting.

### Milestone 1: answer-only

Tools are disabled by runtime configuration, not by prompt: adapter/session options
that disallow tools, MCP servers and hooks, plus Hey Jev denying every
`session/request_permission`. Acceptance proves it: a prompt asking the agent to
write a file or run a command yields no file and no command.

### Later: tools-enabled sessions

Separate, explicit choice. Needs a chosen workspace, visible tool activity, voice or
on-screen approvals, Stop, and its own conversation id. Not claimed until built and
tested.

## Slices

1. `answers.py` boundary; move both OpenRouter call sites behind it. No behaviour change.
2. ACP client + fake-adapter tests (scripted JSON-RPC over pipes), then Claude and
   Codex live, answer-only.
3. Apple helper + provider, if the probe passes.
4. Settings rows, availability reasons, New conversation.

## Review gates

Per backend: availability reason, one real answer, follow-up then reset, missing
login or model, cancel within deadline with late output dropped, tool denial proven,
settings persist across restart, Jev action confirmation unchanged. Full test suite
after each code slice (Teach Jev sheet tests isolated first). Live trials run in
Johnny's app session; no GUI launches from agent shells.

Sources: Apple SystemLanguageModel docs, agentclientprotocol.com agent list,
codex-acp and claude-agent-acp repos, Codex app-server docs, Claude headless docs.
