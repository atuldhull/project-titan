# PROJECT TITAN — Autonomous Software Engineering Platform

## Volume 4 — Frontend & Dashboard (UI/UX Specification)

| Field | Value |
|---|---|
| Series | PROJECT TITAN — Autonomous Software Engineering Platform |
| Volume | 4 of 10 — Frontend & Dashboard |
| Doc ID | TITAN-VOL-04 |
| Status | Draft v1.0 |
| Date | 2026-07-04 |
| Requirement prefix | TITAN-FRT-NNN |

**Abstract.** This volume is the implementation-ready UI/UX specification for the TITAN web dashboard — the interface through which a human client observes and steers an autonomous AI software organization. It defines the information architecture, the design system (Tailwind CSS + shadcn/ui, dark mode default), the state-management model (Next.js 15 server components + React Query + a multiplexed WebSocket sync layer), and screen-by-screen specifications with ASCII wireframes for thirteen surfaces: Organization Overview, Projects, Kanban Board, Task/Dependency Graph, Agent Status Grid, Live Agent Conversations, Logs Viewer, Architecture Viewer, Deployment Dashboard, Monitoring, Memory Explorer, Plugin Manager, and Settings — each with purpose, layout, key components, loading/empty/error states, interactions, and realtime behavior. Responsive rules, WCAG 2.1 AA accessibility, keyboard shortcuts, notifications, and the frontend folder structure complete the volume. Canonical stack decisions are justified in Volume 1; this volume consumes them.

---

## Table of Contents

