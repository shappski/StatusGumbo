# Roadmap

Ideas that are wanted but not started. Nothing here is promised.

## Other coding agents: Codex, Gemini CLI, and more

StatusGumbo only knows Claude Code today. The goal is to show sessions from other agents on the same page, and to have a tap on a card open that session in the agent's own phone app.

- **The collector is mostly agent-neutral already.** It stores host, session, status, context use and a link. It would need an agent field, and a per-agent rule for building that agent's link (never a link taken from the wire, as now).
- **The reporter is the Claude-specific part.** It reads Claude Code's status-line payload, its hooks, and the Remote Control record in the transcript. Each agent needs its own small reporter that posts to the same endpoint. For Codex CLI, its session logs and its turn-complete notify hook look like the starting points. Unverified.
- **Opening the session on the phone works only when the app claims a web URL** for a session, as `claude.ai/code/session_…` does for the Claude app. Codex cloud tasks have chatgpt.com URLs that the ChatGPT app may handle. A local session with nothing like Remote Control would show its status but have nothing to open.
- **First step:** check what each agent currently exposes (status hooks, session logs, cloud APIs, app links) before designing anything.
