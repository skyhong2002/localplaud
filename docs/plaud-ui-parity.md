# Plaud Web UI parity spec (build reference)

Status: **v2 — complete first pass** (tokens, shell, Home, file tables, recording
workspace, search, Ask, Templates, Explore, Settings, Add audio, phone behaviour,
localplaud gap list). See the changelog at the bottom.

Source: read-only audit of the authenticated Plaud Web app (`web.plaud.ai`) on
2026-10-02 at a 1440x900 desktop viewport, with computed styles read through
DevTools. All content below is placeholder. Private screenshots live only in
`data/audits/plaud-ui-20261002/web/` (gitignored), named `d-*.png` (desktop) and
`m-*.png` (phone).

Ground rules for builders:

- Reproduce **layout, measurements, interaction model, and states**. Do not copy
  Plaud artwork, the PLAUD wordmark, logo mark, illustrations, or marketing cards.
  Use localplaud branding (`docs/brand.md`, `static/logo*.svg`).
- Plaud-only commerce (plan badge, "Go Unlimited", minutes left, Membership Center,
  team workspaces, community popularity counts) maps to localplaud equivalents
  (system health / processing queue / storage) or is omitted. Noted per component.
- Where Plaud is weaker than our product principles (no speaker colours, no
  word-level highlight, generation hidden behind one button), the spec says so and
  gives the localplaud target.

Plaud Web stack, for orientation only: Vue 3 + Element Plus + Tailwind, SVG sprite
icons, `vue-virtual-scroller` for long lists, Tiptap-style block editor for notes,
markmap for the mind map.

---

## 1. Design tokens

All measurements are CSS px at 1x. Plaud draws hairlines as `1px` borders.

### 1.1 Typography

| Token | Value | Used for |
|---|---|---|
| `font-sans` | `Inter, "SF Pro Text", "Helvetica Neue", Helvetica, Arial, sans-serif` | everything (body) |
| CJK fallbacks | zh-TW: `PingFang TC, Hiragino Sans CNS, PingFang HK, Microsoft JhengHei, Noto Sans TC` | Plaud switches family by UI language via `.detail_lang_*` class |
| `text-page-title` | 32px / 44–48px, **weight 300**, #000 | "Recent files", "All files", folder name, "Select a template" |
| `text-note-title` | 32px / 44px, weight 500, #000 | editable recording title at top of a note |
| `text-note-h2` | 24px / 36px, weight 500, #000, margin-top 32px | note section headings |
| `text-note-body` | 16px / 26px, weight 400, #3D3D3D | note paragraphs (lists 16/24) |
| `text-ui` | 14px / 21–22px, weight 400 | nav items, table cells, buttons, menu items, transcript text |
| `text-ui-strong` | 14px, weight 600 | active tab label |
| `text-meta` | 12px / 18px, weight 400, #7A7A7A | secondary row metadata, card descriptions, tooltips |
| `text-button-sm` | 12px / 12–18px | batch-action buttons |
| `text-dialog-title-sm` | 16px | compact dialog title ("Export transcript") |

Base `body` font-size is 12px/18px; every visible component sets its own size.
No uppercase labels, no letter-spacing tweaks anywhere.

### 1.2 Colour

| Token | Value | Notes |
|---|---|---|
| `bg-app` | `#FFFFFF` | main content |
| `bg-sidebar` | `#F9F9F9` | left sidebar; also template-chooser dialog background |
| `bg-hover` | `#F2F2F2` | table row hover/selected |
| `bg-hover-ui` | `rgba(31,35,41,0.08)` | sidebar item hover **and** active |
| `bg-subtle` | `#EBEBEB` | file-slider active item, icon-button hover/pressed, "+" tab button, transcript banner chip, skeleton bars |
| `bg-subtle-2` | `#E5E5E5` | active item in dialog side-nav |
| `bg-progress-track` | `#F0F0F0` | player track |
| `border-hairline` | `#EBEBEB` | table header rule, slider divider, batch buttons, dividers in sidebar |
| `border-ui` | `#E5E7EB` | workspace header bottom border, popover border |
| `border-input` | `#CCCCCC` | "Add audio" outlined button |
| `text-primary` | `#000000` | titles, active items, filenames |
| `text-body` | `#3D3D3D` | nav labels, transcript text, note body |
| `text-secondary` | `#7A7A7A` | counts, column headers, durations, dates, metadata, "View all" |
| `text-timestamp` | `#646A73` | transcript timestamps |
| `text-disabled` | `#A3A3A3` | disabled batch button, idle skip buttons |
| `text-icon-muted` | `#606266` | "more" glyph, export icon |
| `danger` | `#FF503F` | "Move to Trash" in batch bar (text + icon) |
| `accent-upgrade` | `#8F53ED` (border) / `rgba(143,83,237,.10)` (fill) | Plaud's plan upsell only — **do not reuse**; localplaud has no upsell |
| `accent-toggle-on` | `#177BE5` | replace-mode toggle in find bar |
| `search-match` | `#FFE14D` | find matches |
| `search-match-current` | `#FFA64D` | current find match |
| `tooltip-bg` | `#3A3A3A`, text #FFF | all tooltips |
| `overlay` | `rgba(0,0,0,.5)` | modal scrim |
| Folder icon palette | `#3DC8C8` teal, `#4C8EFF` blue, `#FB5C5C` red, `#C149EB` purple, `#F9A251` orange, `#191919` black | user-chosen per folder |
| Mind-map branch pastels | `#E5CCB3 #E5DDB3 #CCE5B3 #B7E5C6 #B8E5DE #B3D5E5 #B3C4E5` | one per first-level branch |

