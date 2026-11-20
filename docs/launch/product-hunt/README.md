# Product Hunt launch kit

Everything needed to submit localplaud to Product Hunt. All screenshots and the
demo recording use the synthetic library from `scripts/seed_demo.py`; no real
recordings appear.

## Assets

| Field | File | Notes |
| --- | --- | --- |
| Thumbnail | `thumbnail-240.png` (`thumbnail-480.png` for retina) | 240×240 |
| Gallery 1 | `gallery/1-hero.png` | First image is the social preview |
| Gallery 2–7 | `gallery/2-speakers.png` … `gallery/7-compare.png` | 2540×1520 (2× of 1270×760) |
| Gallery GIF | `demo.gif` | 2.3 MB, 26 s walkthrough |
| Video | `demo.mp4` | Product Hunt only accepts a **YouTube** link: upload this first, then paste the URL |

Recommended gallery order: video, 1-hero, 2-speakers, 7-compare, 3-notes,
5-ask, 4-mindmap, 6-mobile.

## Listing copy

**Name:** localplaud

**Tagline** (≤60 characters):

> Self-hosted transcripts and speaker ID for Plaud recordings

Alternatives:

> Your Plaud recordings, transcribed on your own hardware
> Plaud Intelligence features, self-hosted and minute-free

**Description** (≤260 characters):

> Keep your Plaud recorder, drop the subscription. localplaud syncs raw audio and
> runs Whisper, speaker diarization, notes, mind maps, and cited Ask on your own
> machine. No minute caps, editable speakers, open source (MIT). Not affiliated
> with Plaud.

**Links:** Website <https://skyhong2002.github.io/localplaud/> (live demo at
`/demo/`) · Source <https://github.com/skyhong2002/localplaud>

**Topics:** Open Source · Productivity · Artificial Intelligence · Note-taking ·
Meetings · Privacy

**Pricing:** Free (open source)

## Maker's first comment

> Hi Product Hunt 👋 I'm Sky, the maker of localplaud.
>
> I love my Plaud recorder, but not what happens after upload. Speakers get
> merged or split, names have to be re-entered for every recording, and renaming
> a speaker means regenerating the summary and spending more of a monthly minute
> allowance. Plaud's own help center says it doesn't automatically identify
> speakers other than you.
>
> localplaud keeps the recorder and the official app, and takes over at the raw
> audio. It runs everything else on hardware you control:
>
> • Whisper large-v3-turbo plus pyannote diarization, with speakers you can rename
>   and fix inline
> • Optional local voiceprints that recognize the same person across recordings
> • Notes from your own templates with full-transcript coverage, plus mind maps
> • Ask across your whole library, with citations that jump to the moment in the
>   audio
> • A Web App that works on desktop and phone, plus exports to TXT, SRT, VTT,
>   DOCX, PDF, and Markdown
>
> It runs on Apple Silicon, NVIDIA, or CPU. Each stage can use a local model, your
> own worker, or a cloud provider you choose. Nothing is sent anywhere you didn't
> pick. No minute caps, MIT licensed.
>
> Honest caveats: it's self-hosted, so setup takes a terminal and a few
> installs. Cross-recording voice ID needs extra setup. Taiwan Mandarin and mixed
> Mandarin/English are what I test hardest.
>
> Try the read-only demo at https://skyhong2002.github.io/localplaud/demo/ first.
> I'd love to know where setup gets stuck and which Plaud workflow you'd want next.
> If anything in the comparison table is wrong, tell me and I'll fix it today.

## Launch-day checklist

- [ ] Product name decided (the name contains a third-party trademark; see below)
- [ ] `demo.mp4` uploaded to YouTube (unlisted is fine) and the link added
- [ ] Schedule for 00:01 PT (16:01 Taiwan time during PDT, 15:01 during PST)
- [ ] Post the maker comment immediately after the launch goes live
- [ ] Share with people who would genuinely use it; ask for feedback, not upvotes
      (Product Hunt penalizes vote solicitation)
- [ ] Cross-post: Show HN, r/selfhosted, r/PlaudNoteUsers (check each
      subreddit's self-promotion rules first)
- [ ] Watch GitHub Issues and Discussions all day; answer setup questions fast
- [ ] Re-check competitor prices in the comparison the day before launch

## Risks to keep in mind

- **Trademark:** "localplaud" contains "Plaud". Every surface says the project is
  independent and not affiliated with Plaud, but a rename before launch avoids
  takedown and confusion risk.
- **Comparison claims:** sourced from public pages as of November 2026. Plaud's
  v4 app speaker behavior was not verified. Correct errors quickly and publicly.
- **Expectations:** cross-recording voice identification is an optional service.
  Don't present it as out-of-the-box.

## Website and live demo

The site (landing page plus a read-only snapshot of the real Web App) is built
from `website/` and the synthetic demo library, then published to the
`gh-pages` branch:

```bash
LOCALPLAUD_API__ACCOUNTS_ENABLED=false KEEP=1 scripts/serve_demo.sh 8792
scripts/build_pages.sh /tmp/site && scripts/deploy_pages.sh /tmp/site
```

The demo answers requests from recorded responses through a service worker.
Every write returns a "read-only demo" notice, and nothing reaches a server.

## Regenerating the assets

Seed and serve the demo library, then re-capture:

```bash
python scripts/seed_demo.py --out /tmp/lp-tools/demo --force --short-audio
LOCALPLAUD_API__ACCOUNTS_ENABLED=false scripts/serve_demo.sh 8791
```

The screenshots are composed with Playwright into framed layouts at 2× scale;
keep crops inside the workspace column so text stays legible.
