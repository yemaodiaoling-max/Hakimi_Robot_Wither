# Hakimi

[简体中文](README.md) · **English**

A cat-like AI companion that lives in your QQ groups, on Bilibili, and on your Windows desktop. All three embodiments share **one memory core**: conversations in QQ groups, videos watched on Bilibili, and hours spent on your desktop become memories of the same Hakimi, surfacing naturally whenever a matching topic comes up.

- ⚡ **Runs out of the box with one API key—or splits roles across many**: enter **one single API key** (Google Gemini, Gemini-compatible, or any OpenAI-compatible endpoint) for the full core experience; hot-backup routes, fallback responders, post-reply reviews, heuristic/LLM judges, search pre-routing, and ops supervision are all disabled by default or reuse the main gateway, and can each be pointed at independent APIs or CLIs.
- 🐱 **A timing-aware group companion**: replies immediately to @-mentions and DMs while briefly merging rapid follow-ups (optional C++17 native classifier); unpacks nested forwarded messages and auto-quotes across intervening chat; reads context before joining group banter, sparks topics when quiet, and yields when busy.
- 🧠 **Layered memory core & tiered forgetting**: verbatim storage + SQLite FTS5 + optional exact local dense retrieval (`bge-small-zh-v1.5`), strict per-group isolation, per-person notebooks & social graph, async post-reply curation, nightly illustrated private diary, and tiered physical pruning for aging Bilibili episodes.
- 📺 **Watches Bilibili autonomously & bridges into QQ/pet**: picks recommended videos by interest and monitors a priority live room; exports SHA-256-verified autobiography snapshots recalled only when QQ topics genuinely match (**zero extra tokens on unrelated QQ chat**), plus live presence status synced to desktop pet and gated QQ queries.
- 🖥️ **Desktop pet**: sits on your Windows desktop, comments on your screen, chats by voice, operates your PC after explicit confirmation, reads live Bilibili status, and supports fixed-file shift handoffs for low-cost long-running sessions.
- 🔐 **Your keys stay on your server**: every primary model request goes through a local HTTPS gateway with one cost ledger and an optional monthly spending cap; auxiliary API keys and Bilibili cookies are stored separately and encrypted.

> The bot speaks Chinese and its default persona is written in Chinese; the persona files are plain text you can rewrite in any language.

## Contents