Plaud is effectively monochrome: black primary buttons, grey surfaces, colour only
for folder icons, AI-sparkle gradients, and the mind map. There is **no dark mode**
and **no speaker colour** in the transcript (all speaker names are #000).
localplaud target: keep the monochrome chrome, but add a speaker colour dot/label
tint (8-colour accessible palette) because colour-coded speakers are a product
requirement.

### 1.3 Radius, shadow, motion

| Token | Value |
|---|---|
| `radius-sm` | 4px (icon buttons) |
| `radius` | **5px** (buttons, nav items, menus, popovers, dialogs, chips) |
| `radius-row` | 6px (table rows, transcript segments) |
| `radius-lg` | 12px (large template dialog) |
| `shadow-popover` | `0 0 32px 0 rgba(0,0,0,.10)` (dropdowns, popovers, find panel) |
| `shadow-dialog` | `0 12px 32px 4px rgba(0,0,0,.04), 0 8px 20px 0 rgba(0,0,0,.08)` |
| `ease-standard` | `cubic-bezier(.4,0,.2,1)` |
| sidebar slide | `width .3s, transform .3s` + main `margin-left .3s` |
| file-slider slide | `transform/opacity/max-width .3s` |
| row hover | `background-color .2s` |
| icon-button hover | `.15s` |
| transcript segment | `color/background .3s` |
| progress fill | `width .1s linear` |
| tooltip | fade-in linear ~.2s, appears after ~300ms hover |

Respect `prefers-reduced-motion` (Plaud does not; we must).

### 1.4 Icons

Monoline outline icons drawn as filled paths on a 20x20 viewBox (visual stroke
≈1.25–1.5px), `fill: currentColor`. Sizes: 20px (nav, table actions, header),
16px (chevrons, small buttons), 14px (inline speaker rename), 18px (segment
actions), 24px (find-bar actions, template card icons). Colour inherits text
colour. localplaud should use an open icon set with the same weight (e.g. Lucide at
`stroke-width: 1.5`, 20px) — never Plaud's sprite.

### 1.5 Breakpoints present in Plaud CSS

`max-width: 1252px`, `845px`, `768px` (most rules), `576px`, `480px`, `425px`,
`352px`; `max-height: 750/576/480px`; `min-width: 1920px`. Phone behaviour is
documented in section 7.

---

## 2. App shell (desktop)

```
┌──────────── 220 ───────────┬──────────────────── fluid ─────────────────────┐
│ [wordmark]          [⇤]   │                                                 │
│ [ava] Workspace name   ⌄  │                                                 │
│ [ + Add audio          ]  │                 page content                    │
│ ─────────────────────────  │        (each page owns its own header)          │
│ ⌕  Search                 │                                                 │
│ ⌂  Home                   │                                                 │
│ ✧  Ask                    │                                                 │
│ ◇  Templates              │                                                 │
│ ⊞  Explore                │                                                 │
│ ─────────────────────────  │                                                 │
│ ▭  All files (N)          │                                                 │
│ ▤  Unfiled (N)            │                                                 │
│ 🗑  Trash (N)              │                                                 │
│ Folders ⌄            +    │                                                 │
│ ● Folder A (N)       ⋯    │                                                 │
│ ● Folder B (N)            │                                                 │
│ (scrolls)                 │                                                 │
│ ─────────────────────────  │                                                 │
│ status block (plan/usage) │                                                 │
└───────────────────────────┴─────────────────────────────────────────────────┘
```

No global top bar. Each page renders its own heading or 48px header.

### 2.1 Sidebar (`220px`, `bg-sidebar`, full height, no right border)

| Part | Geometry & style | Behaviour |
|---|---|---|
| Brand row | 46px tall; wordmark 14px tall at x16 y16 | Click → Home. On sidebar hover a **collapse button** (18px icon in a 28px hit box) appears at the row's right edge (x≈183). Tooltip "Collapse sidebar". |
| Workspace switcher | 204x42 at x8, padding 8; 24px rounded-6 avatar; name 14/22 #000; chevron-down 16px at right | Click → popover (below). localplaud: replace with **library/instance switcher or user menu** (account, settings, support, about, sign out). |
| Add audio | 188x38 at x16, 1px #CCC border, radius 5, centered "+" 16px icon + label 14px; hover bg #EBEBEB | Opens the Add-audio dropdown (section 6.6). |
| Divider | 1px #EBEBEB, margin 16 8 0 | |
| Primary nav | `<ul>` of 5 items, 4px gap; each item 194x37, radius 6, inner padding 8, icon 20 + 8px gap + label 14/21 #3D3D3D | Hover/active bg `rgba(31,35,41,.08)`, active label #000. Items: Search, Home, Ask Plaud → **Ask**, Template Community → **Templates**, Explore → **Discover**. Search does not navigate to a page first; it opens the search palette (section 6.1). |
| Divider | 1px #EBEBEB, margin 16 0 | |
| Library categories | All files / Unfiled / Trash; 194x38, radius 5, padding 8, icon 20, label 14 #3D3D3D followed by count "(N)" 14 #7A7A7A with 4px gap | Same hover/active treatment. Route: `/file-list?categoryId=allFiles|unorganized|trash`. |
| Folders header | 194x38; "Folders" 14/28 #7A7A7A + chevron 20 (collapse/expand list); "+" 28x28 hit box at right (create folder) | Chevron toggles the folder list. |
| Folder rows | 194x38, coloured folder glyph 20 (user colour), name truncate 14/24 #3D3D3D, count #7A7A7A | Hover shows a `⋯` button (20px) at right → menu **Edit / Delete**. Rows are drop targets (file rows are `draggable`). Route `/file-list?categoryId=<folderId>`. |
| Folder scroll | The whole nav + folders region (`y 155 → 745`) is one thin-scrollbar scroller; brand, switcher and Add audio stay fixed. | |
| Bottom status block | divider then 204x75 card (padding 6 8, radius 5): plan name 14/20 + chevron; 4px usage bar (#D6D6D6 track, dark fill); "N min left" 12/16 #3D3D3D + info icon 12; then 188x39 upsell button | localplaud: replace with a **status card**: worker state (idle / processing N / failed N), queue progress bar, storage or "last sync N min ago", linking to System health. No upsell button. |

**Collapsed sidebar.** Sidebar translates −220px (`.sidebar-hidden`), main content
re-centres. Only a 28px brand mark remains at top-left (x16 y10); hovering it shows
bg #EBEBEB + tooltip "Expand sidebar"; clicking restores. Plaud persists the state.
There is no icon-rail mode. localplaud: keep this, persist in `localStorage`, and
add keyboard shortcut `[` / `Ctrl+\` (Plaud has none).

**Workspace popover** (260x448, radius 5, `shadow-popover`, padding 8 0):
header row with avatar 32 + workspace name 14 and a small "Settings" outline
button; divider; account email 12 #7A7A7A; workspace list (check on current) and
"Create a Team workspace"; divider; Membership Center, Contact support, Send
feedback, Download App, Sign out — each 38px, 14px #3D3D3D, 16px leading icon.
localplaud mapping: Settings, Support/About, System health, Sign out.

**Tooltip** (all icon-only buttons): #3A3A3A bg, #FFF 12/22 text, padding 8,
radius 5, arrow, placed below (or above) the trigger.

### 2.2 Page content frame

List-style pages use a centred column: content width **952px** max at 1440 (side
margins auto ≈ 91px inside the 1220px main area), padding-top 48px, padding-bottom
72px. Main area scrolls; sidebar does not.

---

## 3. Home (`/`)

Single centred column, sections separated by **60px**.

1. **Recent files** — header row: page title "Recent files" (32/48 w300) left,
   "View all ›" (14 #7A7A7A + 16px chevron) right → All files. Then the 5 most
   recent recordings as rows identical to the file table (section 4.2) but
   without header row and without the checkbox gutter.
2. **What's new** (Plaud marketing) — 3 cards 301x228, image 130px top, body
   padding 16, title 14/22 #000, description 12/18 #7A7A7A clamped 2 lines,
   card bg #F7F7F7, radius 5. localplaud: replace with **"Needs attention"**
   (failed/degraded stages with retry), **"Processing now"**, and **"Ask your
   library"** quick entry; never marketing.
3. **Plaud Community** — header + "View all ›"; 4 template cards 220x220 (1px
   border #EBEBEB, radius 5, padding 16): coloured icon 24, title 20px w300
   2-line, description 14 #7A7A7A, footer "▤ usage-count • author" 12 #7A7A7A.
   localplaud: "Templates" strip showing pinned/recent local templates.

Empty library state (not observed in this account): reuse the table empty state
(section 4.5).

---

## 4. File tables: All files / Unfiled / Folder / Trash

Route `/file-list?categoryId=…`. Same component for all four.

### 4.1 Layout

- Title (32/44 w300) at margin-left **40px** (aligned with the name column; the
  40px gutter holds row checkboxes).
- Header row (margin-top 24, padding-bottom 16, 1px #EBEBEB bottom rule):
  `Name` (flex) · `Duration` (120) · `Date created ⌄` (160, sort trigger) · actions (≈64).
  Column labels 14/21 #7A7A7A. Column gap 24px.
- Rows below, virtualised.

### 4.2 Row

| Property | Value |
|---|---|
| Height | 58px row + 14px spacing (72px pitch). Two-line rows (title + indicator) grow. |
| Padding | 8px 8px 8px **40px** (checkbox gutter) |
| Radius | 6px |
| Name | 14/22 #000, single line, ellipsis. Untitled recordings show the start timestamp `YYYY-MM-DD HH:mm:ss` as name. |
| Under-name indicators | optional 16px icons in #6B7280 (Plaud: note/mark marker). localplaud: **processing-state chips** here — `Transcribing 42%`, `Needs speakers`, `Failed: diarization`, `Imported (Plaud)` provenance — 12px, pill radius 5. |
| Duration | 14/21 #7A7A7A, format `1h 22m 8s` / `8m 54s` / `31s` |
| Date | 14/21 #7A7A7A, `YYYY-MM-DD HH:mm:ss` (date created or modified per sort) |
| Actions | hover-only "generate" icon (24 box, opens template chooser — localplaud: **"Generate notes"** quick action) + always-visible `⋯` (20 box, #606266) |
| Hover | bg #F2F2F2 (`.2s`), checkbox (16px square, 1px grey border) fades in at x+18 in the gutter, generate icon appears |
| Click | anywhere on the row except controls → opens recording workspace `/file/<id>` |
| Drag | rows are `draggable` → drop on a sidebar folder to move |

Row `⋯` menu (248w, radius 5, `shadow-popover`, padding 8 0, items 40h, 14px #000,
16px leading icon, padding 0 16): **Rename · Move to folder · Move to Trash**.
In Trash: **Restore · Delete** (permanent). Generate icon is hidden in Trash.

### 4.3 Sort

Click "Date created ⌄" → menu (180w) with **Date modified / Date created**; active
item shows the header label; trigger gets bg #EBEBEB radius 5 while open. Plaud
only sorts by date in this build (Name/Duration not sortable). localplaud: make all
three columns sortable, show direction arrow, persist per view.

### 4.4 Selection & batch bar

Selecting any checkbox **replaces the header row in place** (no floating bar):

```
[■] Selected ( 2 )    [⇥ Move to folder] [✎ Rename] [⧉ Merge] [🗑 Move to Trash]          Cancel
```

- Header checkbox becomes tri-state (indeterminate when partial); click toggles all.
- Buttons: 32px tall, padding 8 16, gap 8 (icon 20 + label 12px), 1px #EBEBEB
  border, radius 5, white bg, 8px apart. Rename is disabled (#A3A3A3) when >1
  selected. Move to Trash is `danger` (#FF503F text/icon). Cancel is borderless
  text at the far right.
- Selected rows keep bg #F2F2F2 and a filled black checkbox.
- localplaud batch set: Move to folder, Tag, Reprocess…, Export…, Merge (if
  supported), Move to Trash.

### 4.5 States

- Loading: grey skeleton bars (#EBEBEB, radius 5) in row positions.
- Empty folder / empty trash: centred illustration-free message + secondary text
  (localplaud: icon + "No recordings yet" + Add audio / "Trash is empty").
- Trash has no banner in Plaud; localplaud should state the retention policy.

---

## 5. Recording workspace (`/file/<id>`)

Three panes plus an optional fourth:

```
┌sidebar 220┬ file slider 244 ┬──────────── main (fluid) ─────────────┬ Ask 364 (opt) ┐
│           │ header row is shared: 48px, spans slider+main(+ask)     │               │
│           ├─────────────────┼───────────────────────────────────────┤               │
│           │ recording list  │      tabs (centred 700 column)        │               │
│           │ (virtualised)   │      player (transcript tab only)     │               │
│           │                 │      content (scrolls)          ┆TOC  │               │
└───────────┴─────────────────┴───────────────────────────────────────┴───────────────┘
```

### 5.1 Workspace header (48px, padding 8 16, 1px #E5E7EB bottom border)

Left → right:

1. **List toggle** 36x36 icon button (radius 5) — collapses/expands the file slider.
2. Vertical 1px divider (16px tall).
3. **Breadcrumb**: folder glyph 20 + parent name ("All files" or folder) 14px in
   a 29px pill (padding 4 8, radius 5, hover #EBEBEB) → navigates back to that
   list; chevron 16 #A3A3A3; **file name** 14px truncate (hover #EBEBEB pill; click
   to rename inline).
4. Right cluster (gap 12): **Share** button (30px, padding 4 8, radius 5, 1px border,
   share icon + "Share"; when a link exists it becomes black-filled "🌐 Shared"),
   **Export** icon 28 (tooltip "Export"), **Find & replace** icon 28 (pressed
   state bg #EBEBEB), **More** `⋯` 28, 1px divider, **Ask** button (105x29,
   sparkle icon + "Ask Plaud" 14px, radius 5; pressed bg #EBEBEB while panel open;
   disabled grey when the file has no transcript).

localplaud: Share → local share/export; Ask → file-scoped Ask; keep the same order.

### 5.2 File slider (244px incl. 1px #EBEBEB right border, white)

- Virtualised list of the current category/folder, same order as the table.
- Item: 217x72, margin-bottom 4, padding 16 8, radius 5; title 14/22 #000
  single-line ellipsis (full title in tooltip on hover); meta 12/18 #7A7A7A
  `MM-DD HH:mm | 1h 22m 8s`; optional indicator icon row. 1px #EBEBEB separators
  between non-hovered items.
- Active item bg #EBEBEB; hover bg #F2F2F2 (`.2s`).
- Clicking switches the main pane without full reload; the active item is
  scrolled into view on load.
- Collapsible via header list toggle (`.3s` slide).

### 5.3 Tabs bar (inside main; centred 700px column, 55px tall, padding 8 0)

`Transcript` · `Summary ⌄` · `[+]`

- Labels 14/39; inactive #7A7A7A w400; active #000 **w600**; ~32px apart; no
  underline indicator.
- The note tab name is the note/template name; its chevron opens a 248w menu with
  **Change template** (re-generate with another template).
- `[+]` 32x22 button, bg #EBEBEB, radius 5, margin-left 12 → **template chooser**
  (5.9) to add another note. Plaud shows only one note tab in this account;
  localplaud: one tab per note (multiple notes coexist), overflow into a `⋯` menu.
- There is **no Mind map tab**: the mind map is embedded at the end of the note
  (5.8). localplaud: keep it embedded *and* allow a full-screen mind-map view.

### 5.4 Player (transcript tab only; pinned under the tabs)

Container 700x150, padding 48 0 40:

- **Progress bar**: 6px track `#F0F0F0`, radius 3, black fill (`width .1s
  linear`), 6px tall hit area (localplaud: enlarge hit area to ≥16px, keep 6px
  visual; add hover time tooltip and keyboard arrows).
- **Controls row** (40px, margin-top 16), three groups:
  - left: time `00:20 / 58:29` 12px (current #000, separator and total #7A7A7A),
    `HH:MM:SS` format when ≥1h;
  - centre (gap 0): back-15s (38 box, 15-in-circle icon) · **play/pause** (40px
    circle; idle: bg #EBEBEB play glyph; playing: white with 1px ring and pause
    glyph; loading: spinner glyph) · forward-15s;
  - right: **speed** button 32 (icon shows current rate, e.g. "1x", with AI
    sparkle when enhancement on) and **Trim audio** 32 (margin-left 24).
- Speed popover (212w, radius 5, `shadow-popover`): caption "Playback speed"
  12px #A3A3A3, options 0.75x / 1x / 1.25x / 1.5x / 1.75x / 2x (37px rows,
  check mark on current), divider, toggle row "✓ Improve speech clarity".
- A 1px #EBEBEB rule closes the player block.
- localplaud must add: keyboard (Space play/pause, ←/→ 5s, Shift+←/→ 15s,
  `,`/`.` speed), visible focus rings, and keep the player visible on the notes tab
  as a compact bar (Plaud hides it there — a gap, not a feature).

### 5.5 Transcript

- **Polished vs raw**: when an AI clean-up exists, a centred chip (bg #EBEBEB,
  radius 5, padding 8, 38px tall, margin 40 auto 12) reads
  "📄 Transcript cleaned up. **View original**". "View original" opens a
  read-only **Original transcript** modal (708x720, white, radius 5, title 16 +
  close X, same segment format at 12px meta). localplaud: replace modal with an
  in-place **segmented toggle `Corrected | Raw ASR`** at the top of the transcript
  plus a diff-friendly read-only raw view; keep the chip wording style.
- **Segment** (virtualised list, 700 wide):
  - padding 20 0, radius 6, pitch ≈84px for a one-line segment;
  - header row 18px, margin-bottom 4: timestamp `00:00:15` 14/20 **#646A73** ·
    speaker trigger (margin-left 8, padding 4, radius 5) with name 14 #000 ·
    per-segment play glyph 18 (visible on hover and on the active segment, shows
    pause while playing);
  - right side of header (hover only, margin-right 24): **Edit** (pencil 18 in 24
    box) and **Copy** (18 in 24 box);
  - text 14/22.75 #3D3D3D, wraps to full width.
- **Active segment** (during playback): text turns #000, segment play glyph shows
  pause; the list **auto-scrolls so the active segment sits at the top** of the
  viewport, just under the player. No background tint, no word-level highlight.
  localplaud target: same auto-scroll, plus subtle left bar/tint on the active
  segment and word-level highlight when word timestamps exist; pause auto-scroll
  for 5s after manual scroll and show a "Back to current" pill.
- **Click-to-seek**: clicking the timestamp or segment play glyph seeks and plays.
- **Speaker rename popover** (click a speaker name; hover shows pencil 14 + bg
  #EBEBEB): 310w, radius 5, `shadow-popover`; input prefilled-as-placeholder with
  current name; row of recent-speaker suggestion chips (outlined, 12px, radius 5);
  radios "Apply to this segment" / "Apply to all segments from this speaker"
  (default all); footer Cancel (outline) + Save (black, disabled until changed).
- **Name speakers dialog** (More → Name speakers): 708x736, title "Name speakers";
  one row per speaker: left 196x40 input (placeholder current label), right
  "Total duration: 25m 49s", then 2 sample clips (play button 20 + `00:06:16 -
  00:06:48` 14 #7A7A7A + duration, 3-line excerpt 14/22) with a dot pager; rows
  separated by 1px rules; footer note "Speaker names will be updated automatically
  in this file." + Cancel / Save (black). This is the model for localplaud's speaker
  editor (we add colour swatch + merge speakers).
- **Inline edit**: pencil enters edit mode for that segment's text (not exercised
  in the audit). localplaud: contenteditable segment with Save/Cancel, Esc cancels,
  Ctrl+Enter saves, edits create a revision.
- **Feedback** at end: Good / Bad outline buttons (32px). Omit or map to local
  "report ASR issue" if useful.

### 5.6 Find & replace

Header icon toggles a floating panel anchored under the header at the right of
the main pane (325w, radius 5, 1px #E5E7EB, `shadow-popover`, white):

- Row 1 (41px): input "Find & replace" (no border) · counter `1/34` 12px
  #A3A3A3 · prev ⌃ / next ⌄ (24 boxes, disabled until matches) · replace-mode
  toggle (24, #177BE5 when on) · close ✕.
- Row 2 (replace mode only): input "Replace with..." full width; buttons right:
  "Replace all" (text, #78776F) and **"Replace ↵"** (black, white text, 32px,
  radius 5).
- Matches highlighted `#FFE14D`, current `#FFA64D`, list scrolls to current match.
  Enter = next / replace (per mode).
- If the current match is inside a speaker label, row 2 swaps to "Edit this
  speaker's name? [Edit name]" (black button) — replacement never silently renames
  speakers.
- Applies to whichever tab is open (transcript or note).

### 5.7 Notes tab (note document)

- Scroll container is the note editor; content column 700 (content-area padding
  0 72). Title textarea 32/44 w500 (editable inline, auto-grow).
- Block editor: hovering a block shows a left gutter `+` (insert) and `⋮⋮` drag
  handle (28 boxes, at −54/−23px). Supports H1–H4, paragraphs, bullet/numbered
  lists, to-do checkboxes, quotes, code, tables, images (Plaud embeds AI
  infographics), bold/italic/underline/strike, mentions.
- Styles: h2 24/36 w500 #000 mt 32; p 16/26 #3D3D3D mt 8; li 16/24; hr 1px
  #E5E7EB with ~40px vertical space; to-do checkbox 14px outline.
- **Section TOC rail**: at the right edge of the main pane (x = main right − 44),
  a 12px-wide column of 16x2 dashes (one per heading, 14px apart, inactive
  #D0D5DD, current #000). Hover expands a 248w popover listing headings (14px,
  current row bg #F2F2F2); click scrolls to the heading. Also shown on the
  transcript tab as a scroll-position rail.
- End of note: "Template used: **<template name>**" (16px #7A7A7A, link
  underlined blue) → template detail; then the **Mind map card**; then Good/Bad
  feedback.
- Loading state: 7 skeleton bars (first 215w, then 5 full-width, last 400w; 16px
  tall; 21px apart; #EBEBEB; radius 5).
- Empty state (no note yet): centred 40px outline glyph, "Ready to generate"
  18px #000, "Summary will appear here after generation" 14px #7A7A7A, and a
  bottom-anchored black **Generate** button (464x48, radius 5, sparkle + label)
  with a first-run coachmark ("Click "Generate" — Choose a generation method.").
  Transcript tab has the same empty state ("Transcript will appear here after
  generation"). localplaud: show stage status instead — Queued / Transcribing
  (progress) / Failed (error + Retry) / Degraded (diarization unavailable) —
  and a Generate action only when the user must choose.

### 5.8 Mind map

Embedded card inside the note: 464x262, 1px #EBEBEB border, radius 5, padding 16;
caption "Mind map" 14px; markmap horizontal tree, root at left, curved pastel
branches (palette in 1.2), node text 12px, nodes foldable. Export via Export menu.
localplaud: card + "Open full screen" button with zoom/fit/fold-all controls.

### 5.9 Template chooser dialog ("Select a template")

Opened from the `+` tab, the note tab's "Change template", row generate icon,
or empty-state Generate.

- Dialog 1244x804 (inset 98px/48px at 1440x900), bg #F9F9F9, radius 12,
  `shadow-dialog`, close ✕ top-right; title 32/44 w300 at padding 16.
- Left column 178w: search field (36px, bg #EBEBEB, radius 5, placeholder `Try
  "Meeting"`), then nav items 36px (icon 16 + 14px label, active bg #E5E5E5
  radius 5): Recently used, Recommended, Favorites, My templates, My
  contributions, then scenario categories General, Meeting, Speech, Call,
  Interview, Medical, Sales, Consulting, Education, Construction, … (scrolls).
- Grid 3 columns, 16px gaps, cards 325x220 white, 1px #EBEBEB, radius 5,
  padding 16: coloured icon 24 · title 20px w300 (2-line clamp) · description 14
  #7A7A7A · footer 12 #7A7A7A: "Plaud" (first-party) or "▤ 83941 • author".
  Selected card: dark border + check (not exercised).
- Footer bar (white, 72px): **AI model** select (140w, caption 11px "AI model",
  value "Auto"; menu items show model name 14 + 2-line description 12 #7A7A7A,
  check on current), **Language** select (same pattern, "Auto"), and right-aligned
  **Generate now** (black; disabled grey until a template is selected).
- localplaud: identical structure; categories = local template categories;
  footer adds **Execution profile** select and a privacy badge (Local / Cloud).

### 5.10 File-level Ask panel

Toggled by header "Ask"; a 364px column on the right pushes the main pane (the
700 column re-centres in the narrower space).

- Header 56px: "Beta" badge (11px, 1px border, radius 4), right: clear-chat 🗑
  and close ✕ (24 boxes).
- Empty thread: 3 **suggested questions** as stacked cards (full width, padding
  16, 1px #EBEBEB, radius 5, 14/21 #000) — click sends (not exercised).
- Above the input: horizontally scrolling **skills** chips (40px, white, 1px
  #EBEBEB, radius 5, 14px, coloured 16px icon): Generate an infographic,
  Insights, Confirm action items, More skills. First-run coachmark "Meet
  Skills".
- Composer: 96px box, 1px gradient border (green→blue, radius 5), placeholder "Ask
  Plaud about this note", bottom-right deep-think toggle icon + round 32px send
  button (grey until text).
- localplaud: same layout; answers render citations as timestamp chips that seek
  the player; "Save as note" action per answer; skills are local quick actions
  and never mutate notes without explicit save.

### 5.11 Export menu, export dialog, share, more

- **Export menu** (header export icon; 248w, items 38–42px, 16px icons):
  Copy transcript · Copy notes · divider · Export audio · Export transcript ·
  Export notes · Export mind map.
- **Export transcript dialog** (416x290, radius 5, padding 24): title "Export
  transcript" 16px + ✕; rows (42px each): "Export format" with compact select
  (TXT / SRT / DOCX / PDF), "Include timestamps" checkbox (default on), "Include
  speaker labels" checkbox (default on); footer black **Export** button. Export
  notes / audio / mind map dialogs were not opened (they may download directly).
  localplaud formats: TXT, SRT, VTT, DOCX, PDF, Markdown, JSON for transcript;
  MD/DOCX/PDF for notes; original + MP3/M4A for audio; PNG/SVG/OPML for mind map.
- **Share dialog** (708w): tabs "Share link" / "Invite"; "Share with link" title +
  description; "Choose content to include" checkboxes Audio / Transcript /
  Summary; "Link expires" select (Never expires…); footer "Remove link" (outline)
  and "⧉ Copy link" (black). localplaud: local-network share link with the same
  content toggles and expiry, or omit until designed.
- **More menu** (250w): Move to folder · Re-transcribe · Name speakers · Move to
  Trash. localplaud adds: Reprocess stages…, Revision history, Processing details
  (provenance), Download original audio.

### 5.12 Keyboard & focus (Plaud baseline is weak)

Plaud exposes almost no shortcuts and its icon buttons are `<span>`s. localplaud
must use real `<button>`s with `aria-label`s, visible 2px focus rings, `Esc` to
close any popover/dialog (Plaud: Esc closes the whole stack — we should close only
the top layer), and `Ctrl/Cmd+F` → find panel when the workspace is focused.

---

## 6. Global surfaces

### 6.1 Search (command palette, not a page)

Sidebar "Search" opens a **modal palette** over the current page; there is no
`/search` route.

- Panel 750x570, centred horizontally, top at ~165px, white, radius 5,
  `shadow-popover`, scrim `rgba(0,0,0,.5)`.
- Top row (padding 16): search field 40px (1px #000 border when focused, radius
  5, 20px magnifier, placeholder "Search files by name or keyword", clear ✕ when
  non-empty) + 40x40 **filter** button (bg #EBEBEB when open).
- Empty query: "Search history" caption (12px #7A7A7A) + 🗑 clear at right; history
  chips (bg #F2F2F2, radius 5, 14px, padding 4 12); then "Today" caption and
  recently opened files (14px name left, `YYYY-MM-DD HH:mm` 12px #7A7A7A right).
- With a query (results stream as you type, ~300ms debounce):
  1. "Ask Plaud" section: one row "✦ Search all sources for "<q>" with Ask Plaud"
     + Beta badge → hands the query to library Ask.
  2. "Best matches": rows 60px (padding 8, radius 5): title with matches
     highlighted in **#177BE5 text** (no background), second line = matching
     snippet (12/18 #7A7A7A, highlighted term blue), right column date
     `YYYY-MM-DD HH:mm` + source tag (`Transcript` / `Note` / title, 12px
     #7A7A7A).
- **Keyboard**: ↑/↓ moves a selection (bg #EBEBEB, radius 5), Enter opens,
  Ctrl+Enter opens in new tab, Esc closes. A 40px footer legend shows these hints
  (12px #7A7A7A with key glyphs).
- **Filter popover** (280x540, radius 5, `shadow-popover`, anchored under the
  filter button): "Date" radio group (Today / Last 7 days / Last 30 days / Custom
  range) and **"Comes from"** checkbox group listing capture sources (device
  models, Import, Desktop). This is where Plaud's source filter lives — not on the
  file table.
- localplaud: implement exactly this palette (global shortcut `Ctrl/Cmd+K` and `/`),
  sources = Plaud device type, local upload, Plaud import; add processing-state and
  folder/tag filters; snippets must link to the transcript timestamp.

### 6.2 Ask (library) — `/ask`

- Empty state is a single centred column (706 wide, vertically ~30% from top):
  title "What's on your mind?" 32/44 w300, then the composer.
- Composer: 706x57, 1px **gradient border** (`linear-gradient(90deg,#8F53ED,
  #00D0FF 50%,#21EF6A)`, radius 5, implemented as 1px padding on a gradient
  wrapper), inner textarea padding 12 4, placeholder "Ask Plaud about
  recordings, notes, or anything else" 14px #A3A3A3; floating "Beta" label (12px
  #A3A3A3 on white) breaking the top border at left; right side: **Deep thinking**
  toggle (30px round, tinted blue when on, tooltip) and 32px round send button
  (grey #EBEBEB idle → black when text present). Enter sends; Shift+Enter newline.
- No suggested questions, thread history, or scope picker were visible on the
  library page in this build (typing `@` does nothing). Suggested questions exist
  only in the file-level panel (5.10).
- localplaud target (exceeds Plaud): same hero + composer, plus a scope bar
  (All / folder / tag / speaker / date / selected recordings), 3–4 suggested
  questions drawn from recent recordings, a left/right thread history list, and
  answers with numbered citations that open `/file/<id>?t=<sec>`.

### 6.3 Templates — `/templates` (Plaud "Template Community")

- Page header (48px, no border): icon 20 + "Template Community" 14px left; right:
  search field 286x32 (bg #EBEBEB, radius 5, placeholder `Try "Meeting"`) and
  black **Create template** button (32px, radius 5, pencil icon + 12px white
  label).
- Sub-tabs at x=244: **My space** / **Discover** (14/22; active #000 w600,
  inactive #7A7A7A; no underline). Hash routes `#mine` / default.
- Discover: a horizontal row of category chips (36px, 1px #EBEBEB, radius 5,
  padding 0 12, icon 16 + 14px label): General, Meeting, Speech, Call,
  Interview, Medical, Sales, Consulting, Education, Construction, … Then
  sections, each a 32/44 w300 heading (with "›" link to the full category) and a
  **horizontal carousel** of template cards (342x220, 16px gap) with an 80px white
  fade on the right edge and round prev/next buttons (32px, white, 1px border)
  appearing on hover. First section "Top this week" adds rank badges ("No.1",
  12px italic on a pale olive tab at the card's top-right corner).
- Template card: padding 16, 1px #EBEBEB, radius 5; coloured glyph 28px (Material
  Symbols Rounded, per-template colour); title 20/30 w300 (2-line clamp);
  description 14/20 #7A7A7A; footer 12/18 #7A7A7A: usage icon + count • author,
  or "Plaud" for first-party.
- My space: "Recently used ›" carousel, "My templates ›" grid whose first card is a
  dashed-feel **Create template** tile (pencil 28 + 20px label centred).
- **Template detail dialog** (708x720, radius 5): close ✕; header centred: icon 28
  + title 32px w400, meta line "Category • usage • author" 12px #A3A3A3; a grey
  translation banner (bg #EBEBEB, "Translated to English. View original");
  "Description" 18px heading + 14px body; "OUTLINE" heading + bullet list of the
  prompt's requirements (16/24); sticky footer with two 48px buttons: **Favorite**
  (outline) and **Set for next use** (black).
- **Create template** page (`/templates/edit?action=create`): breadcrumb (icon ›
  "Create template"); centred 708 column; "Template details" 32px w300;
  "Template name" label 18px + 44px input; "Prompt" label + 132px textarea; footer
  pinned at the bottom: Cancel (outline 322x48) and Create template (black
  322x48).
- localplaud: keep both tabs ("My templates" / "Library"), add version badge,
  provenance (built-in / mine / imported), "Duplicate to edit", and a structured
  prompt editor (sections, language, output format) instead of a single textarea.

### 6.4 Explore / Discover — modal

Sidebar "Explore" opens a **large modal**, not a page: 1196x765, radius 5, left
nav 220w (bg #F9F9F9) titled "Explore" (20px) with items AutoFlow, Plaud apps,
Integrations, Share ideas (external link, tooltip "Redirects to feedback page");
content pane padding 52, section title 24px w300 with a 1px rule.

- **AutoFlow**: intro paragraph 14px; "Allow notifications" row with a black
  36x20 switch; 1px rule; note "AutoFlow is view-only on the web. To make
  changes, please use the Plaud mobile app." (12px #7A7A7A); rule cards in a
  flowing grid (275x116, 1px #EBEBEB, radius 5, padding 16): top row = trigger
  icon (e.g. mic) › action icon(s) (coloured 16px), status at right ("Enable" #000
  / "Disable" #A3A3A3, `cursor: not-allowed`); body = rule name or first words of
  the human-readable rule (14/22, 2-line clamp); hovering shows a dark tooltip
  with the full sentence ("When the first 60 seconds of a new recording contain
  the keyword "…", automatically summarize it using the "…" template.").
- **Plaud apps**: hero image, two columns (Mobile app / Desktop) with bullets and
  download links. localplaud: replace with "Clients & API" (API token, CLI, MCP).
- **Integrations**: Zapier card (logo, description, "How it works?" link, black
  "Go to Zapier ↗") and a "More apps coming soon" card with a logo strip.
  localplaud: webhook and SMTP destination cards with health, scopes, last use.
- localplaud should promote Discover to a real page (`/discover`) because rules are
  locally editable; keep the card grid + sentence tooltip, add an editor drawer,
  ownership badge (Local / External, read-only), and run history.

### 6.5 Settings — modal

Opened from the workspace popover's "Settings" button. Modal 1196x720 (inset
122/90), radius 5. Left nav 220w (bg #F9F9F9): "Settings" 20px title; group
captions 14px #7A7A7A ("Account", "Workspace", "Help & support"); items 38px
(icon 16 + 14px label, active bg `rgba(31,35,41,.08)` radius 5). The content pane is
one long scroll; nav items scroll to sections. Close ✕ top-right.

Section typography: section title 24/36 w300 + 1px rule; row label 14/22 #000;
helper 12/18 #7A7A7A; control right-aligned; black 36x20 switches (radius 5 —
squarish, not pill); outline buttons 32px; destructive outline button red
(#FF503F border/text).

Sections observed (content structure only):

| Group | Section | Contents |
|---|---|---|
| Account | Account & security | Display language (select); Sign-in methods (provider rows with Remove/Add); **Active sessions** table (Device · Signed in with · Last active · sign-out icon action; "Current" tag in #177BE5); Country/Region; Account actions (Sign out, Delete account + warning) |
| Workspace | Personalization | Personal info (name), profile key/value card (read-only on web), "More about you" 500-char textarea; **AI settings**: Content focus input + suggestion chips, Custom instructions input + chips; Memory (toggle + Manage) |
| Workspace | Preferences | Transcription language (default, overridable per file); Transcript: "Clean up transcript automatically" switch; **Auto speaker labeling**: Auto-label speakers switch, Sync speaker labels switch, "Add your voice profile" button, **Other speakers** table (name, rename ✎, delete 🗑) |
| Workspace | Custom vocabulary (Beta) | enable switch; Industry select; Vocabulary chips + "Add" |
| Workspace | Private Cloud Sync | explanation; "Manage this setting in the mobile app"; connected devices table (Device · Signed in with · Status On/Off) |
| Workspace | Authorized apps | table: Application · Permissions · Authorized date · Revoke |
| Help & support | Contact support, Send feedback, Help Center (external), About (social + legal links, Cookie settings) |

localplaud mapping: keep the modal-with-left-nav pattern **or** a full page with the
same left nav; sections = Account & sessions, Workspace & appearance,
Preferences (language, clean-up, speakers library), Vocabulary, ASR, Diarization,
LLMs & profiles, Embeddings, Plaud connection (OAuth, sync), Integrations
(webhooks, email, API tokens = "Authorized apps"), Automation, Backups, Privacy,
System health, About.

### 6.6 Add audio

"Add audio" is a **dropdown** (not a dialog) in the current build: menu 240w
under the button with **Start recording** (browser microphone recording) and
**Import audio**. Import audio opens a 656x496 dialog: title 16px + ✕, a large
click-or-drop zone (cloud-upload glyph 48, "Click or drag audio files to upload"
14px), footer hint 12px #A3A3A3 "Max file length: 24 hrs. Supports … (MP3, MP4,
WAV, OGG …)". A separate dismissible promo bar sits above the dialog. The older
"Import from Plaud" option is gone from Plaud Web. localplaud: menu = Record in
browser (future), Import audio (device), Sync from Plaud now.

### 6.7 Notifications, plan widget

- Plaud Web has **no notification centre or bell** in this build. AutoFlow has a
  notification toggle only. localplaud already has an inbox; surface it as a bell
  icon button in the sidebar brand row (with unread dot) rather than a full nav
  item.
- Plan widget: see 2.1 bottom block; the info icon tooltip reads "Monthly minutes
  + add-on minutes". Clicking the plan name opens membership (not exercised).

---

## 7. Phone and narrow widths

**Plaud Web is not a phone product.** With a phone viewport (390x844, mobile UA):

- The sidebar is removed entirely and replaced by a 44px fixed top bar
  (`.mobile-logo-bar`): wordmark left, avatar + chevron right. The avatar menu
  only contains name, account id, and Sign out. **There is no navigation**: no
  way to reach folders, search, templates, or settings.
- Pages keep their desktop geometry: Home and All files render the 952px column
  inside a 390px viewport (only the name column is visible; duration/date/actions
  are clipped off-screen); Ask's composer is clipped; Templates cards are
  full-bleed carousels.
- The recording workspace (`/file/<id>`) renders the desktop three-pane layout
  squeezed: file slider occupies ~40% of the width, the note column is cut off on
  the right, the workspace header and player are not reachable. It is effectively
  broken.
- At 820–1024px with a desktop UA, the full sidebar stays (no auto-collapse); at
  820 the home/table column overflows horizontally; at 1024 the workspace still
  works with a narrower content column.
- Plaud's answer on phones is the native app (see `docs/plaud-mobile-app.md`).

localplaud must therefore take its phone IA from the native app study, not from
Plaud Web. Required responsive behaviour for builders:

| Width | Shell | Library | Recording workspace |
|---|---|---|---|
| ≥1280 | sidebar 220 + content | centred 952 table | sidebar + slider 244 + main (+Ask 364) |
| 1024–1279 | sidebar auto-collapsible (persisted) | table, date column narrows | slider hidden by default (toggle), Ask as overlay drawer |
| 768–1023 | sidebar becomes off-canvas drawer (hamburger in a 48px top bar) | table drops Duration into the second line | single pane; slider as drawer; Ask full-height drawer |
| <768 (phone) | 56px top bar (menu, title, search) + **bottom tab bar** (Home, Files, Ask, Settings; 44px targets, safe-area insets) | card list rows (title 16px, meta line `date · duration · state`), swipe-free `⋯` menu, sticky "+" FAB for Add audio | full-screen page with back button; tabs `Transcript / Notes / Mind map / Ask` as a sticky segmented control; **mini player docked above the bottom bar** (play, ±15s, scrubber, speed); transcript segments full-width 16px text; speaker popover and menus become bottom sheets |

---

## 8. localplaud gap list (vs current templates, 2026-10-02)

Compared against `src/localplaud/api/templates/*.html` on `main`.

### 8.1 Visual system

| Area | Plaud | localplaud now | Action |
|---|---|---|---|
| Palette | monochrome: white canvas, #F9F9F9 sidebar, black primary buttons, grey #EBEBEB/#F2F2F2 states | iOS-blue primary, blue active nav, grey `--bg #F2F4F7` page, blue-soft selections, purple gradient wordmark | Move to monochrome chrome: `--bg` white for content, sidebar #F9F9F9, primary button black, selection #EBEBEB/#F2F2F2. Keep blue only for links/search highlights/toggles, red for destructive. Keep dark mode (Plaud lacks it). |
| Type scale | 32px w300 page titles; 14px UI; 16/26 note body; 12px meta | 22px w700 titles, 13.5–14px UI, many 10–11.5px uppercase micro labels | Adopt the 1.1 scale; drop uppercase `nav-h`/`.st` micro labels (use 12px sentence-case). |
| Radius/shadow | 5px everywhere, 6px rows, flat (shadows only on popovers/dialogs) | 8–12px radii, card shadows, `.navi.on` shadow | 5px radius, remove resting shadows, use `shadow-popover` only on floating layers. |
| Icons | 20px outline SVG | emoji (🎙️ 🔍 📝 ⚡ 🔔 ⚙️ 🩺) | Replace with an inline SVG sprite (outline, 1.5 stroke, 20px). |
| Borders | hairline #EBEBEB | #E4E7EC/#D0D5DD 1px | Lighter hairlines. |

### 8.2 Shell

- Sidebar: 216 → **220px**, background #F9F9F9 without right border; add hover
  collapse button + collapsed state with brand-mark "expand" button.
- Nav order/labels → Search (palette), Home, Ask, Templates, Discover; divider; All
  files, Unfiled, Trash with counts; Folders section with colour glyphs, counts,
  hover `⋯` (Edit/Delete), `+` create, collapsible; bottom **status card**. Move
  Saved notes into Ask (history) or under the recording; move Notifications to a
  bell in the brand row; move Settings/Status into the workspace/user popover and
  status card.
- Add audio: outlined 38px button with dropdown (exists) — restyle to spec, remove
  emoji, menu items 38px.
- Missing today: Unfiled and Trash views, folder list in sidebar, counts,
  workspace/user popover, tooltips on icon buttons.

### 8.3 Home

- Current: "Welcome back" + 4 stat tiles + recent list + Needs attention.
- Target: "Recent files" 32px w300 heading + "View all ›" + 5 table rows (spec 4.2);
  "Needs attention" and "Processing now" as row lists in the same style; optional
  "Templates" strip. Remove stat tiles (move to System health).

### 8.4 File table (`index.html`)

- Current: page title + 4 stat tiles + filters + `table.rectable` + floating
  `bulkbar`.
- Target: title 32px w300 at 40px indent; header row Name/Duration/Date ⌄ with
  hairline; 58px rounded rows with hover #F2F2F2, hover-revealed checkbox in a
  40px gutter, generate + `⋯` actions; **batch bar replaces header row in
  place**; sort menu (Date modified/created, plus Name/Duration for us); source
  /date/state filters move into the search palette and a compact filter button.
- Processing state must stay visible per row (Plaud has none) — render as 12px
  chips under the title.

### 8.5 Recording workspace (`detail.html` + `_filelist.html`)

| Plaud | localplaud now | Action |
|---|---|---|
| 48px workspace header: slider toggle · breadcrumb (folder › title) · Share · Export · Find · More · Ask | "← All files" backlink, 22px title input with "Save title" button, metadata line, row of Export/Resume/Rebuild buttons, then `<details>` panels (Local data, Subscription independence, Execution profile) | Build the header bar. Title edits inline in the breadcrumb and note title (auto-save on blur/Enter). Move Resume/Rebuild/Reprocess, Local data, Subscription independence, Execution profile into **More ⋯** → "Processing details" drawer. |
| File slider 244px, 72px items, active #EBEBEB | `_filelist.html` 300px card list with chips, blue active | Restyle to spec, 244px, collapsible. |
| Tabs: Transcript · <note> ⌄ · [+]; text tabs, active w600 | tabs exist (transcript, imported, summaries, saved notes, mind map, ask) | Text-tab styling; Ask becomes the right **panel** (364px), not a tab; mind map embedded at end of note + full-screen; `[+]` opens template chooser. Imported Plaud artifacts get a clearly labelled "Imported" tab with provenance badge. |
| Player pinned under tabs in transcript tab: 6px bar, centred ±15s/play, speed popover, trim | sticky `#persistent-player` panel with shadow, grid controls | Restyle to 5.4 (700 column, centred controls, speed popover with check marks), keep it visible on all tabs (compact) — improvement over Plaud. |
| Segments: timestamp #646A73 · speaker (popover rename) · hover edit/copy; active segment auto-scrolls to top; text #3D3D3D→#000 | `.seg` rows with speaker colours, inline `form.segedit` | Adopt spacing (20px padding, 84px pitch), hover actions, auto-scroll-to-top behaviour with manual-scroll pause; keep our speaker colour dot. |
| Speaker rename popover with scope radios + suggestions; Name speakers dialog with sample clips | speaker rename exists (form-based) | Build popover + dialog per 5.5. |
| Find & replace floating panel with counter, prev/next, replace row, speaker-label guard | transcript search/find-replace exists in some form | Restyle into the 325px floating panel triggered from header icon and Ctrl/Cmd+F. |
| "Transcript cleaned up. View original" chip → raw modal | raw vs corrected switch exists | Use the chip pattern + in-place segmented toggle. |
| Notes: block editor, 32px title, 24px h2, 16/26 body, TOC rail, "Template used: X" footer | rendered markdown | Apply note typography + TOC rail + template provenance footer. |
| Template chooser dialog (1244x804) | template select controls | Build the dialog per 5.9 (with profile select). |
| Export menu + per-type dialog with format + timestamp/speaker toggles | `#open-export` dialog | Split into menu + per-type dialogs per 5.11. |

### 8.6 Other pages

- **Search** (`search.html` page): convert to the palette (6.1); keep `/search` as a
  deep-linkable fallback.
- **Ask** (inside search/notes): build `/ask` hero + composer (6.2) and the file
  panel (5.10); saved notes become "Saved answers" in the Ask history.
- **Templates** (`templates.html`): adopt My space / Library tabs, chips,
  carousels or grid, card spec, detail dialog, create/edit page (6.3).
- **Discover** (`discover.html`): card grid with trigger→action icons + sentence
  tooltip, editor drawer, ownership badges (6.4).
- **Settings** (`settings.html`, 455 lines of tiles): restructure into left-nav
  sections with the 24px w300 section titles, row/label/helper/switch layout
  (6.5). Black squarish switches.
- **Status** (`status.html`): fold into Settings → System health + sidebar status
  card.
- **Mobile**: current `@media (max-width:820px)` turns the sidebar into a
  horizontally scrolling icon row. Replace with section 7 (top bar + bottom tab
  bar + mini player + bottom sheets).

---

## 9. Not observed / caveats

- Inline transcript text editing, segment play from a non-active segment, trim
  editor, Start recording, Export audio/notes/mind-map dialogs, full-screen mind
  map, Ask answers and citations, template card selection state, folder
  create/edit dialog, Change template flow, and Trash empty state were **not**
  exercised (they mutate, download, or need data the account lacks).
- No dark mode exists in Plaud Web.
- Measurements were taken at deviceScaleFactor 1; hairlines report as
  0.59px because of the host display scale — treat them as 1px.

## 10. Changelog

- v1 (2026-10-02): tokens, shell, Home, file tables, recording workspace.
- v2 (2026-10-02): Search palette, library Ask, Templates, Explore, Settings, Add
  audio, notifications/plan, phone behaviour, gap list, caveats.


## Mini integration follow-up (2026-10-02, not deployed)

- Shared surface assets load once. Ask, Templates, Discover, Search and Settings
  initialise on HTMX navigation and history restoration; repeated settle events
  preserve live requests rather than cancelling their listeners.
- A library with recordings but no indexed content shows processing/index recovery
  navigation instead of suggested questions. Provider failures additionally link to
  connection settings. Timestamp labels use H:MM:SS after one hour.
- The handoff's proposed service worker is **not enabled**: its page cache included
  authenticated recording pages and did not partition by account or locale. No
  persistent offline recording cache is promised; the offline notice requests a
  connection to load recordings or save changes.
- Source-level lifecycle and rendered-route regressions are covered by the Mini
  tests. Final integrated browser acceptance and live deployment remain separate
  gates; this follow-up does not claim deployment.
