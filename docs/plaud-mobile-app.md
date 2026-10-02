# Plaud mobile app (iOS/Android) — reference for localplaud's phone layout

Status: research note, 2026-10-02. Scope: the official Plaud App ("Plaud: AI Note
Taker", iOS App Store id6450364080, current iOS version 3.29.x; Android package
`ai.plaud.android.plaud`), App 3.x design generation. Onboarding, device pairing,
Bluetooth/Wi-Fi transfer, and purchase flows are out of scope.

This note describes structure, layout, measurements, and interactions in our own words.
It contains no private recording content, account data, copied marketing text, or
Plaud artwork. localplaud keeps its own branding (`docs/brand.md`). The Plaud wordmark
and logo must never appear in our UI.

The companion desktop/web audit is `docs/plaud-ui-parity.md` (when published).

## Evidence key and sources

Every observation carries a tag:

- **[S]**: official store screenshots. App Store and Google Play use the same
  10-image marketing set, localized.
- **[B]**: official in-app screenshots in Plaud's App/Web 3.0 design article.
- **[H]**: real app screenshots embedded in Plaud's official help-center articles,
  which are the richest evidence. Several are from slightly older 3.x builds.
- **[D]**: text of the official help center or release notes.
- **[I]**: inference or recommendation. Not observed.

| Source | URL |
|---|---|
| App Store listing | <https://apps.apple.com/us/app/plaud-ai-note-taker/id6450364080> |
| Google Play listing | <https://play.google.com/store/apps/details?id=ai.plaud.android.plaud> |
| App/Web 3.0 design article | <https://www.plaud.ai/blogs/news/plaud-app-3-design-intelligence> |
| Release notes | <https://www.plaud.ai/pages/plaud-release-notes> |
| Help center (all articles, public Zendesk API) | <https://support.plaud.ai/api/v2/help_center/en-us/articles.json> |
| Files: rename/move/merge/delete/restore | <https://support.plaud.ai/hc/en-us/articles/50636948998169> |
| Folders | <https://support.plaud.ai/hc/en-us/articles/50636821859097> |
| Sort and source (filter sheet) | <https://support.plaud.ai/hc/en-us/articles/52090258263961> |
| Search files | <https://support.plaud.ai/hc/en-us/articles/50637210520217> |
| Playback | <https://support.plaud.ai/hc/en-us/articles/52090308108441> |
| Edit transcript | <https://support.plaud.ai/hc/en-us/articles/52090330080665> |
| Find and replace | <https://support.plaud.ai/hc/en-us/articles/50637108335129> |
| Name speakers | <https://support.plaud.ai/hc/en-us/articles/50635937755161> |
| Auto speaker labeling | <https://support.plaud.ai/hc/en-us/articles/54027338385177> |
| Multidimensional summaries / New note | <https://support.plaud.ai/hc/en-us/articles/50636073405081> |
| Edit a summary (app) | <https://support.plaud.ai/hc/en-us/articles/52090350858905> |
| Mind map | <https://support.plaud.ai/hc/en-us/articles/57430809095833> |
| Ask, single file | <https://support.plaud.ai/hc/en-us/articles/50636602977817> |
| Ask, all files | <https://support.plaud.ai/hc/en-us/articles/50810294218137> |
| Ask Skills | <https://support.plaud.ai/hc/en-us/articles/55724668424473> |
| Templates | <https://support.plaud.ai/hc/en-us/articles/50636128812441>, <https://support.plaud.ai/hc/en-us/articles/50636094914841> |
| Auto vs custom generation | <https://support.plaud.ai/hc/en-us/articles/51794533414809>, <https://support.plaud.ai/hc/en-us/articles/58029408010137> |
| AutoFlow | <https://support.plaud.ai/hc/en-us/articles/50835520394009>, <https://support.plaud.ai/hc/en-us/articles/62437655066649> |
| Export | <https://support.plaud.ai/hc/en-us/articles/50835453223705> |
| Share link | <https://support.plaud.ai/hc/en-us/articles/50835493131289> |
| Settings: profile, preferences, app lock | <https://support.plaud.ai/hc/en-us/articles/50913191942937>, <https://support.plaud.ai/hc/en-us/articles/50933818499225> |
| Record in app / import audio | <https://support.plaud.ai/hc/en-us/articles/50594418855321>, <https://support.plaud.ai/hc/en-us/articles/50609466994713> |
| Smart audio trimming | <https://support.plaud.ai/hc/en-us/articles/50609597756185> |
| Custom vocabulary | <https://support.plaud.ai/hc/en-us/articles/50636065290137> |

The pages under `support.plaud.ai/hc/...` sit behind a Cloudflare bot check for headless
browsers. The public Zendesk JSON API above returns the same article bodies and image
URLs. Local study copies (gitignored, **never commit**) are in
`data/audits/plaud-ui-20261002/mobile/`: `ios-EN-*.jpg` (App Store), `play-*.jpg`
(Google Play), `blog3-*.jpg` (design article), and `hc/<article-id>-<n>` (help-center
images).

Confidence overall: **high** for navigation, the file list, filters, detail tabs, the
player, speakers, Ask, export/share, AutoFlow, templates, and settings, because real app
screenshots exist for each. **Medium** for exact pixel sizes, which are estimated from
screenshots. **Low** for gestures beyond those documented (only swipe-left and long-press
are documented) and for loading/error states (few examples).

---

## 1. Global navigation and shell

- **Bottom bar with three slots [B][H]**: Files (folder glyph, left), a large **black
  circular "+" button** (centre, about 56px, raised slightly above the bar), and Explore
  (2×2 grid glyph, right). The icons have **no text labels**. The active glyph is filled
  and the inactive one is outlined. The bar is white with no visible divider, and its
  height is about 56px plus the home-indicator inset.
- **Tapping + [H]** turns the button into a black **✕**, dims the screen above with a
  scrim, and pops a small white menu above the button with two large rows: *Start
  recording* (mic glyph) and *Import audio* (file glyph). Phone recording happens only
  when no device is connected [I].
- The tab bar is visible only on the two root screens (Files and Explore). All other
  screens are pushed full-screen with a back chevron, or presented as sheets [B][H].
