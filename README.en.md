# Hakimi

[简体中文](README.md) · **English**

A cat-like AI companion that lives in your QQ groups, on Bilibili and on your desktop. All three forms share **one memory core**: what it chatted about in a QQ group, the videos it watched on Bilibili and the evenings it spent on your desktop while you coded all become memories of the same Hakimi, and it brings them up naturally the next time you talk.

- ⚡ **Runs out of the box with one API key—or splits roles across many**: enter **one single API key** (Google Gemini, Gemini-compatible, or any OpenAI-compatible endpoint) for the full core experience; fallback responders, post-reply reviews, context judges, search pre-routing and ops supervision are all off by default or reuse the main gateway, and can each be enabled and pointed at independent APIs or CLIs.
- 🐱 **A group member with timing**: answers right away when @-mentioned; otherwise it reads the room before deciding whether to join in, starts topics when the group goes quiet and holds back when it is busy.
- 🧠 **Long-term memory and a social graph**: remembers what each member said, their nicknames and running jokes, keeps a notebook on everyone with affinity and closeness that change over time, and writes a private diary every night.
- 📺 **Watches Bilibili on its own**: picks recommended videos by interest and keeps an eye on a live room you choose; what it watched becomes something to talk about in QQ.
- 🖥️ **Desktop pet**: sits on your Windows desktop, comments on your screen, chats by voice and operates your computer after you confirm.
- 🔐 **Your keys stay on your server**: every model request goes through a local gateway with one cost ledger and an optional monthly spending cap; auxiliary API keys are stored separately and encrypted.

> The bot speaks Chinese and its default persona is written in Chinese; the persona files are plain text you can rewrite in any language.

## Contents

