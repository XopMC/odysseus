<p align="center">
  <img src="assets/branding/odysseus-wordmark.png" alt="Odysseus" width="238">
</p>

<p align="center">
  A self-hosted AI workspace for chat, agents, research, documents, email, notes, calendar, and local model workflows.
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> ·
  <a href="website/setup.md">Setup Guide</a> ·
  <a href="CONTRIBUTING.md">Contributing</a> ·
  <a href="ROADMAP.md">Roadmap</a>
</p>

<p align="center">
  <a href="https://repology.org/project/odysseus-ai/versions"><img src="https://repology.org/badge/vertical-allrepos/odysseus-ai.svg" alt="Packaging status"></a>
</p>

<p align="center">
  <img src="assets/branding/odysseus-browser.jpg" alt="Odysseus interface">
</p>

---

## Quick Start

> `dev` is the default branch and gets the newest changes first. Use [`main`](https://github.com/odysseus-dev/odysseus/tree/main) if you want the more curated branch.

```bash
git clone https://github.com/odysseus-dev/odysseus.git
cd odysseus
cp .env.example .env
docker compose up -d --build
```

Open `http://localhost:7000` when the containers are healthy. The first admin password is printed in `docker compose logs odysseus`.

Native installs, GPU notes, Windows/macOS instructions, HTTPS, and configuration live in the [setup guide](website/setup.md).

## Features

- **Chat + Agents** — local/API models, tools, MCP, files, shell, skills, and memory.
- **Cookbook** — hardware-aware model recommendations, downloads, and serving.
- **Deep Research** — multi-step web research with source reading and report generation.
- **Compare** — blind side-by-side model testing and synthesis.
- **Documents** — writing-first editor with AI edits, suggestions, Markdown, HTML, CSV, and syntax highlighting.
- **Email** — IMAP/SMTP inbox with triage, tags, summaries, reminders, and reply drafts.
- **Notes, Tasks + Calendar** — reminders, todos, scheduled agent tasks, and CalDAV sync.
- **Extras** — gallery/image editor, themes, uploads, web search, presets, sessions, and 2FA.

## Engineering extension / Инженерное расширение

This fork adds an opt-in, self-hosted foundation for longer coding and agent
workflows. It keeps the existing single-chat mode intact and does not send data
to an external provider unless that endpoint and its task permission are chosen
explicitly.

- **Team workspace** — a simple mode with one lead model and an unlimited,
  user-selected worker pool; advanced controls remain available for detailed
  assignments. Identically named models stay separate because choices include
  their endpoint identity.
- **Durable work** — Team tasks, events, checkpoints and replay data are kept
  server-side so reconnecting clients can recover the saved state.
- **Host runner** — explicitly approved trusted-host terminal, file and Git
  operations use a persistent runner. A command with unknown outcome is not
  silently repeated; privileged or destructive actions still require explicit
  confirmation.
- **Verified isolation primitive** — the Linux runner can execute a command
  only in a retained verification copy using a server-pinned image digest,
  disabled network, no Docker socket, a read-only container root, dropped Linux
  capabilities and CPU/memory/process limits. It is not exposed as a general
  Docker command and is not a claim of complete hostile-code containment.
- **Engineering controls** — project registration starts read-only, with
  reviewed check commands, acceptance criteria, tool availability diagnostics,
  configurable context-compaction policies, and a read-only LSP discovery
  panel. Language-server availability is reported per execution host; the
  Jetson release includes a user-level Python/Pyright toolchain.
- **Russian UI** — the shipped Team, engineering and context-policy panels are
  localized; endpoint labels in model selectors are not truncated.

### Важно

Это расширение предназначено для доверенных машин, которыми владеет оператор.
Доступ к shell, файлам и сети не равен песочнице: включайте его только после
явного выбора проекта и хоста. Изоляция контейнеров, полнофункциональные
межмашинные worktree, DAP, эксперименты между моделями и полная сквозная
приёмка остаются отдельными этапами разработки. LSP-мост работает только для
языковых серверов, фактически установленных и проверенных на выбранном хосте;
его нельзя считать полной IDE-поддержкой только из-за наличия кнопки.
Низкоуровневый изолированный запуск Linux уже проверен на Jetson, но до
публичного UI-потока проекта он остаётся отдельной операторской возможностью.
На Jetson также проверен Pyright: открытие документа и поиск символов работают
через LSP-runner без замены системного Node.js.

### Important

The extension is for operator-owned, trusted machines. Shell, file and network
access are not a sandbox: enable them only after explicitly choosing the
project and host. Container isolation, full cross-host worktrees, DAP, model
experiments and end-to-end release acceptance remain separate delivery stages.
The LSP bridge works only with language servers actually installed and verified
on the selected host; its presence is not full IDE support.
The low-level Linux isolated-runner operation has been verified on Jetson, but
remains an operator capability until the project-facing UI flow is released.
Pyright has also been verified on Jetson for document open and symbol lookup,
using a user-level Node runtime rather than replacing the system Node.js.

## Demo

A full hover-to-play tour lives on the [Odysseus landing page](https://odysseus-dev.github.io/odysseus/). Its source lives under [`website/`](website/).

## Contributing

Help is welcome. The best entry points are fresh-install testing, provider setup bugs, mobile/editor polish, docs, and small focused refactors. See [CONTRIBUTING.md](CONTRIBUTING.md) and [ROADMAP.md](ROADMAP.md).

## Security

Odysseus is a self-hosted workspace with powerful local tools. Keep auth enabled, keep private data out of Git, and do not expose raw model/service ports publicly.

- Keep `AUTH_ENABLED=true` for any network-accessible deployment.
- Keep `LOCALHOST_BYPASS=false` outside local development.

Deployment details are in the [setup guide](website/setup.md#security-notes).

## Star History

<a href="https://star-history.dera.page/#odysseus-dev/odysseus&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://star-history.dera.page/svg?repos=odysseus-dev/odysseus&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://star-history.dera.page/svg?repos=odysseus-dev/odysseus&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://star-history.dera.page/svg?repos=odysseus-dev/odysseus&type=date&legend=top-left" />
 </picture>
</a>

## License

AGPL-3.0-or-later -- see [LICENSE](LICENSE) and [ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md).
