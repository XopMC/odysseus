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
- **Live chat recovery** — every active Agent event has a durable run/sequence
  identity. Reloading or opening the chat on another device reconstructs
  separate answer, reasoning and tool cards, resumes shell progress without
  duplicates, keeps the elapsed timer, and restores the exact-run Stop action.
- **Plan and Goal** — Plan is a read-only proposal that runs only after explicit
  approval and keeps durable step state. Goal is independent: it continues a
  detached server run until `complete_goal` records verification evidence, or
  pauses for a permission, unknown side effect, budget or user decision.
- **Projects** — chats can be bound to a server-validated folder on a registered
  execution host. The sidebar groups project chats and exposes isolated project
  memory, project-only `SKILL.md` instructions and explicit read-only/trusted/
  verified-isolation access modes. Skill text never grants permissions.
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
  runner advertises only toolchains it can actually start (the current Jetson
  baseline is Python/Pyright, with other languages shown as unavailable until
  installed and verified).
- **Reviewed Team MCP** — the owner can inspect an exact current MCP schema in
  the engineering UI and explicitly enable only public/brokered read access
  for selected Team roles. Every grant is owner-scoped, revision-bound and can
  be revoked; it never grants shell, files, secrets or mutation authority.
- **Browser evidence** — reviewed Browser MCP screenshots are stored as bounded,
  owner-scoped task artifacts and shown in the Team evidence panel. They remain
  untrusted evidence, not an automatic proof that a task or UI check passed.
- **Project memory** — owner-scoped, versioned project facts retain their
  source and review state (`proposed`, `verified`, or `stale`). Saving or
  forgetting a record requires explicit confirmation. Only `verified` records
  are supplied to local Team models; records are never automatically forwarded
  to external models.
- **Russian UI** — the shipped Team, engineering and context-policy panels are
  localized; endpoint labels in model selectors are not truncated.
- **Context continuity** — approval, Stop, pause and provider errors retain the
  last model-visible checkpoint; the displayed percentage cannot fall unless
  an explicit compaction succeeds.

### Важно

Это расширение предназначено для доверенных машин, которыми владеет оператор.
Доступ к shell, файлам и сети не равен песочнице: включайте его только после
явного выбора проекта и хоста. Изоляция контейнеров, полнофункциональные
межмашинные worktree, DAP, эксперименты между моделями и полная сквозная
приёмка остаются отдельными этапами разработки. LSP-мост работает только для
языковых серверов, фактически установленных и проверенных на выбранном хосте;
его нельзя считать полной IDE-поддержкой только из-за наличия кнопки.
Мастер проектов проверяет папку на выбранном runner и не доверяет пути из
браузера. Режим изоляции доступен только при подтверждённой поддержке runner;
скрытого перехода к доверенному хосту нет.
На Jetson UI показывает только языковые серверы и toolchain, которые фактически
установлены и прошли smoke-проверку. Отсутствующие Rust/Go/Swift/CUDA-профили
помечаются как недоступные и не считаются работающими. Xcode и Metal-профили
по-прежнему запускаются только на явно выбранном Mac-host.

**Память проекта** хранит факты, источник и состояние проверки (`предложено`,
`проверено`, `устарело`) отдельно для владельца и проекта. Сохранение и удаление
требуют отдельного подтверждения. Локальным моделям команды передаются только
записи со статусом «проверено»; внешним моделям записи автоматически не
передаются.

Проверенные MCP-инструменты не считаются безопасными по описанию или аннотации:
включайте каждый инструмент только после просмотра его точной схемы. В команде
поддерживаются лишь публичное чтение и чтение сети через посредника; операции
изменения, доступ к секретам и к хосту этим механизмом не выдаются.

Снимки Browser MCP сохраняются как ограниченные артефакты задачи, доступные
только владельцу, и отображаются в панели доказательств команды. Это
непроверенные данные: снимок сам по себе не означает, что задача или UI-проверка
пройдены.

**Восстановление чата:** активный запуск хранит последовательную ленту текста,
размышлений, инструментов, прогресса и раундов. После перезагрузки страницы или
на втором устройстве интерфейс собирает те же отдельные карточки, продолжает
поток с последнего события и возвращает кнопку остановки.

**План и Цель:** План сначала строится только с инструментами чтения и начинает
выполняться после нажатия «Выполнить». Цель не создаёт план: сервер продолжает
работу после преждевременного ответа модели и принимает завершение только через
`complete_goal` с проверяемыми доказательствами. Подтверждения, бюджет и действия
с неизвестным исходом не обходятся — Цель остаётся ждать пользователя.

**Проекты:** папка выбирается на зарегистрированном Jetson/Mac runner, а её
канонический путь и доступность проверяет сервер. Чаты, долговременная память и
`SKILL.md` из `.odysseus/skills/` изолированы по проекту; навыки являются
недоверенным контекстом и не расширяют доступ к машине.

**Настройки доступа:** щит в composer задаёт owner-scoped режим для Agent,
Chat и Team/Command: «Спрашивать каждый раз», «Спрашивать только важные» или
«Полный доступ». Полный доступ убирает обычные карточки подтверждения для
включённых инструментов, но не отключает проверку владельца, проекта/хоста,
делегированных токенов, внешнего недоверенного контекста и неизвестных
побочных эффектов.

HTTP и HTTPS работают одновременно без редиректа и без HSTS. Они используют
раздельные cookies; HTTPS-cookie всегда `Secure`, а HTTP-сессия считается
небезопасной для публичной сети.

### Important

The extension is for operator-owned, trusted machines. Shell, file and network
access are not a sandbox: enable them only after explicitly choosing the
project and host. Container isolation, full cross-host worktrees, DAP, model
experiments and end-to-end release acceptance remain separate delivery stages.
The LSP bridge works only with language servers actually installed and verified
on the selected host; its presence is not full IDE support.
The project wizard validates the folder on the selected runner. Isolation is
offered only when the runner reports verified support; there is no silent
fallback to trusted-host execution.
Jetson reports only language servers and toolchains that are installed and pass
smoke checks. Missing Rust/Go/Swift/CUDA profiles are shown as unavailable, not
as working. This does not turn Linux into a Mac replacement: Xcode and Metal
profiles remain available only on an explicitly selected Mac execution host.

Reviewed MCP tools are not treated as safe based on a description or
annotation: inspect the exact schema before enabling each one. Team supports
only public reads and brokered network reads through this control; it grants no
mutation, secret or host authority.

Reviewed Browser MCP screenshots are retained as bounded, owner-scoped task
artifacts in the Team evidence panel. They are untrusted evidence, not an
automatic pass verdict for a task or UI check.

The composer shield stores one owner-scoped access mode for Agent, Chat and
Team/Command: “Ask every time”, “Ask only important”, or the red “Full access”.
Full access suppresses routine approval cards for enabled tools; it does not
remove owner/project/host checks, delegated-token restrictions, external
untrusted-context gates, or unknown-side-effect protection.

HTTP and HTTPS intentionally remain available without an HSTS policy or redirect.
They use separate session cookies; the HTTPS cookie is always Secure, while the
HTTP session is suitable only for trusted/private networks.

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