- **Files top bar [B][H]**: on the left, a small "library" glyph with a ▾ chevron, which
  opens the Filter & sort sheet. On the right, a search glyph (magnifier with a sparkle,
  because search doubles as the Ask entry point) and a person glyph (opens Settings).
- **Settings is not a tab.** It is reached from the person glyph [H][D].
- **There is no Ask tab.** Library Ask is reached from search ("Ask Plaud anything").
  Single-file Ask is reached from the input pinned to the bottom of a recording [H][D].
- Display language follows the OS. There is no in-app language switch. Ten UI languages
  are supported, including Traditional Chinese [D].

## 2. Files (home)

Layout [B][H] (`blog3-White_Space_as_Oxygen`, `hc/52090258263961-0`, `hc/50637210520217-0`):

- A **huge light-weight title** for the current scope ("All files", about 40px, weight
  300) with a small ▾. Tapping it also opens the Filter & sort sheet. The title scrolls
  away with the list.
- **Rows, not cards.** Each row has:
  - the **title** at about 15–16px regular, near-black, wrapping to two lines. A leading
    emoji may appear when the title contains one (sample files use them). Default titles
    are the recording's date-time ("2025-09-10 16:23:35"), because Plaud names
    recordings by end time unless renamed or auto-named from a calendar.
  - a **meta line** at about 12px grey: date-time, a thin vertical divider, then the
    duration. Two formats have been seen: `09-10 16:23 | 38m` and a newer
    `Nov 14 at 11:29 AM | 1h 21m`.
  - full-bleed hairline dividers inset by the 20–24px page gutter. The row is about
    84px tall with two-line titles and about 64px with one-line titles.
- There is no thumbnail, avatar, status pill, or folder chip in the healthy state. About
  70% of the screen is white space, which Plaud states as a goal.
- **Swipe left on a row [D]** reveals Generate, Move to, Rename, Merge, and Delete.
- **Multi-select [H]**: entered from "…" near the All files heading, or with a
  long-press [I]. The screen switches to a huge **"Selected (N)"** title with a black
  **Done** pill at the top right. Each row gets a right-aligned circular checkbox (an
  empty ring, or a filled black circle with a ✓). A **floating white action card** sits
  near the bottom with a soft shadow and about 12px radius. It holds five icon-over-label
  buttons: Generate, Move to, Rename (disabled when more than one is selected), Merge
  (enabled only with two or more), and Delete (red).
- **Delete confirmation [H]** is a bottom sheet with a title, one sentence of warning,
  and two equal-width buttons: a black **Cancel** and a red **Delete**.
- **Trash [H]**: the same selection UI, with an action card of *Restore* and *Delete*
  (red).

### Filter & sort sheet [H] (`hc/50636948998169-2`, `hc/52090258263961-1`, `hc/50636821859097-3`)

A sheet that covers almost the whole screen from the top. It has a large light title
**"Filter & sort"** and a ✕ at the top right. Contents, top to bottom:

1. A sort control, "Date created ⇕". It opens a small popover with *Date created* and
   *Date modified*, and the selected one is ticked.
2. Scope rows, each with a line icon, a label, a grey count in parentheses, and a ✓ on
   the selected row: **All files (n)**, **Unfiled (n)**, **Trash (n)**.
3. A **Folders** heading with a **+** on the right (create a folder). Each folder row has
   an icon, name, and count, plus a "…" that opens a small popover with *Edit* (pencil)
   and *Delete* (red bin).
4. A **"Comes from"** heading with source rows such as *Note (4)*, *Note Pro (1)*, and an
   Import source. These are automatic **source folders**: imported files also appear
   under an Import source.

There are no tags in the app. Folders plus sources are the only organisation [H][D].

### Search [H] (`hc/50637210520217-1`)

Pushed full-screen with the keyboard open. The top row has a back chevron, a rounded grey
search field with the placeholder "Search or Ask …", and a filter/sliders button. Below
it is a row of three dropdown filters: **Date range ⇕**, **File location ⇕**, and
**Transcription status ⇕**. The first result row is a sparkle-icon entry, "Ask …
anything" with a *Beta* tag, which opens library Ask with the typed text. On the web,
search also matches inside transcripts and notes [D].

## 3. Recording detail

### Header [S][B][H]

- On the left, a back chevron. In the centre, a **two-item text switch: Sources |
  Notes**. The active item is darker, about 15px semibold, with a 2px black underline;
  the inactive one is grey. On the right are a **share/export glyph** and **"…"**.
- **"…" menu [H]** is a popover anchored at the top right, not a sheet. It contains
  *Move to folder*, *Find & replace*, *Re-generate*, *Name speakers*, and *Move to Trash*
  (red), each with a line icon and hairline separators.
- The default tab is Notes when notes exist [B][H].

### Sources tab [S][H] (`ios-EN-04`, `hc/52090308108441-0`, `hc/52090330080665-0`)

1. A small underlined sub-tab, **"Transcript"** (a "Transcript ▾" selector in newer
   builds that switches the transcript language).
2. **The player sits inline at the top of Sources, not docked** [H]:
   - *Expanded*: `00:00:01 / 01:21:05` (current time in black, total in grey) with a
     collapse ▾ on the right. Below that is a **full-width waveform** of the whole file
     (grey bars with a playhead line), then a control row: an outlined circular **play**
     button, **−15s / +15s** circular-arrow buttons, a **speed** button (shows 1×; options
     0.75–2× in six steps [D]), and a **trim/crop** glyph (opens Smart Audio Trimming).
   - *Collapsed*: one row with a grey circular play button, the total duration, and the
     trim glyph on the right.
   - What happens to the player while scrolling (sticky or not) is not documented [I].
3. An **Outline** section in newer builds [S]: rows with an underlined tappable
   timestamp (`0:04:30`) and a topic title. Thumbs up/down sit under it.
4. A **Transcript** heading, then segments. Each segment has a meta line with a grey
   timestamp (`00:00:24`) and the speaker name in near-black about 13px, an optional small
   speaker/sound glyph after the name, then the paragraph at about 16px with a line-height
   around 1.45. The gap between segments is about 24px. Text is plain, with **no bubbles,
   avatars, or speaker colours**. (The coloured name tags in `ios-EN-04` are marketing
   overlays.)