1. [Introduction & Scope](#1-introduction--scope)
2. [Information Architecture & Navigation](#2-information-architecture--navigation)
3. [Design System](#3-design-system)
4. [State Management & Data Flow](#4-state-management--data-flow)
5. [Realtime: WebSocket Synchronization](#5-realtime-websocket-synchronization)
6. [Screen Specifications](#6-screen-specifications)
7. [Notifications System](#7-notifications-system)
8. [Responsive Behavior](#8-responsive-behavior)
9. [Accessibility (WCAG 2.1 AA)](#9-accessibility-wcag-21-aa)
10. [Keyboard Shortcuts](#10-keyboard-shortcuts)
11. [Frontend Folder Structure](#11-frontend-folder-structure)
12. [Decision Records](#12-decision-records)
13. [Risks & Open Questions](#13-risks--open-questions)
14. [Cross-References](#14-cross-references)

---

## 1. Introduction & Scope

### 1.1 Purpose

TITAN's dashboard is the client's window into an autonomous engineering company: the human types a goal ("Build me a Netflix clone"), then watches agents plan, build, review, and deploy. The UI therefore optimizes for **observability of autonomous work**: live status, live conversations, live logs, and clear intervention points (approve, pause, redirect) are first-class; forms and CRUD are secondary.

### 1.2 In scope / out of scope

**In scope:** all dashboard screens, navigation, design system, realtime sync, notifications, responsiveness, accessibility, keyboard shortcuts, state management, folder structure.
**Out of scope:** REST/WS payload schemas (Volume 3), agent behavior (Volume 2), memory internals (Volume 5), deployment pipelines (Volume 9). Where this volume names an endpoint or event, the authoritative schema is Volume 3's.

### 1.3 Canonical stack (consumed, not re-decided)

Next.js 15 (App Router) + React + TypeScript (strict) + Tailwind CSS + shadcn/ui, inside the monorepo under `apps/web`. Realtime via WebSockets served by FastAPI. Auth via OAuth2/OIDC + JWT with RBAC. Everything must run locally under Docker Compose with free/local tools (local-first is a hard requirement); production targets Kubernetes.

### 1.4 Global requirements

| ID | Priority | Requirement | Acceptance criteria |
|---|---|---|---|
| TITAN-FRT-001 | MUST | The dashboard SHALL be a Next.js 15 App Router application in TypeScript strict mode with zero `any` in app code. | `tsc --noEmit` passes with `"strict": true`; ESLint rule `@typescript-eslint/no-explicit-any` = error; CI blocks merge on violation. |
| TITAN-FRT-002 | MUST | Every screen SHALL define and render distinct loading, empty, error, and populated states. | Storybook story exists per state per screen; visual regression suite snapshots all four states. |
| TITAN-FRT-003 | MUST | Any data visible on screen that changes server-side SHALL update in the UI within 2 s of the server event, without page reload. | E2E test: mutate via API, assert DOM update ≤ 2 s on Kanban, Agent Grid, Conversations, Logs, Deployments. |
| TITAN-FRT-004 | MUST | The dashboard SHALL function fully against a local Docker Compose backend with no external network access (local LLMs via Ollama). | Playwright suite passes with machine offline except loopback. |
| TITAN-FRT-005 | MUST | First Load JS for the Organization Overview route SHALL be ≤ 300 kB gzipped; heavy visualizations (graphs, charts, terminals) SHALL be dynamically imported. | `next build` budget check in CI fails the build when exceeded. |

---

## 2. Information Architecture & Navigation

### 2.1 Hierarchy

Data hierarchy is **Organization → Projects → (Tasks, Agents, Conversations, Deployments, Artifacts)**. Agents, Memory, Plugins, and Settings are organization-scoped; Board, Graph, and Architecture are project-scoped. The URL is the single source of navigational truth — selected project, filter, tab, and selected node are path- or query-encoded, so links are shareable and refresh-safe.

### 2.2 Route map

```
/                                → redirect → /org
/org                             Organization Overview
/projects                        Projects list
/projects/[pid]                  Project detail (Overview tab)
/projects/[pid]/board            Kanban board
/projects/[pid]/graph            Task / Dependency graph
/projects/[pid]/architecture     Architecture viewer
/projects/[pid]/deployments      Project deployments
/agents                          Agent status grid
/agents/[agentId]                Agent detail drawer route (intercepted)
/conversations                   Live agent conversations (channel list + feed)
/conversations/[channelId]       Specific channel
/logs                            Logs viewer (org-wide; ?project= filters)
/deployments                     Deployment dashboard (org-wide)
/monitoring                      Monitoring
/memory                          Memory explorer
/plugins                         Plugin manager
/settings/{profile|appearance|providers|members|api-keys|notifications}
```

### 2.3 App shell

Persistent shell: a 240 px left sidebar (collapsible to 64 px icon rail, state persisted in `localStorage`), a 56 px top bar, and a content region. The top bar contains: project switcher (`Ctrl/⌘+P`), global command palette trigger (`Ctrl/⌘+K`), connection-status indicator (WS live/reconnecting/offline), notification bell with unread badge, and user menu.

```
+------------------------------------------------------------------+
| ◉ TITAN   [Project: netflix-clone ▾]   ⌘K Search…   ⚡live 🔔3 👤 |
+----------+-------------------------------------------------------+
| ▣ Org    |                                                       |
| ▤ Projects                                                       |
| ▥ Board  |                                                       |
| ⬡ Graph  |                  <route content>                      |
| ⚉ Agents |                                                       |
| 💬 Convos|                                                       |
| ≡ Logs   |                                                       |
| ⌂ Arch   |                                                       |
| 🚀 Deploy|                                                       |
| 📈 Monitor                                                       |
| 🧠 Memory|                                                       |
| 🔌 Plugins                                                       |
| ⚙ Settings                                                       |
+----------+-------------------------------------------------------+
```

| ID | Priority | Requirement | Acceptance criteria |
|---|---|---|---|
| TITAN-FRT-006 | MUST | All navigational state (selected project, tab, filters, selected graph node) SHALL be URL-encoded and restorable on reload. | Deep-link E2E test per screen restores identical view. |
| TITAN-FRT-007 | MUST | A command palette (`Ctrl/⌘+K`) SHALL navigate to any screen, project, agent, or task by fuzzy search. | Palette opens ≤ 100 ms; typing "kan net" surfaces the netflix-clone board. |
| TITAN-FRT-008 | MUST | Navigation items SHALL be filtered by RBAC role from the JWT (e.g., `viewer` sees no Settings→Members, no destructive actions). | Role-matrix test renders sidebar per role and asserts visibility. |
| TITAN-FRT-009 | SHOULD | The sidebar SHALL show per-item live badges (running agents count, failing deployments) via the notification store. | Badge updates within 2 s of WS event. |

---

## 3. Design System

### 3.1 Foundations

Tailwind CSS is the styling layer; shadcn/ui provides the vendored component primitives (Button, Dialog, DropdownMenu, Table, Tabs, Toast, Command, Sheet, Tooltip, Badge, Card, Skeleton...). All colors are expressed as CSS custom properties consumed by Tailwind semantic tokens — components never hardcode hex values.

### 3.2 Color tokens

Dark mode is the **default**; light mode is a supported user preference (`class` strategy: `<html class="dark">` by default, toggled in Settings → Appearance, persisted per user and pre-hydrated via a blocking inline script to prevent flash).

| Token | Dark (default) | Light | Usage |
|---|---|---|---|
| `--background` | `hsl(224 20% 7%)` | `hsl(0 0% 100%)` | App background |
| `--card` | `hsl(224 18% 10%)` | `hsl(0 0% 98%)` | Cards, panels |
| `--foreground` | `hsl(215 20% 92%)` | `hsl(224 20% 10%)` | Primary text |
| `--muted-foreground` | `hsl(217 12% 62%)` | `hsl(220 10% 40%)` | Secondary text |
| `--primary` | `hsl(210 100% 60%)` | `hsl(212 92% 45%)` | Actions, links, focus |
| `--destructive` | `hsl(0 72% 56%)` | `hsl(0 74% 46%)` | Errors, dangerous actions |
| `--success` | `hsl(150 62% 45%)` | `hsl(152 60% 34%)` | Healthy, passed |
| `--warning` | `hsl(38 92% 55%)` | `hsl(35 92% 40%)` | Degraded, pending |
| `--border` | `hsl(224 14% 18%)` | `hsl(220 12% 88%)` | Borders, dividers |
| `--ring` | `hsl(210 100% 60%)` | `hsl(212 92% 45%)` | Focus rings |

Status colors map 1:1 to domain states everywhere (agent state, task state, deployment state): `success`=running/done/healthy, `warning`=waiting/review/degraded, `destructive`=failed/blocked, `muted`=idle/draft. Status is **never** conveyed by color alone — always paired with an icon or label (see §9).

### 3.3 Typography, spacing, elevation

- Fonts: `Inter` (UI) and `JetBrains Mono` (code, logs, IDs), self-hosted via `next/font` (no external font CDN — offline requirement).
- Type scale: 12 / 13 / 14 (body default) / 16 / 18 / 22 / 28 px, line-height 1.5.
- Spacing: Tailwind 4 px grid; density is compact — tables at 36 px row height, 44 px touch targets on mobile.
- Elevation: borders over shadows in dark mode; radius `--radius: 8px`.
- Motion: 120–200 ms ease-out for micro-interactions; all animation respects `prefers-reduced-motion` (§9).

| ID | Priority | Requirement | Acceptance criteria |
|---|---|---|---|
| TITAN-FRT-010 | MUST | All components SHALL consume semantic CSS-variable tokens; raw palette values in app code are forbidden. | Lint rule (custom Tailwind plugin) rejects arbitrary hex/rgb values in `apps/web`. |
| TITAN-FRT-011 | MUST | Dark theme is default; theme choice SHALL persist per user and apply before first paint (no flash of wrong theme). | Lighthouse trace shows no theme flip; new anonymous session renders dark. |
| TITAN-FRT-012 | MUST | Both themes SHALL meet WCAG 2.1 AA contrast (≥ 4.5:1 body text, ≥ 3:1 large text/UI components). | axe-core CI run reports zero contrast violations in both themes. |
| TITAN-FRT-013 | SHOULD | shadcn/ui components SHALL be vendored under `components/ui` and modified in-place rather than wrapped ad hoc. | No second styling wrapper layer exists; changes reviewed like normal code. |

---

## 4. State Management & Data Flow

### 4.1 Model

Three complementary layers, chosen per data class (ADR-FRT-01):

1. **React Server Components (RSC)** render the first paint of every route from FastAPI REST endpoints (server-side fetch with the user's JWT); read-only content (settings pages, metadata headers) ships no client JS.
2. **TanStack React Query v5** owns all client server-state: lists, boards, grids. RSC data is passed as `initialData` via the Hydration API so the cache starts warm. Structured keys: `['project', pid, 'tasks', filters]`.
3. **WebSocket sync layer** (§5) invalidates or patches React Query caches on server events; `refetchInterval` is only a 60 s safety net on critical screens.

Pure client UI state (sidebar collapsed, palette open, drawer selection) lives in **Zustand** stores. Forms use `react-hook-form` + `zod`, with API types generated from the FastAPI OpenAPI spec (`openapi-typescript` in `packages/api-client`).

### 4.2 Mutations

All mutations use React Query `useMutation`: optimistic update where the outcome is locally predictable (Kanban drag, toggle), rollback on error with a toast, and cache invalidation driven by the WS confirmation event rather than the HTTP response — the WS event is the canonical "it happened" signal, deduplicated by `seq`.

| ID | Priority | Requirement | Acceptance criteria |
|---|---|---|---|
| TITAN-FRT-014 | MUST | Server state SHALL be managed exclusively by React Query; no bespoke fetch-in-`useEffect` patterns. | Lint rule bans `fetch` outside `packages/api-client` and route handlers; code review checklist item. |
| TITAN-FRT-015 | MUST | Optimistic mutations SHALL roll back on failure and surface a toast with a retry action. | E2E test: kill API mid-drag on Kanban → card returns to origin column, toast shown. |
| TITAN-FRT-016 | MUST | API types SHALL be generated from the backend OpenAPI schema; hand-written duplicates are forbidden. | CI job regenerates types and fails on uncommitted diff. |
| TITAN-FRT-017 | SHOULD | RSC SHALL provide `initialData` hydration so no visible refetch flash occurs on route entry. | No loading skeleton on warm navigation to Projects list in E2E trace. |

---

## 5. Realtime: WebSocket Synchronization

### 5.1 Connection model

One multiplexed WebSocket per browser tab to FastAPI at `wss://<host>/ws?token=<jwt>` (ADR-FRT-03). The client subscribes/unsubscribes to **topics** as routes mount/unmount:

```
Client                                 FastAPI /ws
  | connect (JWT)                          |
  |--------------------------------------->|  auth, register session
  | {op:"sub", topics:["project:42:tasks", |
  |   "agents:status","notifications:me"]} |
  |--------------------------------------->|
  |        {topic, event, seq, ts, payload}|
  |<---------------------------------------|  fan-out from Redis pub/sub
  | {op:"ping"} / {op:"pong"} every 25s    |
```

Envelope: `{ topic: string, event: string, seq: number, ts: string, payload: T }`. `seq` is monotonic per topic; a gap triggers a targeted React Query refetch — the same reconciliation path used after reconnect.

### 5.2 Core topics and events

| Topic | Events | Consumed by |
|---|---|---|
| `org:{oid}:activity` | `activity.created` | Organization Overview feed |
| `project:{pid}:tasks` | `task.created/updated/moved/completed` | Kanban, Graph, Project detail |
| `agents:status` | `agent.status_changed`, `agent.heartbeat` | Agent grid, sidebar badges |
| `channel:{cid}:messages` | `message.created`, `message.chunk` (token stream) | Conversations |
| `logs:{stream}` | `log.appended` (batched ≤ 100/frame) | Logs viewer |
| `project:{pid}:deployments` | `deployment.status_changed`, `deployment.step` | Deployment dashboard |
| `monitoring:summary` | `metric.sample` (1 Hz aggregate) | Monitoring, Overview KPIs |
| `notifications:{userId}` | `notification.created` | Bell, toasts |

### 5.3 Degradation

Reconnect uses exponential backoff (1 s → 30 s cap, jitter). While disconnected the top bar shows an amber "reconnecting" pill; after 60 s a banner offers manual retry and React Query falls back to 30 s polling on the active screen, keeping the app usable behind hostile proxies.

| ID | Priority | Requirement | Acceptance criteria |
|---|---|---|---|
| TITAN-FRT-018 | MUST | The client SHALL maintain exactly one WS connection per tab, with topic-scoped subscribe/unsubscribe on route change. | Network inspector shows 1 socket across full navigation; server session log shows sub/unsub pairs. |
| TITAN-FRT-019 | MUST | On reconnect or `seq` gap, affected caches SHALL be re-fetched; no stale or duplicated items may remain. | Chaos test: drop socket during 50 task mutations → board converges to server state, no dupes. |
| TITAN-FRT-020 | MUST | Connection state SHALL be always visible (live / reconnecting / offline) in the top bar. | Visual states verified in Storybook + E2E toggling network. |
| TITAN-FRT-021 | SHOULD | High-frequency events (logs, token streams, metrics) SHALL be batched and applied via `requestAnimationFrame` to keep interaction latency < 100 ms. | Performance test: 1,000 log lines/s keeps INP < 200 ms. |

---

## 6. Screen Specifications

Conventions for this section: every screen lists Purpose, Layout (wireframe), Key components, States, Interactions, and Realtime. All list screens use skeleton loaders shaped like their content (not spinners); all error states show the failed scope inline (not a blank page) with a Retry button and error reference ID; all empty states include a one-line explanation and a primary action.

### 6.1 Organization Overview (`/org`)

**Purpose.** The landing screen: answers "what is my AI company doing right now?" in under five seconds.

```
+------------------------------------------------------------------+
| Organization Overview                          [ + New Project ] |
+-------------+-------------+-------------+------------------------+
| Projects  4 | Agents 12/15| Tasks 37    | Cost today  $4.12      |
| 2 active    | ▲ 9 working | 5 blocked ⚠ | 61k tok  [budget 68%▓] |
+-------------+-------------+-------------+------------------------+
| Active Projects                | Live Activity                   |
| ┌────────────────────────────┐ | 12:04 QA-1 opened bug TASK-231  |
| │ netflix-clone   ▓▓▓▓░ 71%  │ | 12:04 BE-2 merged PR #88        |
| │ sprint 3 · 9 agents · ✔ CI │ | 12:03 Architect approved ADR-7  |
| ├────────────────────────────┤ | 12:01 Deploy staging ✔ v0.4.1   |
| │ banking-sys     ▓▓░░░ 34%  │ | 11:58 CEO re-prioritized epic 3 |
| └────────────────────────────┘ | …infinite scroll…               |
+--------------------------------+---------------------------------+
| Needs Your Attention (2): [Approve release v0.4.1] [Unblock T-231]|
+------------------------------------------------------------------+
```

- **Key components:** four KPI stat cards; active-project cards (progress, sprint, agent count, CI status); virtualized live-activity feed; "Needs Your Attention" queue of human-approval items (release approvals, blocked decisions, budget overruns).
- **States:** loading = skeleton cards + shimmer feed; empty = onboarding hero "Create your first project — describe what you want built" with prompt input; error = per-panel inline error, KPIs degrade independently.
- **Interactions:** KPI cards deep-link to filtered screens (e.g., "5 blocked" → Kanban filtered `state=blocked`); attention items open an approval dialog with context and Approve/Reject/Comment; activity rows link to their entity.
- **Realtime:** subscribes `org:{oid}:activity`, `agents:status`, `monitoring:summary`. Feed prepends items with a subtle 200 ms highlight; KPIs patch in place.

**TITAN-FRT-022 (MUST).** Human-approval items SHALL appear on the Overview within 2 s of creation and be actionable in ≤ 2 clicks. *AC:* E2E — backend requests release approval; approve via Overview dialog; workflow proceeds.

### 6.2 Projects List (`/projects`) & Project Detail (`/projects/[pid]`)

**Purpose.** Portfolio management: create, find, and inspect projects.

```
/projects
+------------------------------------------------------------------+
| Projects   [Search…] [Status ▾] [Sort: Activity ▾]  [+ New]      |
+------------------------------------------------------------------+
| ● netflix-clone   building   ▓▓▓▓░ 71%  9 agents  CI ✔  2m ago   |
| ● banking-sys     planning   ▓▓░░░ 34%  4 agents  CI –  1h ago   |
| ○ hospital-mgmt   paused     ▓▓▓░░ 55%  0 agents  CI ✔  3d ago   |
+------------------------------------------------------------------+

/projects/[pid]   (tabs: Overview | Board | Graph | Architecture |
                         Deployments | Files | Settings)
+------------------------------------------------------------------+
| netflix-clone  · building · sprint 3/6        [⏸ Pause] [⋯]      |
| Milestones: [M1 ✔]—[M2 ✔]—[M3 ▓▓░ 64%]—[M4]—[Release]           |
| Requirements summary | Team (agents) | Recent decisions (ADRs)   |
| Risk register        | Tech-debt report | Sprint burndown        |
+------------------------------------------------------------------+
```

- **Key components:** filterable table/card list; "New Project" wizard (a chat-style intake where the CEO Agent asks its essential clarification questions before the roadmap is generated); detail tabs; milestone timeline; auto-generated artifacts (roadmap, risk analysis, tech-debt report) rendered from backend Markdown.
- **States:** loading skeleton rows; empty = same onboarding hero as §6.1; error inline. Detail tab errors isolate to the tab.
- **Interactions:** row click → detail; Pause/Resume gates all agent work on the project (confirmation dialog, RBAC `admin|owner`); wizard streams the CEO Agent's questions token-by-token.
- **Realtime:** `project:{pid}:tasks` and `org:{oid}:activity` keep progress bars, CI badges, and milestone bars current.

**TITAN-FRT-023 (MUST).** Project creation SHALL be a conversational intake that yields a visible roadmap + milestone plan without a page reload. *AC:* E2E from prompt to rendered roadmap on detail Overview tab.

### 6.3 Kanban Board (`/projects/[pid]/board`)

**Purpose.** The canonical view of task flow through the autonomous pipeline.

```
+------------------------------------------------------------------+
| Board · sprint 3  [Filter: assignee ▾ label ▾ ⌘F]   [⚙ columns]  |
+-----------+-----------+-----------+-----------+------------------+
| Backlog 14| Doing 6   | Review 3  | QA 2      | Done 41          |
| ┌───────┐ | ┌───────┐ | ┌───────┐ | ┌───────┐ | ┌───────┐        |
| │T-240  │ | │T-231 ⚠│ | │T-228  │ | │T-226  │ | │T-225 ✔│        |
| │Auth   │ | │Payment│ | │PR #88 │ | │e2e:   │ | │Schema │        |
| │API    │ | │BE-2 ● │ | │Rev-1 ●│ | │QA-1 ● │ | │DB-1   │        |
| │3 pts  │ | │blocked│ | │       │ | │       │ | │       │        |
| └───────┘ | └───────┘ | └───────┘ | └───────┘ | └───────┘        |
+-----------+-----------+-----------+-----------+------------------+
```

- **Key components:** columns mapped to workflow states (configurable per project from the Volume 6 workflow definition); task cards (ID, title, agent avatar, points, labels, blocked badge); WIP-limit indicators; task detail Sheet (right slide-over) with description, acceptance criteria, dependency links, linked PR/conversation, and full event history.
- **States:** loading = 5 skeleton columns × 3 skeleton cards; empty = "No tasks yet — the Project Manager Agent creates tasks after planning completes" with link to project status; error = board-level retry panel; a column stream failure shows a stale-data badge on that column.
- **Interactions:** drag-and-drop between columns (`@dnd-kit`, optimistic, RBAC-gated — moving a card is a human override that notifies the PM Agent); `⌘F` filter; card click opens Sheet (route `?task=T-231` for deep-linking); keyboard DnD per §10.
- **Realtime:** `task.moved/updated/created` patch cards in place; a card moved by an agent animates (300 ms translate) to its new column with the acting agent's avatar pulsing; conflicting local drag resolves server-wins with toast.

**TITAN-FRT-024 (MUST).** Board SHALL stay consistent under concurrent agent + human moves (server-wins, no ghost cards). *AC:* chaos E2E with 20 concurrent moves converges to API state.
**TITAN-FRT-025 (MUST).** Drag-and-drop SHALL be fully operable by keyboard (§10) and announced to screen readers. *AC:* axe + manual NVDA script pass.

### 6.4 Task Graph / Dependency Graph (`/projects/[pid]/graph`)

**Purpose.** Visualize the DAG of tasks and dependencies; spot the critical path and blockers instantly.

```
+------------------------------------------------------------------+
| Graph  [Layout: LR ▾] [Group: milestone ▾] [Fit ⤢] [Mini-map ▣]  |
+------------------------------------------------------------------+
|  ┌T-201✔┐→┌T-210✔┐→┌T-226●┐→┌T-231⚠┐→┌T-240○┐                    |
|  └──────┘ └──┬───┘ └──────┘ └──┬───┘ └──────┘                    |
|              ↓                 ↓  (critical path ═══ highlighted)|
|           ┌T-212✔┐→→→→→→→→→┌T-233○┐                              |
+------------------------------------------------------------------+
| Selected: T-231 Payment API · blocked by T-226 · BE-2 · [Open]   |
+------------------------------------------------------------------+
```

- **Key components:** React Flow (`@xyflow/react`) canvas with `dagre` auto-layout (ADR-FRT-04); custom task nodes (status icon, ID, title, agent avatar); mini-map; critical-path toggle (longest incomplete chain, computed client-side); selection inspector bar; blocked edges rendered dashed-red.
- **States:** loading = centered skeleton canvas + "computing layout"; empty = same as board empty; error = retry panel; > 500 nodes triggers clustered milestone-group rendering with drill-down (performance guard).
- **Interactions:** pan/zoom (wheel, pinch, `+/-`), click node → inspector + `?node=` in URL, double-click → task Sheet, hover → highlight upstream/downstream chains, `Shift+click` multi-select for bulk label ops. Layout is read-only — dependencies are edited by agents; humans request changes via the task Sheet.
- **Realtime:** node status patches recolor in place; new tasks enter with fade-in and incremental re-layout limited to the affected subgraph (full re-layout only on explicit "Re-layout" click, to avoid disorienting jumps).

**TITAN-FRT-026 (MUST).** Graph SHALL render 300 nodes / 600 edges at ≥ 30 fps pan/zoom on a mid-range laptop. *AC:* perf test in CI against seeded fixture.

### 6.5 Agent Status Grid (`/agents`)

**Purpose.** The "org chart in motion" — one card per agent, its state, and its current work.

```
+------------------------------------------------------------------+
| Agents  12 working · 2 idle · 1 error       [Filter: role ▾]     |
+----------------+----------------+--------------------------------+
| ◉ CEO          | ◉ PM-1         | ◉ Architect                    |
| deciding       | planning       | reviewing ADR-8                |
| epic priority  | sprint 4       | ▓ 2m 10s                       |
+----------------+----------------+--------------------------------+
| ● BE-1 coding  | ● BE-2 blocked | ● FE-1 coding                  |
|   T-238 auth   |   T-231 ⚠      |   T-227 player UI              |
|   claude ·1.2k | needs T-226    |   ollama/qwen ·640 tok/s       |
+----------------+----------------+--------------------------------+
| ○ QA-1 idle    | ✖ Sec-1 error  | ● Doc-1 writing                |
|                | LLM timeout ↻  |   API docs                     |
+----------------+----------------+--------------------------------+
```

- **Key components:** responsive card grid grouped by hierarchy tier (executive / management / engineering / quality); each card shows role, state (working / idle / blocked / error / paused), current task link, model+provider in use, token throughput, and elapsed time; header roll-up counts; agent detail drawer (`/agents/[id]`, intercepted route) with KPIs, recent messages, memory usage, and action buttons (Pause, Restart, Reassign — RBAC `admin+`).
- **States:** loading skeleton grid; empty = "No agents provisioned — start a project to spawn the organization"; error per-card (an unreachable agent renders its card in error style with last-seen time — the grid never blanks).
- **Interactions:** card click → drawer; Pause/Restart with confirmation; filter by role/state; clicking the model chip opens provider settings (RBAC-gated).
- **Realtime:** `agents:status` heartbeats (5 s cadence) drive state chips; missed 3 heartbeats → card degrades to "stale" with timestamp; error events raise a toast + bell notification.

**TITAN-FRT-027 (MUST).** An agent entering `error` SHALL be visible in the grid and the notification bell within 2 s, with a one-click path to its logs. *AC:* fault-injection E2E.

### 6.6 Live Agent Conversations (`/conversations`)

**Purpose.** The company's Slack: watch structured agent-to-agent communication live; intervene when needed.

```
+-------------+----------------------------------------------------+
| Channels    | # netflix-clone/standup                    ⚡live   |
| # standup   |----------------------------------------------------|
| # arch      | [PM-1] 12:01  Sprint 3 status: 6 doing, 2 at risk  |
| # backend ● | [BE-2] 12:02  T-231 blocked: payment sandbox creds |
| # frontend  |   └ type: BLOCKER · task: T-231 · [Open task]      |
| # qa        | [CEO ] 12:03  Approve use of mock gateway for MVP  |
| # reviews   |   └ type: DECISION · ADR-9 draft · [View ADR]      |
| # incidents | [Arch] 12:04  ▍streaming reply…                    |
|             |----------------------------------------------------|
| 👤 You      | [You ▸ #backend]  Message as client… [Send]        |
+-------------+----------------------------------------------------+
```

- **Key components:** channel list (per-project + org channels, unread badges); virtualized message feed; **structured message chips** — every message carries its Volume 2 type (TASK_ASSIGNMENT, PROGRESS_REPORT, REVIEW_REQUEST, BUG_REPORT, DECISION, BLOCKER, MEETING_SUMMARY, CONFLICT_RESOLUTION) rendered as a typed chip with deep links to the referenced entity; token-streaming bubbles for in-flight LLM output; human input box (posts as role `client`; agents treat client messages as top-priority input); search across history.
- **States:** loading skeleton bubbles; empty channel = "No messages yet"; error = feed keeps history, shows "stream interrupted — reconnecting" divider; history pagination on scroll-up (cursor-based).
- **Interactions:** jump-to-entity links; filter by message type; "🔇 mute channel"; copy message permalink; `Ctrl/⌘+Enter` send.
- **Realtime:** `message.created` appends; `message.chunk` streams tokens into a growing bubble with a typing caret; auto-scroll only when the user is at bottom (a "↓ 12 new" pill otherwise); unread counts update across channels.

**TITAN-FRT-028 (MUST).** Streaming agent output SHALL render incrementally (< 500 ms first-token-to-paint after receipt) without layout thrash. *AC:* perf trace during a 2,000-token streamed message keeps CLS ≈ 0.
**TITAN-FRT-029 (MUST).** Every structured message SHALL deep-link to its referenced entity (task, PR, ADR, deployment). *AC:* link-integrity E2E over all eight message types.

### 6.7 Logs Viewer (`/logs`)

**Purpose.** Unified, greppable view of agent logs, system logs, and generated-app build/test output.

```
+------------------------------------------------------------------+
| Logs  [Stream: agents ▾][Project ▾][Level: info+ ▾][🔍 regex]    |
|       [⏸ pause tail]  [Wrap ☐]  [⬇ Export]           ⚡tailing   |
+------------------------------------------------------------------+
| 12:04:11.201 INFO  be-2      task=T-231 starting payment module  |
| 12:04:11.940 WARN  qa-1      flaky test retry 2/3 e2e/checkout   |
| 12:04:12.113 ERROR sec-1     dep scan: lodash@4.17.20 CVE-…  ⧉   |
| 12:04:12.500 INFO  deploy    staging rollout 3/4 pods ready      |
| ───────────────── reconnected · 41 lines skipped [load] ──────── |
+------------------------------------------------------------------+
```

- **Key components:** virtualized monospace list (TanStack Virtual; 100k+ lines, ADR-FRT-05); filter bar (stream, project, agent, level, time range, regex — all URL-encoded); follow-tail toggle (auto-pauses on scroll-up); line expansion for structured JSON payloads; copy-line and export (filtered NDJSON); level color-coding + icons.
- **States:** loading = header instantly + "connecting to stream…"; empty = "No log lines match filters" with one-click filter reset; error = banner with retry while retaining the buffer; gap markers after reconnect with lazy "load skipped" fill from the REST history endpoint (Loki-backed, per Volume 9).
- **Interactions:** click ERROR line → context drawer (±50 lines + link to owning agent/task); `⏸`/`Space` pauses tail; regex highlights matches.
- **Realtime:** `log.appended` batches (≤ 100 lines/frame, rAF-applied per TITAN-FRT-021); client ring buffer caps at 50k lines (older lines fetched on demand).

**TITAN-FRT-030 (MUST).** Tail mode SHALL sustain 1,000 lines/s without dropping frames below 30 fps or freezing filters. *AC:* load-test fixture in CI perf suite.

### 6.8 Architecture Viewer (`/projects/[pid]/architecture`)

**Purpose.** Render the System Architect Agent's living architecture: C4-style diagrams + ADR history for the *generated* project.

```
+------------------------------------------------------------------+
| Architecture  [Level: Container ▾] [Version: v7 (current) ▾]     |
+---------------------------------------+--------------------------+
|   ┌─────────┐   ┌───────────┐         | ADRs (9)                 |
|   │ Next.js │──▶│ API (Fast │──┐      | ✔ ADR-9 Mock payment gw  |
|   │  Web    │   │  API)     │  │      | ✔ ADR-8 JWT sessions     |
|   └─────────┘   └─────┬─────┘  ▼      | ✔ ADR-7 Postgres schema  |
|        │              ▼     ┌──────┐  | … click → full record    |
|        ▼         ┌────────┐ │Redis │  |--------------------------|
|   ┌─────────┐    │Postgres│ └──────┘  | Selected: API container  |
|   │  CDN    │    └────────┘           | tech: FastAPI · owns:    |
|   └─────────┘                         | auth, catalog, billing   |
+---------------------------------------+--------------------------+
```

- **Key components:** React Flow canvas reusing the §6.4 graph engine with architecture node types (system/container/component/datastore/external); level switcher (Context → Container → Component, C4); **version timeline** — every architecture revision the Architect Agent commits is selectable, with a visual diff mode (added=green outline, removed=red ghost, changed=amber); ADR side panel (Context/Decision/Alternatives/Trade-offs rendered from backend records); node inspector.
- **States:** loading skeleton canvas; empty = "Architecture not yet designed — appears after the Architecture phase" with phase status link; error retry; oversized component views cluster like §6.4.
- **Interactions:** drill-down (double-click container → component level scoped to it); diff toggle between any two versions; export current view as SVG/PNG (client-side, offline-safe); ADR click opens full record dialog.
- **Realtime:** `architecture.updated` event shows a non-intrusive banner "New version v8 available — [View]" rather than mutating the canvas under the user.

**TITAN-FRT-031 (MUST).** Every architecture version SHALL be viewable and diffable against any other version. *AC:* E2E creates 3 revisions, asserts diff highlights match fixture.

### 6.9 Deployment Dashboard (`/deployments`, `/projects/[pid]/deployments`)

**Purpose.** Observe and control environment state and pipeline runs for generated applications.

```
+------------------------------------------------------------------+
| Deployments · netflix-clone                                      |
| ENV      VERSION   STATUS      LAST DEPLOY   ACTIONS             |
| local    v0.4.2    ● healthy   12:04 today   [Logs]              |
| staging  v0.4.1    ◐ rolling   12:01 today   [Logs] [Abort]      |
| prod     v0.3.9    ● healthy   yesterday     [Promote v0.4.1 🔒] |
+------------------------------------------------------------------+
| Pipeline run #88  (v0.4.1 → staging)                             |
| [build ✔ 2m] → [test ✔ 4m] → [scan ✔ 1m] → [deploy ◐ 3/4 pods]  |
|                                            └ step log stream ▾  |
| History: #87 ✔ · #86 ✖ rollback (view cause) · #85 ✔ …           |
+------------------------------------------------------------------+
```

- **Key components:** environment cards (version, health, URL, uptime); pipeline visualization (stage chips with per-stage duration + expandable live step logs reusing the §6.7 log list); deploy history table with rollback causes; **approval-gated actions** — Promote/Rollback/Abort require confirmation dialog restating scope, and `prod` promote requires typed environment name (RBAC `owner|admin`; 🔒 shown when the release awaits CEO-Agent or human approval per Volume 6).
- **States:** loading skeletons; empty = "No deployments yet — DevOps Agent deploys after the first passing release build"; error inline per environment; an aborted/failed run keeps its stage chips with failure detail.
- **Interactions:** stage chip expands logs; Promote/Rollback/Abort with confirmations; history row → full run detail; env card → live app URL (new tab).
- **Realtime:** `deployment.status_changed` and `deployment.step` animate stage chips; health probes update env cards; a failed prod deploy raises a `critical` notification (§7).

**TITAN-FRT-032 (MUST).** Destructive deployment actions SHALL require explicit confirmation (typed env name for prod) and be RBAC-gated. *AC:* E2E asserts viewer role sees no actions; admin prod-promote requires typed "prod".

### 6.10 Monitoring (`/monitoring`)

**Purpose.** Health of TITAN itself *and* of generated apps: golden signals, agent throughput, LLM cost.

```
+------------------------------------------------------------------+
| Monitoring  [Scope: platform ▾] [Range: 1h ▾]        ⚡ 1s feed   |
+---------------+---------------+---------------+------------------+
| API p95 182ms | Error rate    | Queue depth   | LLM spend/hr     |
| ▁▂▃▂▄▃▂ ok    | 0.4% ▁▁▂▁ ok  | 12 ▂▃▅▃ ok    | $1.87 ▃▄▆ warn   |
+---------------+---------------+---------------+------------------+
| Requests & latency (1h)        | Agent throughput (tasks/hr)     |
|  [line chart p50/p95/p99]      |  [stacked bar per role]         |
+--------------------------------+---------------------------------+
| Active alerts (1)                                                |
| ⚠ WARN  llm-budget 68% of daily cap · since 11:40 · [Ack] [Rules]|
+------------------------------------------------------------------+
```

- **Key components:** stat tiles with sparklines and threshold coloring; time-series charts (Recharts, dynamically imported) for latency, error rate, queue depth, token usage and cost per provider/agent; alert list with Ack; scope switcher (platform / per generated app); deep links into Grafana for power users ("Open in Grafana", local Grafana from the Compose stack per Volume 9) — the dashboard embeds nothing from Grafana, keeping the UI offline-consistent.
- **States:** loading skeleton tiles/charts; empty range = "No data in range"; error per-panel with retry; datasource loss shows last-known values flagged "stale @ 12:03".
- **Interactions:** range picker (15 m / 1 h / 24 h / 7 d); hover crosshair with synced tooltips across charts; tile click → pre-filtered chart; alert Ack (RBAC `admin+`) and link to alert rules (Settings).
- **Realtime:** `metric.sample` (1 Hz aggregate) rolls tiles and appends chart points in the live range; historical ranges are REST-fetched (Prometheus-backed API per Volume 9).

**TITAN-FRT-033 (SHOULD).** LLM cost SHALL be attributable per agent and per project over any selected range. *AC:* cost chart group-by toggle matches seeded ledger fixture.

### 6.11 Memory Explorer (`/memory`)

**Purpose.** Inspect and curate what the organization has learned: memory tiers, embeddings search, decisions, reusable patterns (backing model in Volume 5).

```
+------------------------------------------------------------------+
| Memory Explorer      [🔍 semantic search: "auth token refresh"]  |
+-------------+----------------------------------------------------+
| Tiers       | Results (semantic, top 20)          score          |
| ▸ Short-term| ▸ Pattern: JWT refresh rotation      0.92          |
| ▸ Project   |   src: banking-sys · used 3× · ✔ verified          |
| ▸ Org       | ▸ Decision: httpOnly cookie storage  0.88          |
| ▸ Patterns  |   ADR-8 netflix-clone · [view ADR]                 |
| ▸ Lessons   | ▸ Lesson: bcrypt cost=12 CI timeout  0.83          |
+-------------+----------------------------------------------------+
| Selected: "JWT refresh rotation"                                 |
| content preview · embedding meta · provenance chain · usage log  |
| [Pin ⭐] [Edit tags] [Archive 🗄 (admin)]                          |
+------------------------------------------------------------------+
```

- **Key components:** tier tree (short-term / project / organization / reusable patterns / lessons learned); semantic search box (Qdrant-backed endpoint) with score-ranked results; detail panel with provenance (which agent wrote it, from which task/conversation) and usage history (which retrievals used it); curation actions (pin, tag, archive — archive is soft-delete, RBAC `admin+`); browse mode with faceted filters (type, project, date, agent).
- **States:** loading skeleton list; empty tier = "Nothing stored here yet" with tier explanation; search miss = "No semantic matches ≥ 0.6 — try broader phrasing"; error inline.
- **Interactions:** search (`Enter`), facet filters, result click → detail, provenance links jump to source conversation/task; diff view when a memory entry was revised.
- **Realtime:** `memory.updated` badges the affected tier ("3 new"); the list refreshes on demand, not live-mutating under a reading user.

**TITAN-FRT-034 (MUST).** Every memory entry SHALL display full provenance (creating agent, source entity, timestamp) and its usage history. *AC:* fixture entry renders complete provenance chain; links resolve.

### 6.12 Plugin Manager (`/plugins`)

**Purpose.** Discover, install, configure, and monitor plugins (GitHub, GitLab, Slack, Discord, Jira, Linear, Figma, browser automation, MCP servers, LLM providers — catalog per Volume 7).

```
+------------------------------------------------------------------+
| Plugins   [Installed 6] [Catalog] [MCP Servers]      [+ Add MCP] |
+------------------------------------------------------------------+
| ● GitHub        v2.1  ✔ healthy   repo: acme/netflix-clone  [⚙]  |
| ● Slack         v1.4  ✔ healthy   #titan-alerts             [⚙]  |
| ● Ollama        v1.0  ✔ healthy   3 models · local          [⚙]  |
| ✖ Jira          v0.9  ✖ auth expired  [Reconnect]           [⚙]  |
+------------------------------------------------------------------+
| Catalog: [GitLab] [Discord] [Linear] [Figma] [Browser] …         |
|  each card: description · permissions requested · [Install]      |
+------------------------------------------------------------------+
```

- **Key components:** Installed tab (status, version, health check result, config gear); Catalog tab (permission-scoped install cards — each install dialog lists exactly which capabilities/scopes the plugin gets, mirroring the Volume 7 permission model); MCP Servers tab (add by URL/command, tool list preview, enable per-agent); per-plugin config forms generated from the plugin's JSON Schema manifest (rendered with the shared form engine — no hand-built form per plugin); health/activity log per plugin.
- **States:** loading skeletons; empty installed = "No plugins installed — browse the catalog"; catalog fetch failure falls back to the bundled offline catalog (local-first); plugin error state shows last error + Reconnect.
- **Interactions:** Install → permission consent dialog → config form → test-connection step → enabled; Disable/Uninstall with confirmation (uninstall states data retention behavior); secrets entered in config are write-only (masked after save, per Volume 8).
- **Realtime:** `plugin.status_changed` updates health chips; a plugin failure that blocks an agent raises a notification linking here.

**TITAN-FRT-035 (MUST).** Plugin configuration forms SHALL be generated from the plugin's JSON Schema manifest; secrets SHALL be write-only in the UI. *AC:* installing fixture plugin renders schema-driven form; saved secret displays as `••••` and never round-trips in any API response.

### 6.13 Settings (`/settings/*`)

**Purpose.** Organization and user configuration.

```
+-------------+----------------------------------------------------+
| Settings    | LLM Providers                                      |
| Profile     |  Default: [Anthropic ▾]  Fallback: [Ollama ▾]      |
| Appearance  |  ┌ Anthropic  key ••••7f2  ✔ verified  [Test]      |
| Providers   |  ├ OpenAI     key —        not set     [Add]       |
| Members     |  ├ Google     key —        not set     [Add]       |
| API Keys    |  └ Ollama     http://ollama:11434  ✔ 3 models      |
| Notifications| Per-role overrides: [Architect→anthropic/opus …]  |
| Budgets     |  Budget: $10/day  hard-stop ☑   alert at 80% ☑     |
+-------------+----------------------------------------------------+
```

- **Sub-pages:** **Profile** (name, email, avatar, session list with revoke); **Appearance** (theme dark/light/system, density, reduced motion override, language); **LLM Providers** (provider keys, default + fallback chain, per-agent-role model overrides, budgets with hard-stop — this is the configuration-only provider switching required by the canonical gateway decision); **Members & Roles** (invite, RBAC role assignment owner/admin/developer/viewer, per-project grants); **API Keys** (personal access tokens for the TITAN API, scoped, shown once); **Notifications** (per-category channel matrix, §7); **Budgets & Limits** (token/cost caps per project).
- **States:** each sub-page loads independently (RSC-rendered, minimal JS); form errors inline per field (zod messages); destructive actions (remove member, revoke key) use typed-confirmation dialogs.
- **Interactions:** all forms `react-hook-form` + zod; Save is disabled until dirty; provider "Test" performs a live round-trip and reports latency/model list; role changes take effect on next token refresh and are announced to the affected user via notification.
- **Realtime:** minimal — `member.updated` and `budget.threshold` events refresh relevant panels.

**TITAN-FRT-036 (MUST).** Switching the default LLM provider (e.g., Anthropic → Ollama) SHALL require only Settings changes and take effect without redeploying. *AC:* E2E switches provider, next agent action logs the new provider.
**TITAN-FRT-037 (MUST).** RBAC SHALL be enforced in the UI (hidden/disabled controls) **and** never trusted client-side — server re-checks every mutation. *AC:* forged request from viewer role returns 403 and UI shows permission toast.

---

## 7. Notifications System

### 7.1 Channels and taxonomy

Three in-app surfaces plus external channels (via plugins):

1. **Toasts** (shadcn/ui `sonner`, bottom-right, max 3 stacked, auto-dismiss 6 s except `critical` which persists until dismissed) — transient confirmations and failures.
2. **Notification Center** (bell → popover; full page at `/settings/notifications` history) — persistent, filterable, mark-read/mark-all, deep links.
3. **Attention Queue** (Overview §6.1) — items *requiring* human action; these never auto-dismiss and mirror to the bell.

| Category | Default severity | Examples | Default channels |
|---|---|---|---|
| `approval` | critical | Release approval, budget hard-stop | Toast + bell + attention queue |
| `failure` | high | Deploy failed, agent error, CI red | Toast + bell |
| `progress` | info | Milestone done, sprint closed | Bell |
| `activity` | low | Task moved, PR merged | Feed only (muted by default) |
| `security` | critical | Dependency CVE, secret detected | Toast + bell + attention queue |

Per-category routing is user-configurable (channel matrix in Settings; Slack/Discord delivery via plugins). Browser (Web Push) notifications are opt-in and used only for `approval`/`critical` while the tab is unfocused.

| ID | Priority | Requirement | Acceptance criteria |
|---|---|---|---|
| TITAN-FRT-038 | MUST | Notifications SHALL be delivered over `notifications:{userId}` and persisted server-side; the bell badge survives reload. | Reload retains unread count; mark-read syncs across two open tabs ≤ 2 s. |
| TITAN-FRT-039 | MUST | `critical` toasts SHALL persist until explicitly dismissed and never be replaced by lower-severity toasts. | Storybook + E2E stacking test. |
| TITAN-FRT-040 | SHOULD | Toast storms SHALL coalesce (≥ 4 same-category events in 10 s → one grouped toast "12 tasks updated"). | Burst fixture produces grouped toast. |

---

## 8. Responsive Behavior

Breakpoints (Tailwind): `sm 640`, `md 768`, `lg 1024`, `xl 1280`, `2xl 1536`. The dashboard is desktop-first (its users watch dense realtime data) but every screen MUST be usable at 375 px width.

| Range | Shell | Screen adaptations |
|---|---|---|
| ≥ 1280 | Full sidebar + content | All layouts as wireframed; side panels inline |
| 1024–1279 | Icon-rail sidebar | Two-panel screens (Conversations, Memory, Architecture) keep both panels; inspectors become overlays |
| 768–1023 | Icon rail | Kanban horizontal-scrolls with snap columns; Overview KPI grid 2×2; detail panels → bottom Sheets |
| < 768 | Sidebar → Sheet (hamburger); bottom tab bar with Org/Board/Agents/Convos/More | One panel at a time (channel list ↔ feed); Graph and Architecture render read-only with pinch-zoom; tables → card lists; Logs keeps virtualized list with simplified filters |

Rules: no horizontal body scroll ever (wide content scrolls in its own container); touch targets ≥ 44 px under `md`; drag-and-drop gains long-press initiation on touch plus an explicit "Move to…" menu fallback; charts re-render responsively via container queries.

**TITAN-FRT-041 (MUST).** All 13 screens SHALL pass a 375 px viewport E2E pass with no horizontal body scroll and all primary actions reachable. *AC:* Playwright mobile suite green.

---

## 9. Accessibility (WCAG 2.1 AA)

| ID | Priority | Requirement | Acceptance criteria |
|---|---|---|---|
| TITAN-FRT-042 | MUST | Full keyboard operability: every interactive element focusable in logical order; visible focus ring (`--ring`, 2 px offset); no traps; skip-to-content link. | Manual keyboard script per screen + axe automated run in CI (zero serious/critical). |
| TITAN-FRT-043 | MUST | Correct semantics/ARIA: landmarks (`nav`, `main`), tables as tables, live regions — activity feed and toasts `aria-live="polite"`, critical alerts `aria-live="assertive"`; Kanban DnD announces pick-up/over/drop via `aria-live` (dnd-kit announcements). | NVDA + VoiceOver scripted pass for Board, Conversations, Overview. |
| TITAN-FRT-044 | MUST | Status is never color-only: every colored state pairs an icon/text (✔ ⚠ ✖ ◐ shapes as in wireframes). | Design review checklist + grayscale screenshot audit. |
| TITAN-FRT-045 | MUST | `prefers-reduced-motion` disables non-essential animation (card fly, pulses, streaming caret blink); realtime data still updates instantly. | E2E with emulated preference asserts no CSS animations run. |
| TITAN-FRT-046 | MUST | Contrast per TITAN-FRT-012 in both themes; text resizable to 200 % without loss of function. | Browser zoom 200 % E2E on all screens. |
| TITAN-FRT-047 | SHOULD | Graph/Architecture canvases provide an accessible alternative: a structured list view of nodes/edges toggleable via "List view". | Toggle exists; list exposes same data and actions. |

---

## 10. Keyboard Shortcuts

Global (never override browser/system defaults; disabled inside text inputs except `Esc`/`⌘Enter`; discoverable via `?` overlay):

| Shortcut | Action |
|---|---|
| `Ctrl/⌘ + K` | Command palette |
| `Ctrl/⌘ + P` | Project switcher |
| `?` | Shortcut help overlay |
| `g` then `o / p / b / g / a / c / l / d / m` | Go to Org / Projects / Board / Graph / Agents / Conversations / Logs / Deployments / Monitoring (Gmail-style sequences) |
| `Esc` | Close dialog/sheet/palette (top-most first) |
| `Ctrl/⌘ + Enter` | Send message / submit form |
| `n` | New (contextual: project, message, MCP server) |
| `/` | Focus the screen's primary search/filter |

Screen-scoped: Board — arrows navigate cards, `Enter` opens, `Space` lifts card then arrows move / `Space` drops / `Esc` cancels (dnd-kit keyboard sensor); Graph — `+`/`-` zoom, `0` fit, arrows pan, `Tab` cycles nodes; Logs — `Space` pause tail, `e` jump to next ERROR; Conversations — `Alt+↑/↓` switch channels.

**TITAN-FRT-048 (MUST).** All listed shortcuts SHALL work and appear in the `?` overlay; sequences time out after 1.5 s. *AC:* E2E per shortcut; overlay snapshot matches table.

---

## 11. Frontend Folder Structure

Located at `apps/web` in the monorepo (structure canonical per Volume 1; shared packages under `packages/`).

```
apps/web/
├── app/                              # Next.js 15 App Router
│   ├── layout.tsx                    # Root: theme script, providers, shell
│   ├── (dashboard)/                  # Auth-gated group with app shell
│   │   ├── org/page.tsx
│   │   ├── projects/
│   │   │   ├── page.tsx
│   │   │   └── [pid]/
│   │   │       ├── page.tsx          # Overview tab
│   │   │       ├── board/page.tsx
│   │   │       ├── graph/page.tsx
│   │   │       ├── architecture/page.tsx
│   │   │       └── deployments/page.tsx
│   │   ├── agents/
│   │   │   ├── page.tsx
│   │   │   └── @drawer/(.)agents/[agentId]/page.tsx   # intercepted drawer
│   │   ├── conversations/[[...channelId]]/page.tsx
│   │   ├── logs/page.tsx
│   │   ├── deployments/page.tsx
│   │   ├── monitoring/page.tsx
│   │   ├── memory/page.tsx
│   │   ├── plugins/page.tsx
│   │   └── settings/{profile,appearance,providers,members,api-keys,notifications,budgets}/page.tsx
│   ├── (auth)/login/page.tsx         # OIDC redirect flow
│   └── api/                          # BFF-only route handlers (auth callback, WS ticket)
├── components/
│   ├── ui/                           # vendored shadcn/ui primitives
│   ├── shell/                        # Sidebar, TopBar, CommandPalette, ConnectionPill
│   ├── kanban/                       # Board, Column, TaskCard, TaskSheet
│   ├── graph/                        # FlowCanvas, TaskNode, ArchNode, MiniMap, ListViewAlt
│   ├── agents/                       # AgentCard, AgentGrid, AgentDrawer
│   ├── conversations/                # ChannelList, MessageFeed, TypedChip, StreamBubble
│   ├── logs/                         # LogList, LogFilterBar, LogLine
│   ├── deployments/                  # EnvCard, PipelineRun, StageChip
│   ├── monitoring/                   # StatTile, TimeSeries, AlertList
│   ├── memory/                       # TierTree, ResultList, ProvenancePanel
│   ├── plugins/                      # PluginCard, SchemaForm, McpServerForm
│   └── notifications/                # Bell, Center, ToastHost, AttentionQueue
├── lib/
│   ├── ws/                           # socket client, topic registry, seq reconciliation
│   ├── query/                        # QueryClient, key factory, hydration helpers
│   ├── auth/                         # JWT/session helpers, RBAC guards (useCan)
│   ├── shortcuts/                    # sequence engine, registry, ?-overlay data
│   └── utils/
├── hooks/                            # useTopic, useAgentStatus, useTailBuffer, …
├── stores/                           # Zustand: shell.ts, palette.ts, toasts.ts
├── styles/globals.css                # token variables (dark default + light)
├── e2e/                              # Playwright suites incl. a11y + perf budgets
├── tailwind.config.ts
└── next.config.ts

packages/
├── api-client/                       # OpenAPI-generated types + typed fetch
└── ws-protocol/                      # shared envelope/topic/event TS types
```

**TITAN-FRT-049 (MUST).** Feature code SHALL live in its feature directory; cross-feature imports go through `lib/` or `packages/` (enforced by ESLint import boundaries). *AC:* boundary lint passes; violation fails CI.

---

## 12. Decision Records

### ADR-FRT-01 — RSC-first pages with React Query for interactive state
**Context.** Screens mix static-ish content (settings, project metadata) with highly dynamic content (boards, feeds).
**Decision.** Render first paint via Server Components; hydrate interactive islands with React Query using RSC-provided `initialData`; WS layer keeps caches fresh.
**Alternatives.** (a) Full client SPA + React Query only — slower first paint, larger bundles, weaker SEO-less-relevant but worse TTI; (b) RSC + server actions only — no mature story for realtime cache patching; (c) Redux Toolkit Query — more boilerplate, weaker cache-invalidation ergonomics for our event-driven model.
**Trade-offs.** Two data paths (RSC fetch + client query) must share the typed client to avoid drift; mitigated by `packages/api-client` and hydration helpers.

### ADR-FRT-02 — Vendored shadcn/ui over a packaged component library
**Context.** Need accessible primitives, deep theming, and long-term control in an open-source repo.
**Decision.** Vendor shadcn/ui (Radix-based) into `components/ui` and treat as first-party code.
**Alternatives.** MUI (heavy runtime, harder deep theming), Chakra (runtime CSS-in-JS cost), Mantine (own token system conflicts with Tailwind), headless-only Radix (rebuild everything shadcn already gives).
**Trade-offs.** We own upgrades/bugfixes for vendored code; accepted — components are reviewed like normal source, and upstream diffs are applied deliberately.

### ADR-FRT-03 — Single multiplexed WebSocket with topic subscriptions
**Context.** A dozen surfaces need realtime data; browsers and the FastAPI gateway both suffer under per-widget sockets.
**Decision.** One socket per tab; JSON envelope with `topic/event/seq`; subscribe on mount, unsubscribe on unmount; gap-triggered refetch for reconciliation.
**Alternatives.** Socket per feature (connection storms, N×auth); SSE (one-way only — we need client sends for subs and chat); polling (violates TITAN-FRT-003 latency and wastes local-LLM-laptop resources).
**Trade-offs.** Client-side router/registry complexity and head-of-line risk on one socket; mitigated by batching (TITAN-FRT-021) and keeping bulk history on REST.

### ADR-FRT-04 — React Flow (@xyflow/react) + dagre for both graphs
**Context.** Task DAG and Architecture Viewer both need interactive node-edge canvases with custom nodes.
**Decision.** One shared graph engine (`components/graph`) on React Flow with dagre layout; custom node types per domain.
**Alternatives.** D3 from scratch (months of interaction work), Cytoscape.js (styling model fights React), Mermaid render (static, no interaction), commercial libs (violates open-source/free requirement).
**Trade-offs.** React Flow renders nodes as DOM (not canvas), capping practical size — addressed by clustering beyond 500 nodes (TITAN-FRT-026 guard).

### ADR-FRT-05 — Virtualized ring-buffer log viewer, not pagination
**Context.** Agents emit bursty, high-volume logs; users need grep-like tailing.
**Decision.** TanStack Virtual list over an in-memory ring buffer (50k lines) with rAF-batched appends; history via REST (Loki) on demand.
**Alternatives.** Paginated table (kills tailing UX), xterm.js terminal (great for raw streams, poor for structured filtering/expansion), server-rendered chunks (latency).
**Trade-offs.** Client memory bounded but filters only search the buffer; full-history search delegates to the backend search endpoint, which the UI exposes when a filter is active with tail paused.

### ADR-FRT-06 — Dark default via CSS variables + `class` strategy
**Context.** Product requirement: dark mode default, professional; light mode supported; no theme flash.
**Decision.** HSL CSS variables per semantic token; `dark` class on `<html>` by default; blocking inline script applies persisted choice pre-paint; both themes AA-validated.
**Alternatives.** `prefers-color-scheme` only (violates "dark default" for light-OS users), duplicate Tailwind palettes (drift), runtime theming lib (unneeded weight).
**Trade-offs.** Inline script is a tiny render-blocking cost (< 1 ms) — accepted to guarantee no flash.

---

## 13. Risks & Open Questions

| # | Risk / Question | Impact | Mitigation / owner |
|---|---|---|---|
| R-1 | WS fan-out volume (logs + token streams + metrics) may saturate a single laptop in local mode. | UI jank, dropped events | Server-side per-topic rate caps and batch frames (Volume 3); client rAF batching (TITAN-FRT-021); load fixtures in CI. |
| R-2 | React Flow DOM rendering may not scale for very large generated systems (> 500 nodes). | Graph unusable on big projects | Clustering + drill-down is specified; if insufficient, evaluate canvas renderer behind the same `components/graph` API. |
| R-3 | Conversational intake (§6.2) quality depends on CEO Agent latency; slow local models could make first-run UX feel broken. | Onboarding abandonment | Streaming UI with progress narration; skeleton roadmap placeholder; document expected local-model latency in onboarding copy. |
| R-4 | Optimistic Kanban updates vs. agent-initiated moves can conflict in bursts. | Flicker, mistrust | Server-wins + seq reconciliation specified (TITAN-FRT-024); monitor conflict rate metric; consider column-level locks if > 1 % of moves conflict. |
| R-5 | Open question: should human free-text messages in Conversations be able to *directly* reassign tasks, or only signal the PM Agent? | Authority-model ambiguity | Volume 2 (agent authority) + Volume 6 (workflow) must rule; UI ships signal-only until then, which is forward-compatible. |
| R-6 | Open question: multi-organization tenancy in the UI (org switcher) — MVP assumes a single org per deployment. | IA rework if multi-tenant later | Route map already roots data under org IDs server-side; sidebar reserves a slot for an org switcher; decision deferred to Volume 10 roadmap. |
| R-7 | Web Push for approvals requires a push service; strictly offline deployments cannot use it. | Missed approvals when tab closed | Push is opt-in enhancement only; attention queue + external channel plugins (Slack) remain primary; document limitation. |

---

## 14. Cross-References

- **Volume 1 — Foundation:** canonical stack decisions, monorepo layout, personas, success metrics this UI serves.
- **Volume 2 — Multi-Agent Intelligence:** agent roles/states shown in the Agent Grid; structured message types rendered in Conversations; agent authority for R-5.
- **Volume 3 — Backend Engineering:** REST + WebSocket contracts, topic/event schemas, auth (OAuth2/OIDC + JWT) and RBAC roles consumed by `useCan`.
- **Volume 5 — AI Brain (Memory, Knowledge & RAG):** memory tiers, provenance, and semantic search behind the Memory Explorer.
- **Volume 6 — Engineering Workflow:** Kanban column semantics, approval gates surfaced in the Attention Queue and Deployment Dashboard.
- **Volume 7 — Integrations & Tool Ecosystem:** plugin catalog, permission model, and MCP server integration behind the Plugin Manager.
- **Volume 8 — Security:** secret handling (write-only fields), audit-logged UI actions, session management in Settings → Profile.
- **Volume 9 — Deployment & Production:** Prometheus/Grafana/Loki datasources behind Monitoring and Logs; pipeline stages shown in the Deployment Dashboard.
- **Volume 10 — Autonomous Evolution:** plugin marketplace evolution, custom-agent creation UI, and multi-org tenancy (R-6) roadmap.

*End of Volume 4 — TITAN-VOL-04, Draft v1.0.*