- [Modules](#modules) · [Architecture](#architecture) · [Single-API Default and Independent Auxiliaries](#single-api-default-and-independent-auxiliaries) · [Features](#features)
- [Quick start (Linux server)](#quick-start-linux-server) · [Desktop pet (Windows)](#desktop-pet-windows)
- [Persona and identity](#persona-and-identity) · [Commands](#commands) · [Updating](#updating)
- [Data and privacy](#data-and-privacy) · [Development and tests](#development-and-tests) · [Documentation](#documentation) · [License](#license)

## Modules

One required **memory core** plus three optional modules, in any combination: install one, two, or all of them. A module that is not enabled never starts and does not affect the others.

| Module | What it does | Runs on |
|---|---|---|
| **Memory core** `core` (required) | Long-term memory (SQLite + FTS5, optional `bge-small-zh-v1.5` semantic search), social graph & affinity, async curation & diary, info/setup console, local HTTPS model gateway | Linux server (also runs on Windows) |
| **QQ bot** `qq` | Official QQ bot channel (plus OneBot adapter): group/private chat, follow-up merging, nested forward unpacking, cold-group topics, traceable web search (`[W1]`), stickers, formula rendering, image/video understanding, optional AI drawing & local CPU voice, nightly diary | Same as above |
| **Video bot** `video` | `bili_companion`: browses Bilibili recommendations by interest, watches a priority live room, optional danmaku & comments; writes SHA-256-verified autobiography & live presence snapshots into shared memory | Linux server (needs `ffmpeg`) |
| **Desktop pet** `pet` | Windows desktop pet: screen commentary, voice chat, confirmation-gated PC actions, live Bilibili status readout, and fixed-file shift handoff synced back into memory | Windows PC |

## Architecture

### System topology & unified memory bridge

```
                        ┌─────────────────── memory core ───────────────────┐
                        │  memory.sqlite3 (FTS5 + opt. bge-small-zh dense)  │
  QQ bot (qq) ────────▶ │  social graph · async curation · private diary    │ ◀─── desktop pet (pet, Windows local/SSH)
  (Official / OneBot)   │  info & setup console (:18768, loopback + token)  │      screen · voice · presence · fixed handoff
                        │  local HTTPS gateway (:18765, ledger) ─▶ Upstream │
                        └───────────────────▲─────────────────────────┬─────┘
                                            │ hakimi-memory-bridge    │ optional extensions (aux_config.json /
                                            │ 30m verified digest sync│                      antigravity_tasks.json)
                                  video bot (video, bili_companion)   └─▶ independent HTTP APIs · CLI agents
                                  ├─ to core: autobiography (topic-gated recall + tiered pruning) + presence
                                  └─ from core: privacy-sanitized QQ/pet interest cues (no self:bilibili loop)
```

- **One codebase, config-only differentiation**: a minimal single-API install runs the exact same repository code as a multi-provider, multi-agent production deployment; extra responders and supervisors are enabled strictly by files in your **instance folder** (default `~/hakimi-data`, `HAKIMI_ROOT`; QQ runtime files live in `<root>/official_test_runtime/`).
- **Autobiography bridge & strict QQ token/disk budgets**:
  - `bili_companion` exports SHA-256-verified watch/live autobiography snapshots (synced into `session='self:bilibili'`, recalled in QQ only when topics genuinely match—adding zero tokens to ordinary unrelated QQ chat) and lightweight live `presence.json` (gated to owner/status questions on QQ, automatically included on desktop pet).
  - Tiered forgetting & physical disk cleanup: watched video/live episodes expire by depth/engagement (45–60 days high-interest, 14 days normal, 5 days low-interest, 240 active items cap by default), physically purging expired rows along with FTS rows, dense embeddings, and orphaned graph edges while `bili_companion` trims old evidence and acked outbox rows daily.
- **One upstream API is enough**: all core calls pass through the local gateway at `https://127.0.0.1:18765` (tracked in `gateway_ledger.sqlite3`), supporting Google Gemini, Gemini-compatible endpoints, or any OpenAI Chat Completions compatible API (DeepSeek, OpenRouter, local servers, etc.).

### Seven-layer package architecture

The core Python package `hakimi` (`src/hakimi/`) is organized into **seven unidirectional layers** where lower layers cannot import higher layers—enforced statically via `hakimi arch --check` and `tests/test_architecture.py`:

| Layer | Packages / Modules | Responsibility |
|---|---|---|
| **L6 Entry & Ops** `entry` | `ops`, `console`, `setup`, `config`, `modules`, `__main__`, `architecture`, `tools.qq_cli` | systemd unit runners, watchdog & minimal repair, info console, `/setup.html`, CLI entry, import-direction checker |
| **L5 Orchestration** `orchestration` | `bridge`, `transport`, `curation`, `features` | Turn scheduling & follow-up merging (optional C++17 `bridge/followup.cpp`), official QQ & OneBot transports, async memory/member curation, diary / QZone / help queue |
| **L4 Models & Web** `models` | `models`, `web` | Model routing & budgets, primary/backup/fallback/Antigravity sessions; public web search & page fetch, metered native Google grounding, optional DSH search & `[W1]` citation archive |
| **L3 Prompting** `prompting` | `prompt` | Context compaction & token budgets, per-group tone profiles, tool catalog & selection, gated Bilibili experience & presence injection |
| **L2 Capabilities** `capabilities` | `tools`, `voice` | Multi-model AI drawing, Markdown/TeX math rendering, stickers, Bilibili & QQ mini-app frame sampling, feeding, optional local CPU ASR & TTS/humming |
| **L1 Primitives & Data** `data` | `chat`, `memory`, `social`, `gateway` | Shared QQ primitives (bubbles, mentions, quotes, media/file permissions, quotas, send logs), `MemoryStore` & autobiography sync/pruning, social graph & cross-group identities, gateway ledger |
| **L0 Foundation** `foundation` | `core`, `persona`, `defaults`, `paths`, `_legacy` | `root`/`runtime`/`base` paths, JSON I/O, time helpers, Fernet secrets, persona text, backward-compatible legacy module aliases |

The video companion lives alongside it in `bili_companion/`, and the Windows desktop pet lives in `desktop_pet/` (with root entry compatibility shim `desktop_pet_agent.py`).

## Single-API Default and Independent Auxiliaries

Every secondary model call and background supervisor is decoupled behind `<instance>/aux_config.json` and the collapsed **“高级与独立 API” (Advanced & independent APIs)** section in `/setup.html`:

| Capability | Key in `aux_config.json` | Open-source default | Optional independent modes |
|---|---|---|---|
| **Post-reply review & daily events** | `review` | `mode: "main_gateway"` (reuses main gateway) | `custom_api` (separate OpenAI/Gemini HTTP API), `companion_duty`, or `disabled` |
| **QQ primary hot-backup route** | `qq_backup` | `enabled: false` (`mode: "disabled"`) | `custom_api` (separate OpenAI/Gemini HTTP API with optional `fallback_model`) or `companion_duty` |
| **Timeout / outage fallback responder** | `fallback` | `enabled: false` (`backend: "http_api"`) | Enable with an independent HTTP API (no external CLI required) or external `dsh` CLI |
| **Context & continuation judge** | `context_judge` | `mode: "heuristic"` (local rules, zero API calls) | `main_gateway`, `custom_api`, or `dsh` |
| **Search pre-routing & paid search** | `search_routing` | `enabled: false`, `judge_mode: "local_rules"`, `paid_search_mode: "disabled"` | Enable pre-search judge (`main_gateway` / `custom_api` / `dsh`) and DeepSeek paid search; optionally enable `dsh_on_budget` to search with the DSH account when reservations are declined or a provider reports a quota limit (separate charges may apply). Off by default; public sources remain available |
| **Daily diary fallback** | `diary.fallback_enabled` | `false` (main model only) | `true` (falls back to the configured `fallback` backend if primary fails) |
| **Ops watchdog / Terra supervision / Sol repair / Help queue** | `ops.*` | All 4 toggles `false` (zero extra supervisor processes) | Toggle each independently (`watchdog_enabled`, `terra_supervision_enabled`, `sol_repair_enabled`, `codex_help_enabled`) with `main_gateway`, `custom_api`, or `codex_cli` |

> Leave all auxiliary settings at their defaults for zero-dependency single-API operation. Linux instances can also configure optional Antigravity CLI task routing in `<instance>/antigravity_tasks.json` for `complex` QQ questions, `reflection` passes, and `diary` drafts. Whenever you want a separate model or CLI agent for redundancy or supervision, configure it in `/setup.html` or `<instance>/aux_config.json` and verify with `hakimi config check`. See [docs/configuration.md](docs/configuration.md) (Chinese) for schema details.

## Features

**Conversation & scheduling**
- Replies immediately to @-mentions and private messages while briefly buffering and merging consecutive clarifications from the same sender (using optional C++17 native rules or pure Python); unpacks nested forwarded QQ records for context review.
- Collects context and scores each turn before deciding to speak, yielding in busy groups; auto-quotes target messages when intervening replies arrive; opens topics on configurable cold-group windows.
- Per-user rate limit: one person can trigger at most 7 replies per rolling 30 minutes across routes, stopping spam and bounding cost.

**Memory, relationships & diary**
- Verbatim storage with SQLite FTS5 + optional exact local dense retrieval (`bge-small-zh-v1.5`), strictly isolated per group so private chats never leak into group replies.
- Per-person notebooks, affinity and closeness scores (sources tracked separately), familiarity, and memory graph; `hakimi.curation` updates event cards and impressions asynchronously after reply windows and quietly reviews members every two hours.
- Bilibili autobiography (`B站亲历`) recalled naturally on matching topics with tiered TTL expiry and physical pruning.
- Illustrated private diary written nightly at 22:30 (New York time) with LaTeX layout, PDF export, and optional delivery to verified owner DMs.

**Web retrieval & citations**
- Public web search and bounded HTTPS page reads (including news/encyclopedia recovery and anime calendar), strictly obeying per-user network bans without bypassing CAPTCHAs or paywalls.
- Immutable `[W1]` source snapshots (`web.sources`) scoped to delivered replies so follow-up quotes can inspect citations offline.
- Optional metered native Google Search Grounding, DeepSeek paid search, and `dsh_on_budget` fallback.

**Media & voice**
- Reads authorized images and watches Bilibili and QQ mini-program videos via on-demand frame sampling.
- Renders Markdown/TeX formulas locally as images; picks context-appropriate stickers and collects member stickers.
- Optional ordered multi-model AI drawing (off by default) with single quota reservation and daily/monthly caps configured in drawing settings.
- Optional QQ voice (`hakimi.voice`, off by default): local CPU speech-to-text (kept session-private, never saved to long-term memory) and explicit-request TTS/short humming using user-authorized reference clips only; see [docs/voice.md](docs/voice.md).

**Console & operations** (loopback only at `127.0.0.1:18768`, token-protected, reached through an SSH tunnel)
- Relationship graph, profiles across groups, message log, activity view (showing reply channel and trigger alongside model tier), legacy group/member matching, video activity, review history, model budget, drawing settings, and `/setup.html`.
- Local model gateway ledger with monthly cap enforcement; optional watchdog, Terra style supervision, Sol minimal repair, and expert help queue (all off by default).

The full feature list and toggles live in [docs/features.md](docs/features.md) (Chinese).

## Quick start (Linux server)

Requires Python 3.12+ and git; the video module also needs `ffmpeg`. Tested on Ubuntu and Debian. During installation, `setup.py` automatically compiles the optional C++17 follow-up classifier (`hakimi.bridge._followup_rules`) when a compiler is available and falls back seamlessly to pure Python otherwise.

```bash
git clone https://github.com/yemaodiaoling-max/Hakimi_Robot_Wither.git hakimi && cd hakimi
deploy/install.sh --modules qq,video,pet      # QQ bot only: --modules qq
# add --vector for local semantic memory search (about 500 MB)
```

The installer creates a virtual environment, the instance folder (default `~/hakimi-data`), the gateway certificate and the console token, and starts the **gateway** and the **console** as systemd user services. It never asks for keys.

Then open the setup panel:

```bash
# on your own computer: open a tunnel (the console listens on the server's loopback only)
ssh -L 18768:127.0.0.1:18768 user@server
# on the server: print the address with the token
HAKIMI_ROOT=~/hakimi-data .venv/bin/hakimi setup url
```

Open the printed `http://127.0.0.1:18768/setup.html#token=...` in your browser and fill in:

1. **Modules**: tick the modules you want.
2. **Model and key**: enter a Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey) or switch to **OpenAI-compatible** for DeepSeek / OpenRouter / local endpoints. Click “检测” (check) to list models, then pick your main and light models.
3. **Owner and persona**: the bot's name, what it should call you, and your ID.
4. **QQ bot**: create a bot on the [QQ open platform](https://q.qq.com) and enter its AppID, AppSecret and test group number.
5. **Bilibili**: log in by QR code (or paste a cookie); optionally choose a priority live room.
6. **Desktop pet**: choose local or SSH and copy the generated `pet_connection.json`.
7. **Optional — Advanced & independent APIs**: skip this if you want single-API mode; expand **「高级与独立 API」** only when wiring dedicated endpoints or CLI agents for review, QQ hot-backup, fallback, judges, or ops supervision.

Tick “保存后重启相关服务” (restart affected services after saving) and save: the panel writes each module's configuration and starts its services. Check them with `systemctl --user status 'hakimi-*'`.

> To keep the bot running after you log out of SSH: `sudo loginctl enable-linger $USER`.

## Desktop pet (Windows)

```powershell
git clone https://github.com/yemaodiaoling-max/Hakimi_Robot_Wither.git hakimi; cd hakimi
python -m venv .venv; .venv\Scripts\pip install -e .[pet]
# save the content generated in the setup panel's desktop pet section as desktop_pet\pet_connection.json
.venv\Scripts\python desktop_pet\pet_app.py
```

Choose “local” when the bot runs on the same Windows PC (the pet starts its back end locally), or “SSH” when it runs on a server (the pet talks to it over one persistent SSH connection). Every action on your computer (opening an app, running a command, keyboard or mouse input) first shows a confirmation button and only runs after you click “允许” (allow). Live Bilibili companion status can be queried via `desktop_pet\pet_cli.py bili-status`, and long-running watch shifts use fixed-file handoff (`rotate` / `handoff-sync`) to avoid context bloat.

## Persona and identity

Persona and prompts are text files, so no code changes are needed:

- **Persona**: edit it in the setup panel's persona section, or put a file with the same name in `<instance>/persona/` to override the packaged default in `src/hakimi/persona/`. What each file is for is listed in the [persona guide](src/hakimi/persona/README.md) (Chinese); the video module's persona is in `bili_companion/personas/`.
- **Owner and group vocabulary**: `<instance>/owner.json`, generated by the setup panel and open to manual additions (the comments below are for explanation only; the real file cannot contain comments):

  ```jsonc
  {
    "name": "主人",                 // what the bot calls you
    "short_name": "主人",           // shorter name for casual speech
    "aliases": ["your group card"], // other nicknames that identify you
    "ids": ["<official member id>"],// your accounts; they have admin rights
    "pet_session": "private:<id>",  // private session the desktop pet files its memories under
    "known_bot_ids": [],            // other bots in your groups, never treated as people
    "local": {
      "privacy_phrases": [],        // phrases that mark a desktop observation as a private chat (kept out of shared memory)
      "public_group_keywords": [],  // window titles containing these are public groups
      "signature_terms": [],        // running jokes and nicknames kept longer as core memories
      "no_instant_wake_ids": []     // spam accounts whose @-mentions do not wake the bot instantly
    }
  }
  ```

Restart the affected services afterwards (`systemctl --user restart 'hakimi-*'`).

## Commands

```bash
hakimi modules                 # installed / enabled modules (including aux_config.json ops gating)
hakimi arch                    # print the seven-layer package layout and module sizes
hakimi arch --check            # statically verify downward-only import direction
hakimi setup status            # what is configured (keys are never shown)
hakimi gateway status          # gateway: upstream, model mapping, requests and estimated cost this month
hakimi gateway models          # models available to the upstream key
hakimi config check            # validate instance configuration files (including aux_config.json)
hakimi features                # feature list and on/off state
systemctl --user status 'hakimi-*'
journalctl --user -u hakimi-agent -f   # follow the reply service log
```

## Updating

```bash
cd hakimi && git pull
.venv/bin/pip install -e ".[qq,video]"        # match the modules you use; skip if dependencies did not change
systemctl --user restart 'hakimi-*.service'
```

Data and configuration in the instance folder are not touched by updates.

## Data and privacy

- **Keys stay on your server**: the main model key is stored in the gateway folder (mode 0600) and no module process can read it; the QQ AppSecret, fallback keys, and independent auxiliary API keys configured in `/setup.html` (stored under `provider` and `aux` in `credentials.fernet`) are Fernet-encrypted in `~/.config/hakimi/`; the Bilibili cookie is encrypted in the video module's private folder. The panel only shows “saved / not set” and never echoes a value.
- **Chats, memory and diaries live in the instance folder**, not in the code repository; `.gitignore` excludes these folders. Do not commit your instance folder to git.
- **Privacy boundaries**: memory is scoped per group and group replies never quote private chats; when the desktop pet sees you chatting privately with someone it keeps you company without writing to shared memory or shift handoffs; modules exchange only privacy-sanitized summaries and never duplicate private messages or account IDs.
- **Console security**: listens on `127.0.0.1` only, requires the token, and checks same-origin on every write.

## Development and tests

```bash
python -m hakimi arch --check                    # enforce seven-layer import direction
python tests/run_tests.py                        # main suite (cases in tests/known_stale.py run as expected failures)
cd bili_companion && PYTHONPATH=src python -m unittest discover -s tests
```

- The code layout and layer boundaries are described in [docs/architecture.md](docs/architecture.md) (Chinese). Register new features in `src/hakimi/features/registry.py`, then refresh the feature list with `hakimi features --markdown`.
- Tests use only scrubbed data and fake identities (`tests/fixtures/`); they never send real messages or model requests.
- The main suite currently has a few known stale cases; please make sure a change adds no new test or architecture failures.
- Issues and pull requests are welcome.

## Documentation

The documents are written in Chinese.

- [Modules](docs/modules.md): what each module installs, which services it starts and how they share memory
- [Architecture](docs/architecture.md): seven-layer layout, import rules and code-vs-data separation
- [Model gateway](docs/gateway.md): model mapping, unit prices and monthly cap, proxy, compatible endpoints
- [Configuration](docs/configuration.md) · [Features](docs/features.md) · [Web search routing](docs/search-routing.md)
- [Memory & Bilibili bridge](docs/memory_operation.md) · [Diary](docs/diary_operation.md) · [Math rendering](docs/math_tool.md) · [Bilibili video observation](docs/bilibili_video_operation.md) · [Fallback model](docs/fallback_operation.md) · [Voice](docs/voice.md)
- [Persona and prompts](src/hakimi/persona/README.md): rename the bot, change its personality, add style samples
- Video module: [bili_companion/README.md](bili_companion/README.md)

## License

[MIT](LICENSE).

This project is not affiliated with Tencent, Bilibili or Google. Follow each platform's rules when using a QQ bot or a Bilibili account; automatic danmaku and comments are off by default, so check that they comply before turning them on.