5. **Tapping a line while playing seeks** to it. The player and transcript stay in sync
   [D].
6. **Editing [H][D]**: long-press the text to get the native selection menu (Edit, Copy,
   Look Up…). *Edit* turns the paragraph into an editable field with a light blue
   highlight.
7. **Find & replace** (from "…") opens a find bar over the transcript [H].

### Speakers [H] (`hc/50635937755161-*`)

- **Tapping a speaker label** (it gets a thin outline box when tapped) opens a bottom
  sheet, **"Name this speaker"**: a text field with the current label, a *Recently used
  names* list (with an empty state, "No recently used names…"), two radio rows (*Apply to
  this paragraph* and *Apply to all paragraphs from this speaker*, with a note that the
  summary updates), and a full-width black **Save**.
- **"…" → Name speakers** opens a full-screen sheet with one block per detected speaker:
  a name field (placeholder "Speaker 1"), *Total duration*, and 2–3 **sample clips**
  (▷ button, time range, length, and a two-line greyed transcript excerpt) so the user
  can identify the voice. The footer has a hint plus **Cancel** (outlined) and
  **Confirm** (black).
- Settings → Preferences → Auto speaker labeling has *Auto-label speakers*, *Sync speaker
  labels* across recordings, and *Add your voice profile*, which asks the user to read
  for 30 seconds [D].
- Renaming after notes exist: the transcript updates immediately, and the summary updates
  only after regeneration (the sheet's wording suggests automatic updates in newer
  builds) [D][H].

### Notes tab [S][B][H] (`ios-EN-05`, `hc/50636073405081-*`, `hc/57430809095833-0`)

- **A horizontally scrolling row of note tabs**: *Summary*, *Meeting Summary*,
  *Highlights*, *To-dos*, a translated summary, and so on. The active one is bold with a
  ▾ (and in some builds a thin outline box), and the rest are grey. A grey rounded **+**
  chip at the end creates a new note. Each generation **adds a tab** and never overwrites
  an existing one [D].
- **Note body**: an editable **title** (about 30–34px, weight 300, wrapping across 3–4
  lines; this is where the recording is renamed [D]), then a **metadata block** with a
  thin grey left border (Date & Time, Location, Participants placeholders), then
  **keyword chips** (small grey rectangles in monospaced-looking type). After those come
  H2-style section headings (about 24px light) and body text (about 16px) with
  numbered/bulleted lists.
- A row of participant names in small grey text appears under the title in 3.0
  marketing shots [S][B].
- **Mind map [H]** is **not a separate tab**. It is a bordered card at the **end of the
  summary** with the title "Mind map", an expand ↗ icon, and a small horizontal tree
  preview with coloured branch lines. Expanding goes full-screen [I]. It cannot be edited
  and regenerates with the summary [D].
- **Editing a note [H][D]**: tap into the text and the keyboard opens with a **formatting
  toolbar** above it (H₁, Aa, bullet list, indent, outdent, quote, undo, redo, dismiss
  keyboard). Changes autosave and there is no Save button.
- **Not generated yet [B]**: a "Summary" sub-label, the title, an empty body, then a
  grey "Ready to generate" line with a hint, and a full-width black **✦ Generate**
  button near the bottom.
- **Generate sheet [B][H]** (`hc/51794533414809-1`): a bottom sheet with a grabber and
  two radio sections:
  - **Auto generation**: a sparkle icon and a blue-to-green gradient title. One line says
    it needs no setup.
  - **Custom generation**: when selected, it shows rows for *Template ›* (current value
    in grey), *Auto speaker labeling* (toggle), *Audio language ›* (Auto), and *AI model
    ›* (Auto).

  The footer is a full-width black **Generate now** button with gradient text.
- **New note sheet [S][H]** (`hc/50636073405081-1`): a large title "New note" with a
  *Beta* tag and ✕, then "✦ Suggested templates for this file:" above a **2-column grid
  of template cards**. Each card is about 160×230 with a line icon top left, an expand ↗
  top right, a 2-line name about 20px, a grey category chip, and an author at the bottom
  ("Plaud" or a community user). The selected card has a black border and a black
  corner-triangle ✓. The sticky footer is a black **Generate note** button. Tapping ↗
  opens a **template preview sheet** (icon, name, category · author, Overview, Preview
  sections, and a black *Use this template*).
- **Generating [B]**: skeleton grey bars in the note body, a centred "Generating…" label
  with a hint that the user may leave, and a soft blue/violet gradient glow at the bottom
  of the screen.

### Pinned Ask input [S][B][H]

The bottom of the detail screen (both tabs) has a **rounded input "Ask … about this
note"**. It has about a 12px radius, a **thin purple-to-green gradient border**, a small
*Beta* tab notch on its top-left edge, and a circular grey **send ↑** on the right. A
voice glyph appears in marketing builds. Tapping it opens the Ask sheet.

## 4. Ask

### Single-file Ask [S][H] (`ios-EN-06`, `hc/50636602977817-*`)

- A **near-full-height sheet** with a grabber above, about 16px top radius, and the
  parent visible behind. Its header has a **trash** (clear conversation) on the left, the
  centred title "Ask …", and **✕** on the right.
- **Empty state**: 2–3 **suggested questions** as full-width outlined boxes (about 8px
  radius, 16px text). Above the input is a horizontally scrolling row of **quick-action
  chips**, each with its own pastel fill (blue *Insights*, lavender *To-do*, green *Write
  Email*, yellow *Conclusion*, and so on). Newer builds show **Skills** chips here with a
  skill icon ("Summary with bullet points", "Generate an infographic") [H].
- **Conversation**: the user's message is a grey rounded box aligned right with about 70%
  max width. The answer is **plain full-width text** (no bubble) with bold key terms,
  lists, and small grey **numbered citation pills** (1, 2) inline.
- **Tapping a citation [H]** opens a second bottom sheet, **"Sources"**, listing the cited
  transcript segments (timestamp, speaker, and text). The cited segment is highlighted
  with a light blue fill.
- An **answer action row** under each answer offers edit (pencil), copy, and "…" on the
  left, and **save as note** on the right [S][H][D].