- [Modules](#modules) · [Architecture](#architecture) · [Single-API Default and Independent Auxiliaries](#single-api-default-and-independent-auxiliaries) · [Features](#features)
- [Quick start (Linux server)](#quick-start-linux-server) · [Desktop pet (Windows)](#desktop-pet-windows)
- [Persona and identity](#persona-and-identity) · [Commands](#commands) · [Updating](#updating)
- [Data and privacy](#data-and-privacy) · [Development and tests](#development-and-tests) · [Documentation](#documentation) · [License](#license)

## Modules

One required **memory core** plus three optional modules, in any combination: install one, two or all of them. A module that is not enabled never starts and does not affect the others.

| Module | What it does | Runs on |
|---|---|---|
| **Memory core** `core` (required) | Long-term memory (full-text search, optional semantic search), social graph and affinity, diary, info console, setup panel, local model gateway | Linux server (also runs on Windows) |
| **QQ bot** `qq` | Uses the official QQ bot platform: group and private chat, instant replies to @-mentions, starting conversations in quiet groups, stickers, formulas rendered as images, image and video understanding, optional AI drawing, daily diary | Same as above |
| **Video bot** `video` | Browses Bilibili recommendations by itself, watches a chosen live room, optionally sends danmaku and comments; what it watched goes into the shared memory | Linux server (needs `ffmpeg`) |
| **Desktop pet** `pet` | Windows desktop pet: comments on your screen, voice chat, helps you operate the computer; its experiences sync into memory | Windows PC |

## Architecture

```
                 ┌──────────────── memory core ────────────────┐
                 │ memory.sqlite3 · social graph · diary       │
  QQ bot ──────▶ │ info console / setup panel (:18768)         │ ◀─── desktop pet (Windows, local or SSH)
                 │ local gateway (:18765) ─▶ Gemini / OpenAI   │
                 └────────────────▲───────────────────────┬────┘
                                  │ summaries every 30m   │ optional auxiliaries (aux_config.json)
                           video bot (Bilibili)           └─▶ independent HTTP APIs · optional CLI agents
```

- **One codebase, config-only differentiation**: a minimal single-API install runs the exact same repository code as a multi-provider, multi-agent production deployment; extra responders and supervisors are turned on only by files inside your **instance folder** (default `~/hakimi-data`).
- **Package layout**: the core is the installable Python package `hakimi` (`src/hakimi/`); the video module is the `bili_companion/` package in the same repository.
- **One upstream API is enough**: all core calls go through the local gateway at `https://127.0.0.1:18765`, which talks to Google Gemini, Gemini-compatible endpoints, or any OpenAI Chat Completions compatible API (DeepSeek, OpenRouter, local servers, etc.).

## Single-API Default and Independent Auxiliaries

Every secondary model call and background supervisor is decoupled behind `<instance>/aux_config.json` and the collapsed **“高级与独立 API” (Advanced & independent APIs)** section in `/setup.html`:

| Capability | Key in `aux_config.json` | Open-source default | Optional independent modes |
|---|---|---|---|
| **Post-reply review & daily events** | `review` | `mode: "main_gateway"` (reuses main gateway) | `custom_api` (separate OpenAI/Gemini HTTP API), `companion_duty`, or `disabled` |
| **QQ primary hot-backup route** | `qq_backup` | `enabled: false` (`mode: "disabled"`) | `custom_api` (separate OpenAI/Gemini HTTP API with optional `fallback_model`) or `companion_duty` |
| **Timeout / outage fallback responder** | `fallback` | `enabled: false` (`backend: "http_api"`) | Enable with an independent HTTP API (no external CLI required) or external `dsh` CLI |
| **Context & continuation judge** | `context_judge` | `mode: "heuristic"` (local rules, zero API calls) | `main_gateway`, `custom_api`, or `dsh` |
| **Search pre-routing & paid search** | `search_routing` | `enabled: false`, `judge_mode: "local_rules"`, `paid_search_mode: "disabled"` | Enable pre-search judge (`main_gateway` / `custom_api` / `dsh`) and `deepseek` paid search; manual `web.search` still uses free feeds when disabled |
| **Daily diary fallback** | `diary.fallback_enabled` | `false` (main model only) | `true` (falls back to the configured `fallback` backend if primary fails) |
| **Ops watchdog / Terra supervision / Sol repair / Help queue** | `ops.*` | All 4 toggles `false` (zero extra supervisor processes) | Toggle each independently (`watchdog_enabled`, `terra_supervision_enabled`, `sol_repair_enabled`, `codex_help_enabled`) with `main_gateway`, `custom_api`, or `codex_cli` |

> Leave all auxiliary settings at their defaults for zero-dependency single-API operation. Whenever you want a separate model or CLI agent for redundancy or supervision, configure it in `/setup.html` or `<instance>/aux_config.json` and verify with `hakimi config check`. See [docs/configuration.md](docs/configuration.md) (Chinese) for schema details.

## Features

**Conversation**
- Replies immediately to @-mentions and private messages; otherwise it collects context and scores each turn before deciding to speak, and yields in busy groups.
- Starts a topic on its own when a group has been quiet for a while (configurable time windows).
- Per-user rate limit: one person can trigger at most 7 replies per 30 minutes, which stops spam and keeps costs down.
- Fallback responders, separate review/backup APIs, LLM context judges, search pre-classifiers, and ops supervision agents are **off by default or reuse the main gateway**—and can each be enabled and pointed at separate APIs or CLIs as shown above.

**Memory and relationships**
- Messages are stored verbatim, with full-text plus optional semantic hybrid search; memory is scoped per group, so private chats never leak into group replies.
- Per-person notebooks, affinity and closeness scores (with their sources recorded separately), interaction familiarity, and a memory graph.
- After each reply window it reviews the conversation asynchronously and updates event cards and impressions; every two hours it quietly reviews a few members (no messages are sent).
- Writes an illustrated private diary every night, exportable as PDF.

**Media**
- Reads images (only those authorised for the current turn) and watches Bilibili and QQ mini-program videos by sampling frames on demand.
- Renders Markdown/TeX formulas locally into images.
- Picks stickers from a library to fit the context and collects members' stickers.
- Optional AI drawing: off by default; set your own image model endpoint and key, plus daily and monthly spending caps, on the console's drawing settings page.

**Console** (loopback only, token-protected, reached through an SSH tunnel)
- Relationship view (graph, people, affinity and profiles across groups), message log, legacy group and member matching, video activity, review history, model budget, drawing settings.
- Setup panel: module switches, model and key, owner and persona, QQ bot, Bilibili login, desktop pet connection, plus the collapsed **“高级与独立 API” (Advanced & independent APIs)** section; saving writes configuration files and restarts affected services.

**Operations**
- Local model gateway: one ledger, cost estimated from your unit prices, optional monthly cap; at the cap the bot switches to your configured backup/fallback route or pauses video watching.
- Optional health watchdog, Terra style supervision, Sol minimal repair, and help queue (all off by default; configurable in the panel or `aux_config.json`).

The full list, with the switch for each feature, is in [docs/features.md](docs/features.md) (Chinese).

## Quick start (Linux server)

Requires Python 3.12+ and git; the video module also needs `ffmpeg`. Tested on Ubuntu and Debian.

```bash
git clone https://github.com/yemaodiaoling-max/Hakimi_Robot_Wither.git hakimi && cd hakimi
deploy/install.sh --modules qq,video,pet      # QQ bot only: --modules qq
# add --vector for semantic memory search (about 500 MB)
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

Choose “local” when the bot runs on the same Windows PC (the pet starts its back end locally), or “SSH” when it runs on a server (the pet talks to it over one persistent SSH connection). Every action on your computer (opening an app, running a command, keyboard or mouse input) first shows a confirmation button and only runs after you click “允许” (allow).

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
- **Privacy boundaries**: memory is scoped per group and group replies never quote private chats; when the desktop pet sees you chatting privately with someone it keeps you company but writes nothing to shared memory; modules exchange only curated summaries, never private messages or account data.
- **Console security**: listens on `127.0.0.1` only, requires the token, and checks same-origin on every write.

## Development and tests

```bash
python tests/run_tests.py                       # main suite (cases in tests/known_stale.py run as expected failures)
cd bili_companion && PYTHONPATH=src python -m unittest discover -s tests
```

- The code layout is described in [docs/architecture.md](docs/architecture.md) (Chinese). Register new features in `src/hakimi/features/registry.py`, then refresh the feature list with `hakimi features --markdown`.
- Tests use only scrubbed data and fake identities (`tests/fixtures/`); they never send real messages or model requests.
- The main suite currently has a few known failures; please make sure a change adds no new ones.
- Issues and pull requests are welcome.

## Documentation

The documents are written in Chinese.

- [Modules](docs/modules.md): what each module installs, which services it starts and how they share memory
- [Model gateway](docs/gateway.md): model mapping, unit prices and monthly cap, proxy, compatible endpoints
- [Configuration](docs/configuration.md) · [Architecture](docs/architecture.md) · [Features](docs/features.md)
- [Memory](docs/memory_operation.md) · [Diary](docs/diary_operation.md) · [Math rendering](docs/math_tool.md) · [Fallback model](docs/fallback_operation.md) · [Voice](docs/voice.md)
- [Persona and prompts](src/hakimi/persona/README.md): rename the bot, change its personality, add style samples
- Video module: [bili_companion/README.md](bili_companion/README.md)

## License

[MIT](LICENSE).

This project is not affiliated with Tencent, Bilibili or Google. Follow each platform's rules when using a QQ bot or a Bilibili account; automatic danmaku and comments are off by default, so check that they comply before turning them on.