- **"Keep asking"**: a grey label above 3 follow-up questions shown as plain text rows
  (or outlined boxes in marketing builds) [S][H].
- A floating circular **↓ scroll-to-bottom** button appears when the user has scrolled up
  [H].
- **Skills sheet [H]** (`hc/55724668424473-3`): a "Skills" title with **+** (create
  skill), then a *Recommended* section and an *All skills* section. Each row has a
  coloured square icon, a title, and a one-line description.

### Library Ask [B][H][D] (`hc/50810294218137-2`, `blog3-Ask_Plaud`)

- It is opened from **search → "Ask … anything"**. It is a full-height sheet titled
  "Ask …" with ✕, and (in the 3.0 design) a **hamburger/history** glyph on the left.
- **Empty state**: one centred paragraph inviting the user to ask about recordings,
  notes, or any topic, then the input ("Ask anything") with a **deep-thinking toggle**
  (brain glyph). Turning it on shows a dark tooltip confirming it is active.
- **Answer**: a progress trail with "Completed scanning all files", a boxed sources card
  ("15 key sections"), and a collapsible "Deep thinking complete ▾". The answer has
  citation pills, plus grey chip-style "Keep asking" follow-ups.
- Only transcribed files are searched [D].

## 5. Templates [H][D] (`hc/50636128812441-*`, `hc/50636094914841-*`)

- Reached from Explore → **Template Community**, and from the New note sheet's "More
  templates".
- The header has a back chevron, a search glyph, and a black **✎ Create** pill.
- Two text tabs: **My workspace | Discover**. The active one is boxed or bold.
- **My workspace** has a *Recently used ›* horizontal carousel of cards (icon, name,
  description, author/usage count) and a *My templates ›* carousel whose first card is a
  dashed-border **"Create template"** card.
- **Discover** has *Top this week* (a carousel of cards with a "No.1" ribbon, usage count
  such as "13.2k", and author), then a *Categories* 2-column grid of category tiles
  (General, Speech, Meeting, Call, …), then per-category carousels.
- **Template detail** is a sheet with ✕, an icon and title, a *Publish to Community* card
  with a **Publish** button, the **Prompt** text, and a footer with a red-outlined delete
  icon button and a black **Edit**. For community templates, the footer button is **Set
  for next use** (it sets the default) [D].
- **Create** offers *Description to template* (name plus prompt) or *Photo to template*,
  where AI derives a template from a photographed document [D].
- Only a tile (grid) layout exists. There is no list view and no manual sort [D].

## 6. AutoFlow [S][H][D] (`ios-EN-07`, `hc/50835520394009-*`)

- Reached from Explore → AutoFlow. The **list screen** has a back chevron, **+** at the
  top right, a centred icon tile, a large light "AutoFlow" title, and a grey description.
  A **Receive notifications** toggle follows. Then each flow appears as a block: a mini
  pipeline glyph (mic › template icon), its status on the right ("Disabled ›"), and a
  **one-sentence natural-language description** of the rule. A "Send suggestion" link is
  at the bottom. Swipe left on a flow to rename or delete it [D].
- **A preset flow, "Auto-summarize new recordings", is on by default** [D].
- **Flow editor** is a sheet with a grabber, back chevron, and a blue **Done** text
  button. It has a *Name ›* row and an *Enable AutoFlow* toggle, then:
  - **When** (about 36px light heading): a mic icon, a black **Edit** pill, a bold
    sentence about a new recording syncing to the cloud and meeting all conditions, and a
    grey explanation. Below are label/value rows: Recording keyword (it must be spoken in
    the first 60 seconds), Recording duration ("0 min – 3 hours"), and Recording source
    (All).
  - **Then**: a sparkle icon, **Edit**, "Transcribe & summarize", a grey explanation, and
    rows for Summary template, Auto speaker labeling, Audio language, and AI model.
  - An optional **Send email** card with recipient and attachments.
- Flows run in list order and **only the first match runs**. The web can only view
  AutoFlows, while the app can edit them [D].

## 7. Explore tab [B][H] (`blog3-Functional_Color_main_color`, `hc/50835520394009-0`, `hc/50636128812441-0`)

- A **black hero band** at the top (on paid tiers): the plan name in huge light white
  type, "More details ›", a thin progress meter (minutes used versus remaining, or ∞
  Unlimited), and a white **Go Unlimited** button on lower tiers.
- A large light **"Explore"** title (it is the first element when the hero is absent).
- **Grouped plain rows** (line icon, label, optional grey value, ›) with hairline group
  separators:
  - Private Cloud Sync (On) and Plaud Desktop/Web (Mac / Windows / Web)
  - Template Community, AutoFlow, and Integrations (Google Calendar and others)
  - Share ideas and Refer a friend
  - Privacy & security
  - Help & support, Contact support, and About

## 8. Settings / Me [H] (`hc/50913191942937-1`, `hc/50933818499225-2`)

- Opened from the person glyph on Files. The header has a back chevron and a centred
  "Settings".
- **Profile block**: a large circular avatar with an initial (camera badge to change it)
  and the display name. Then **three stats** (Days, Recordings, Hours) as small grey
  labels over large light numbers, and a **52-week activity heat-map** (small square
  cells in light grey and cyan shades).
- Rows: **Personalization ›** (Memory: role, industry, preferred formats [D]),
  **Preferences ›**, **Account ›** (sign-in methods, password, two-step sign-in, active
  sessions, sign out, delete account [D]).
- **Preferences** groups:
  - *Transcript & summary*: Transcription language (Auto ›, with a grey helper line),
    Auto speaker labeling (On ›), Custom vocabulary (On ›).
  - *Notifications*: Messages & notifications ›
  - *Security*: App lock (Off ›; Face ID/Touch ID)
  - A *Help improve* toggle with a grey explanation and a "Learn more" link.
- **Custom vocabulary** has an icon tile, a title with a *Beta* tag, a description, an
  *Enable* toggle, and an *Industry & vocabulary* section with an Industry › picker and a
  Vocabulary › list with a count.
- Toggles are **black when on** (white knob) and light grey when off.

## 9. Capture (from the + button) [S][B][D]

- **Start recording** (a connected device records, or the phone mic records) opens a
  full-height sheet with a grabber (▾). It has *Pause* on the left, a waveform glyph and
  elapsed time in the centre, and *Stop* on the right. Below these are a "Recording
  highlights" heading and a timeline: flags (flag glyph and time), photos (image glyph,
  time, and the photo with a delete button), and typed notes ("Write down key points…").
  Two large outlined bottom buttons add a **flag** and take a **photo**.
- Highlights can only be added while recording. They appear afterwards in Notes →
  Highlights, and the summary prioritises them [D].
- **Import audio** uses the OS file picker [H].

## 10. Export and share [S][H][D] (`ios-EN-10`, `hc/50835453223705-*`, `hc/50835493131289-*`)

- The share glyph opens a **bottom sheet** with sections: *Share* (Share link), *Copy to
  clipboard* (Transcript ›, Notes ›), and *Export as file* (Audio ›, Transcript ›,
  Notes ›, Mind map ›).
- Each export row opens a **second sheet** titled "Export <type>" with ✕. It has an
  *Include* section with toggles for **Timestamps** and **Auto speaker labels**
  (transcript only), an *Export format ⇕* row (it opens a small popover radio list:
  TXT/SRT/DOCX/PDF for transcripts; MP3/WAV for audio; TXT/Markdown/DOCX/PDF for notes;
  PNG/Markdown for the mind map), and a full-width black **Export** button.
- **Share link** opens a full-height sheet with two tabs (*Share link | Invite*), *Done*,
  and a ? help icon. It has a "Share with link" heading and explanation, *Choose content
  to include ›* (All items), *Link expires ⇕* (Never expires), a black **🔗 Share link**
  button, and a plain *Remove link*.

## 11. Other tools [H]

- **Smart Audio Trimming** (from the trim glyph on the player) is a full-screen editor.
  It has *Cancel* (grey pill), undo/redo, and *Save as* (grey pill), with the file name
  centred. Below are a zoomed waveform with orange range handles and a blue playhead, the
  time readout, ⇤ ▶ ⇥ controls, a mini overview waveform with an orange selection box,
  and a bottom toolbar of **Smart Clip**, **Trim**, and **Delete**.

## 12. Visual language summary

| Aspect | Observation |
|---|---|
| Base | White / very light warm grey (`#F7F7F7`-ish) page background. Content sits directly on it, with almost no cards in lists [B][H] |
| Text | Near-black primary text, mid-grey secondary text (meta about 12px), and light grey placeholders [H] |
| Primary action | **Solid black** (`#000`/`#111`) buttons with white text and about 8px radius: Done, Save, Export, Generate now, Edit pills, toggles, and the FAB [S][H] |
| Destructive | Red text/icons, or a solid red button in confirmations [H] |
| AI accent | A **cyan→violet→green gradient** used sparingly: the Ask input border, the "Auto generation" title, Generate text, and the generating glow [B][H]. Blue appears for iOS text buttons (Done) and the light-blue highlight of cited/edited segments [S][H] |
| Type | A neutral grotesque (Inter-like). **Large titles are light weight (300)** at 30–40px. Body is about 16px with relaxed line height. Labels are about 13–15px [B][H] |
| Spacing | 20–24px side gutters, generous vertical rhythm, hairline dividers. About 70% white space is the stated goal [B] |
| Surfaces | Bottom sheets with a grabber and about 16px top radius over a dark scrim. Small popover menus for "…", sort, and format. A floating action card for multi-select [H] |
| Icons | Thin (about 1.5px) line icons, monochrome. Template/skill icons get a single pastel colour [H] |
| Badges | A small outlined grey *Beta* tag next to titles and on the Ask input [H] |
| Empty states | Short centred grey sentence plus one primary black button ("Ready to generate") [B]. Ask shows suggested questions instead of an empty box [H] |
| Loading | Skeleton bars, a "Generating…" label, and a gradient glow [B]. List-level processing indicators are not observed [I] |

## 13. Mobile app versus Plaud Web

| Area | Mobile app | Plaud Web |
|---|---|---|
| Navigation | 3-slot bottom bar (Files, +, Explore). Folders and sources live in a Filter & sort sheet. Settings is reached from the avatar | Persistent left sidebar: Home/All files, Ask, Template Community, Explore, folders with +, source folders [D][B] |
| File actions | Swipe-left actions, a "Selected (N)" multi-select with a floating action card | Hover "…" menus and hover checkboxes for batch actions [D] |
| Search | Full-screen search with three filter dropdowns and an "Ask anything" entry | Search page with *Find files* (title) versus *Find content* (inside transcripts/notes) and *Date* / *Comes from* filters [D] |
| Detail | **Sources / Notes** switch. The player is inline at the top of Sources. Note tabs scroll horizontally. The mind map is a card at the end of the summary | One flat tab row (Transcript, Highlights, Summary, …, +), a **note catalog** (outline) on the right, and **Ask docked in a right column** [B][D] |
| Ask | Pinned input that opens a near-full sheet. Citations open a "Sources" sheet | A right side panel with quick-action chips above the input [B] |
| Transcript edit | Long-press text, native menu → Edit | An explicit Edit button [D] |
| Note edit | Tap to type, with a keyboard formatting toolbar | Double-click a paragraph, with a side handle per paragraph [D] |
| AutoFlow | Full create/edit | **View only** [D] |
| Capture | Phone/device recording with flags, photos, and notes. Import audio | Upload. The Desktop app captures meetings [D] |
| Audio export | MP3 + WAV | MP3 only [D] |
| Settings | Profile stats and heat-map, Preferences (vocabulary, speaker labeling, app lock) | Settings page in the sidebar [D] |

---

## 14. Responsive Web App mapping for localplaud (viewport < 768px)

These are concrete, original implementations of the mobile patterns above. All
user-facing strings go through `i18n.py` (zh-TW and en). Touch targets are at least 44px.
Every gesture has a visible button equivalent. Honour `prefers-reduced-motion`.

### 14.1 Shell, navigation, and PWA

1. **Bottom tab bar, Plaud-parity 3 slots**: **Files** | **+** | **Explore**.
   - Fix it to the bottom, 56px tall plus `env(safe-area-inset-bottom)`, white, with an
     optional top hairline.
   - Files and Explore are `<a>` elements with `aria-current="page"` when active. Use
     filled versus outlined glyphs, **plus a visually hidden or 11px label** for
     accessibility. Plaud shows no labels, but our labels may be visible for clarity.
   - The centre **+** is a 56px black circle `<button aria-expanded>`. It opens a small
     anchored menu (not a full sheet) with **Import audio** (file upload), **Sync from
     Plaud now** (trigger the poller), and, only if browser capture is supported later,
     **Record**. While the menu is open, the button morphs to ✕ and a scrim dims the page.
   - Show the bar **only on the root views** (Files, Explore). Hide it on detail, Ask,
     editors, and while the soft keyboard is open (`visualViewport` height check).
2. **Files top bar**:
   - Left: a library glyph with ▾ that opens the Filter & sort sheet.
   - Right: **Search** (magnifier, which is also the entry to library Ask) and an **avatar
     button** that opens Settings/Me.
   - The page title is a large light "All files ▾" (`font-weight: 300; font-size:
     clamp(32px, 9vw, 40px)`), which is a second trigger for the same sheet.
3. **Explore** (localplaud's hub, which replaces Plaud's plan band with system status):
   - A top **status band** (dark, like Plaud's plan hero) showing pipeline health: queue
     depth, ASR/diarization/LLM availability, last Plaud sync time, and a "Details ›"
     link to System health.
   - Then grouped rows: **Ask (whole library)**, **Templates**, **AutoFlow / Automation**,
     **Integrations** (Plaud OAuth, webhooks, email), **Vocabulary**, **Sync & backup**,
     **Privacy**, **Help / About**.
4. **Settings/Me** (from the avatar): a profile block with **Days / Recordings / Hours**
   stats and an optional 52-week activity heat-map (computed locally), then
   *Personalization*, *Preferences* (transcription language, diarization, vocabulary,
   default template, notifications), *Providers* (ASR/LLM/embedding profiles), and
   *Account & sessions*.
5. **PWA and viewport**:
   - `<meta name="viewport" content="width=device-width, initial-scale=1,
     viewport-fit=cover">`
   - `manifest.webmanifest` with `name`/`short_name` "localplaud", original icons from
     `static/logo.svg` (192/512 PNG plus a maskable variant), `display: "standalone"`,
     `start_url: "/"`, `scope: "/"`, `theme_color` and `background_color` matching the
     tokens, and `apple-mobile-web-app-capable`/`-status-bar-style` meta tags.
   - Use `100dvh` (not `100vh`) for full-height screens, and `env(safe-area-inset-*)` on
     the top bar, tab bar, sheets, and pinned Ask input.
   - Inputs need font-size ≥16px to prevent iOS zoom-on-focus. Use
     `-webkit-tap-highlight-color: transparent` with visible `:focus-visible` rings.
   - Use `overscroll-behavior: contain` on sheets and scrollers.

### 14.2 Files list

6. **Rows, not cards**:
   - Title at 16px regular, clamped to 2 lines. The meta line is 12–13px grey:
     `MM-DD HH:mm │ 1h 21m`, with locale formatting through i18n.
   - Hairline dividers inside 20px gutters, and a minimum row height of 64px. The whole
     row is one link to the detail view.
   - **localplaud addition**: a tiny status marker at the end of the meta line, only when
     not healthy-complete. Examples: "Transcribing 42%", "Waiting for diarization",
     "Needs attention" (red dot). The healthy state stays clean like Plaud's.
7. **Row actions**:
   - Long-press (500ms, pointer events, with `contextmenu` suppressed) or a trailing "…"
     opens an **action sheet**: Generate notes, Move to folder, Rename, Merge (when
     available), Move to Trash.
   - Swipe-left reveal (translateX with a threshold of about 80px) is an optional
     enhancement. Never rely on it alone.
8. **Multi-select mode** ("Select" in the Files overflow, or long-press then "Select"):
   - The title becomes **"Selected (N)"** and a black **Done** pill appears at the top
     right. Rows show a 22px circular checkbox on the right.
   - A **floating action card** appears above the safe area with Generate, Move, Rename
     (single only), Merge (two or more), and Delete (red). Disabled items stay visible but
     are dimmed with `aria-disabled`.
9. **Filter & sort sheet** (the folder switcher), with full parity of structure:
   - Sort popover: Date created / Date modified / Duration.
   - Scope rows with counts: All files, **Uncategorized** (Plaud's "Unfiled"), and
     **Trash**.
   - Folders with **+** and per-folder "…" (Rename/Delete).
   - **Comes from**: capture-source filters such as Plaud device models, Import, and
     Plaud app capture.
   - **localplaud addition**: a *Status* group (Processing / Needs attention / Ready) and
     *Tags* when tags are enabled.
10. **Search** (full-screen route `/search`):
    - A back chevron, a rounded field with `type="search"`, `enterkeyhint="search"`, and
      autofocus, plus a filter button.
    - A chip row with Date range, Folder, and Status dropdowns.
    - The first result row is "✦ Ask the library: '<query>'", which opens library Ask
      prefilled. The other results group into *Files* (title matches) and *Content*
      (transcript/note hits with snippet and timestamp, so tapping one opens the detail
      view seeked to that moment).
11. **Pull-to-refresh**: not recommended for the web (it fights native overscroll). Use
    **"Sync now"** in the + menu and auto-refresh through HTMX polling or SSE while items
    are processing.
12. **Empty and edge states**:
    - Empty library: a centred grey sentence plus a black **Import audio** or **Connect
      Plaud** button.
    - Empty folder or filter: "No files match" plus **Clear filters**.
    - Trash shows its retention note.
    - Long titles clamp at 2 lines.

### 14.3 Recording detail

13. **A full-screen pushed view** (its own URL, `/recordings/{id}`):
    - A sticky top bar 52px tall plus the top safe-area inset. Left: a back chevron
      (`history.back()` if same-origin history exists, else the Files URL). Centre: a
      **segmented text switch "Transcript | Notes"** (our "Sources"), implemented as
      `role="tablist"` with a 2px underline on the active tab. Right: **Share** and
      **"…"**.
    - "…" is a **popover menu** on phones (Plaud uses a popover here, not a sheet) with
      Move to folder, Find & replace, Name speakers, Re-run stages… (ASR / diarization /
      notes, with provenance), View raw vs corrected, and Move to Trash (red).
    - Restore the Files scroll position on back, from sessionStorage keyed by list URL.
14. **Player: inline at the top of the Transcript tab (Plaud parity), plus a localplaud
    mini player**:
    - *Expanded card*: `current / total` time, a collapse ▾, a full-width waveform (it
      can be a precomputed peaks JSON drawn on `<canvas>`, or a simple progress bar as
      fallback), a control row of ▷ (48px), −15s, +15s, a speed menu (0.75–2×), and
      optionally Trim (later).
    - *Mini player (localplaud addition, recommended)*: when the inline player scrolls
      out of view, or on the Notes tab, a 56px **sticky bar sits above the pinned Ask
      input**. It holds ▷/❚❚, the time, a thin progress line, and +15s. Tapping it scrolls
      back to or expands the full player. This fixes a real usability gap, because
      citations and note timestamps must play from anywhere.
    - One shared `<audio>` element survives tab switches. Use the `MediaSession` API for
      lock-screen controls and artwork (our logo).
15. **Transcript**:
    - Segments with a meta line (grey `00:04:30`, then the speaker name in 13px semibold),
      the paragraph at 16px/1.5, and 24px gaps.
    - **localplaud addition**: an 8px **speaker colour dot** before the name. Plaud has
      no colours, but our spec asks for them, so keep them subtle.
    - The active segment gets a faint tinted background, and the active word gets an
      underline (word timestamps). Auto-scroll follows playback until the user scrolls
      manually, then a floating "↓ Back to current" pill appears.
    - **Tap a timestamp or paragraph to seek**, and play when the player is already
      playing (Plaud behaviour).
    - **Editing**: an explicit **✎ Edit** on segment long-press, or an edit button in the
      segment's meta line (more discoverable than relying on the native long-press menu).
      This opens inline `contenteditable` (or a textarea) with a sticky **Cancel / Save**
      bar above the keyboard. Saves create a corrected revision and the raw text is kept.
    - **Find & replace**: a top bar that slides under the header, with a field, match
      count, ▲▼, a Replace field, and Replace / Replace all.
16. **Speakers** (copy Plaud's two-level pattern):
    - Tap a speaker name to open the **"Name this speaker" bottom sheet**: a name field,
      *Recently used names* chips (from the speaker directory), radio options *This
      segment only* / *All segments from this speaker*, and a black **Save**. Note in the
      sheet that notes and the index refresh automatically, because localplaud re-indexes
      on edit.
    - "…" → **Name speakers** opens a full-screen sheet: per speaker, a name field, total
      talk time, and 2–3 ▷ sample clips with excerpts. The footer has **Cancel** and
      **Confirm**.
17. **Notes tab**:
    - A **horizontally scrolling tab strip** (`overflow-x: auto; scroll-snap-type: x
      proximity`) with one tab per note output (Summary, To-dos, Highlights, custom
      templates…). The active tab is bold with a ▾ that opens a sheet with *Regenerate*,
      *Change template*, *Revisions / provenance*, *Copy*, and *Delete note*. The trailing
      **+** chip opens **New note**.
    - The note body has an editable light title (renames the recording), a metadata
      block with a left border (date, duration, participants from speakers, source/model
      provenance line), keyword chips, and then content.
    - **Mind map**: like Plaud, a **bordered preview card at the end of the summary** with
      an expand ↗ that opens a full-screen pan/zoom view (`touch-action: none` on the
      canvas, pinch and drag, and +/− buttons for accessibility). It may also be offered as
      a tab for direct linking.
    - **Editing**: tap opens edit mode with a **sticky formatting toolbar above the
      keyboard** (H, bold, list, indent, quote, undo/redo, done). Autosave is debounced
      and shows a "Saved" indicator. Each save creates a revision.
18. **Generate flow**:
    - **Empty state**: a light title, grey "Notes not generated yet", and a full-width
      black **✦ Generate** button.
    - **Generate sheet**: *Auto* (an automatic profile picks the template) versus
      *Custom* (Template ›, Language ›, **Provider/model profile ›** with a local/cloud
      badge, Speaker labels toggle), and a black **Generate now** button. The
      provider/privacy boundary must be visible (CLAUDE.md principle 7).
    - **New note sheet**: "Suggested templates for this recording" as a 2-column card
      grid (icon, name, category chip, author/source, ↗ preview), with a sticky **Generate
      note** button.
    - **Running state**: skeleton bars, the stage label ("Summarizing section 3 of 7"),
      a note that the user can leave the page, and a subtle accent glow (with no
      animation under reduced motion).
    - **Failure state**: an inline error card with the stage, provider, and message, plus
      **Retry** and **Change provider**. The rest of the recording stays usable.
19. **Pinned Ask input** at the bottom of detail (both tabs):
    - A rounded 12px field labelled "Ask about this recording…" with a thin AI-accent
      border (localplaud blue→violet `#007AFF → #8F53ED`, not Plaud's cyan-green) and a
      circular ↑ send button.
    - It sits above the mini player and safe-area inset.
    - Tapping it opens the Ask sheet with focus in the input.

### 14.4 Ask

20. **Single-recording Ask sheet**:
    - Use `<dialog>` with `showModal()`, about 92dvh tall, a grabber, 16px top radius,
      and a header of 🗑 Clear, centred title, and ✕.
    - **Empty state**: 3 grounded suggested questions (outlined boxes) and a scrolling
      chip row of **quick actions / skills** (Summarize, To-dos, Insights, Draft
      email…). These insert a prompt, and nothing mutates notes automatically.
    - **Answers**: plain text with **numbered citation pills**. Tapping a pill opens a
      **"Sources" sub-sheet** listing the cited segments (time, speaker, and text, with
      the cited one highlighted). Each has ▷ to play from there, and closing returns to
      the conversation.
    - The answer action row has Copy, Edit question, "…", and **Save as note** (an
      explicit user action that creates a note revision with provenance).
    - "Keep asking" follow-ups appear as plain rows, and a ↓ scroll-to-bottom floating
      button appears when needed.
21. **Library Ask**:
    - Entered from Search ("Ask the library"), Explore → Ask, and on desktop the
      sidebar. It is a full-screen route (`/ask`) on phones so it can be linked and
      revisited, with a history drawer (hamburger, left).
    - A **scope chip** at the top: All files / Folder / Date range.
    - Answers show a progress trail ("Searched 128 recordings · 15 passages"), a
      collapsible sources card listing recordings, then the answer with citation pills
      that open the recording at the timestamp.
    - An optional "Deep search" toggle in the input maps to a larger retrieval or rerank
      budget.

### 14.5 Templates, automation, export

22. **Templates**:
    - Header: back chevron, search, and a black **Create** pill. Tabs: **Mine |
      Discover** (Discover is local/bundled template packs, so there is no network
      community unless one is configured).
    - Horizontal carousels (Recently used, My templates with a dashed "Create" first
      card) and a 2-column category grid.
    - Template detail sheet: provenance (author, version, source), the prompt, **Set as
      default**, Edit, Duplicate, and Delete.
23. **AutoFlow / Automation**:
    - The list mirrors Plaud: a notifications toggle, then one block per rule with a
      mini pipeline glyph, an enabled/disabled status, and a **one-sentence description**
      generated from the rule.
    - The editor is a sheet with Name, an Enable toggle, a **When** card (keyword in
      first N seconds, duration range, source) and a **Then** card (template, diarization,
      language, provider profile, delivery such as email or webhook), each with a black
      **Edit** pill and **Done**.
    - Rules owned externally show a lock badge and a read-only editor.
    - Show "first match wins" ordering with drag handles plus ▲▼ buttons.
24. **Export & share sheet**:
    - Sections: *Copy* (Transcript, Notes) and *Export as file* (Audio, Transcript,
      Subtitles, Notes, Mind map).
    - Each row opens a second sheet with format radio rows, toggles (**Timestamps**,
      **Speaker labels**) for transcript and subtitle exports, and a black **Export**
      button that triggers a download.
    - Leave out "Share link" unless localplaud implements authenticated share links. If
      it does, use Plaud's two-tab Share/Invite structure with expiry.

### 14.6 Shared components to build once

| Component | Behaviour |
|---|---|
| `BottomSheet` | `<dialog>` with `showModal()`, a focus trap, Esc/✕/backdrop to close, grabber drag-to-dismiss as an enhancement, `max-height: 92dvh`, internal scroll, bottom padding with `env(safe-area-inset-bottom)`, and a slide-up animation disabled under reduced motion. Restore focus to the trigger on close |
| `PopoverMenu` | Anchored menu for "…", sort, and format pickers. Arrow-key navigation with `role="menu"`. On very small screens it may fall back to `BottomSheet` |
| `ActionCard` | Floating icon-over-label toolbar for multi-select |
| `TabStrip` | Horizontally scrolling `role="tablist"` with arrow-key support, which scrolls the active tab into view |
| `Segmented` | The two-item Transcript/Notes switch |
| `MiniPlayer` | Sticky player bound to the single shared `<audio>` |
| `CitationPill` | A button that seeks/plays and opens the Sources sub-sheet |
| `Toast` | Bottom toast above the tab bar or Ask input, with `aria-live="polite"` |

### 14.7 Tokens (conceptual, localplaud-branded)

- Background `#F7F7F8`, surface `#FFFFFF`, text `#111`, secondary `#6B7280`, hairline
  `rgba(0,0,0,.08)`.
- Primary action **black** (`#111`) with white text, matching Plaud's monochrome. Keep
  localplaud's blue `#007AFF` for links/focus and the **blue→violet gradient** only on
  AI surfaces (Ask input border, Generate).
- Large titles weight 300 at 32–40px, H2 22–24px weight 400, body 16px/1.5, meta
  12–13px.
- Gutters 20px (24px at ≥ 600px), row min-height 64px, sheet radius 16px, button
  radius 10px, chip radius 8px.

---

## 15. Getting a real look at the app on this machine (feasibility)

Assessed on the SteamOS host (kernel `6.16.12-valve`, AMD x86_64, 15 GB RAM with about
5 GB free during the check, 75 GB free on `/home`, Wayland session). **Nothing was
installed.**

| Option | Feasibility | Notes |
|---|---|---|
| **Android Emulator (AVD, Google Play x86_64 image)** | **Feasible, recommended if needed** | `/dev/kvm` exists and the CPU has virtualization. Needs the Android SDK cmdline-tools, emulator, and one Google Play system image (about 3–4 GB total in `$HOME`, no root required). Android 11+ x86_64 images include ARM translation for ARM-only native libraries. Plaud must be installed from Play with a Google account (avoid third-party APK mirrors). The app can be used without a Plaud device. It needs a Plaud login: a **throwaway account** keeps private recordings off screen; using the user's account requires explicit permission, and captures go only under `data/audits/`. Rendering works under Wayland/X with `-gpu swiftshader_indirect` if host GL misbehaves. Expect 2–4 GB RAM while running, which is tight with other workloads |
| **Waydroid** | Possible but awkward | The kernel has `CONFIG_ANDROID_BINDER_IPC=y` and `CONFIG_ANDROID_BINDERFS=y`, but no binderfs is mounted, and SteamOS has an immutable root, so it needs root, LXC, a binderfs mount, and the Waydroid package installed outside the read-only image (or `steamos-readonly disable`, which updates wipe). Google Play needs GApps images plus device certification. Not recommended on this host |
| **Docker/Podman "redroid"** | Possible, needs root/binder | Same binderfs requirement as Waydroid. Not worth it here |
| **iOS on Linux** | **Impossible** | The iOS Simulator requires macOS/Xcode and cannot install App Store apps anyway |
| **iPad app on an Apple Silicon Mac** | Feasible elsewhere | The App Store listing marks the app "Designed for iPad. Not verified for macOS", so it can likely be installed on an Apple Silicon Mac (for example the SkyLabMac host) from the Mac App Store's iPhone & iPad apps section. This gives a real iOS UI at iPad/phone-like window sizes, and only the user can authorize it |

For the current sprint, the help-center screenshots cover nearly every screen, so an
emulator is **not required**. It would mainly confirm motion, gestures (swipe distance,
sheet drag), and the sticky behaviour of the player while scrolling.
