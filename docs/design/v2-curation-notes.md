# V2 decision queue: curation notes (#503)

Claude reviewed all 122 open card decisions on 2026-10-03, using the constructicon-test copy of production. Each decision now has a reviewed suggestion, a confidence (high/medium/low) and a one-line evidence note. Nothing has been answered: these are only the pre-ticked boxes in the queue.

- **Source:** `scripts/data/v2c_curation_suggestions.json`.
- **Applied with:** `scripts/archive/apply_curation_suggestions.py`, which is dry-run by default and changes only the `suggested`, `suggested_reason` and `confidence` fields of open decisions.
- **For prod:** run the script after the V2 stack merges.

## The suggestions, counted

| Decision | Suggested answers |
|---|---|
| card_status (48) | done 41, paused 3, collection 3, in_use 1 |
| card_kind (68) | thing 52, project 10, collection 4, action 1, event 1 |
| card_built_for (5) | none 3, alienwhoop-f7-the-queen 1, is_event 1 |
| card_family_members (1) | The Queen + AW canopy (confirmed) |

Confidence across all 122: **53 high, 47 medium, 22 low**.

### Changes from the migration's defaults

**Kind changed to `project`:**
- compute-rc
- The Corvus Series
- KSP Walker Studies
- Fleet (Star Trek Online)
- Wallpaper Engine Theme
- HJ mini-X
- Custom GIjoe Skyhawk

**Kind changed to `collection`:**
- Drawings
- Aliens Collection
- historic old hooptiej.com
- Printable787

**Other kind changes:**
- Clodapede Lua is now **action**: per the spec, Action means code that runs on one truck.
- Arcade Warehouse Field Trip is now **event**.

**Status changes:**
- Microtech LCC is now **in_use**.
- Drawings is now **collection**, not paused.
- Aliens Collection is now **collection**, not paused.
- hooptiej.com is now **collection**, not paused.
- Konghead is now **paused**, not collection.
- Rlaarlo 787 is now **paused**, not collection.
- Slashmaro is now **paused**, not collection.

**built_for:** the three means-to-an-end cards (apc-cabin, fwd-touring-car-beta, Printable787) now suggest **none**. The migration had picked a card that only shared a hobby.

**Kept as they were, with confidence raised:**
- the AlienWhoop family (Queen + canopy);
- the canopy's built_for → The Queen;
- the 41 `done` answers, where the evidence supports them.

## Answer these by hand first (low confidence)

These 22 calls rest on thin or conflicting evidence. Each one is the least-claiming option, so check them before any bulk accept.

| Card | Question | Suggested | Why it's uncertain |
|---|---|---|---|
| Desk Build | status | done | The write-up says it was built as a birthday desk for your stepson. If it's his (gifted), In use isn't allowed. If it's the desk you work at, answer In use. |
| Radiolink AT9 | status | done | You listed the AT9 among owned/bought things, but FPV is inactive, the files stop in 2017, and the newer RC scripting targets the MT12. |
| Spyderco MicroBug | status | done | Only one photo (2017). If you still carry it, answer In use. |
| TurboGrafx 16 Restomod | status | done | Console gaming is active. The card has 2012 photos only, and the console could still be played. |
| Dreamcast Restomod | status | done | Same situation as the TurboGrafx. |
| NES restomod | status | done | Same situation as the TurboGrafx. |
| Resto Mod Gameboy | status | done | Same situation as the TurboGrafx. |
| 6x6 LeafEater APC | status | done | R/C is active. The only post-2022 files are a re-import of the 2022 files. Could still be driven. |
| M4s Interceptor | status | done | R/C is active. Nothing is newer than May 2020. |
| Cobra HOOD (WPL 6x6) | status | done | R/C is active. 3 photos from one day in 2022. |
| Camera Rover | status | done | 6 files over two weeks in 2018. |
| Wallpaper Engine Theme | status | done | It's a published Steam Workshop wallpaper, and nothing says whether you still run it. |
| Teraburst Arcade | status | done | Only a flyer and a manual, with no photo of a machine you owned. |
| Teraburst Arcade | kind | thing | If it's only reference material, collection fits better. |
| Gate of Doom Arcade | status | done | Only promo art. A 2016 note mentions a "Silkworm/Gate of doom Jamma HS1". |
| Gate of Doom Arcade | kind | thing | It may be a game board that ran in the Silkworm cabinet rather than its own machine. |
| HJ mini-X | kind | project | Every image is a render. If it was never built, it's design-only (project); if it was built, it's a thing. |
| Hellbender | kind | thing | Only 3 uncaptioned photos. |
| apc-cabin-gi-joe | kind | thing | Two downloaded STLs for a replacement APC cab, with no photo of a printed part. If this is really a repair, action fits better. |
| fwd-touring-car-beta | built_for | none | Nothing names the car these STLs were for. |
| fwd-touring-car-beta | kind | thing | It's a downloaded design, and nothing shows it was printed. |
| Printable787 | kind | collection | A downloaded kit kept to compare with the Rlaarlo 787 (a reference set). If you printed it, it's a thing. |

## Surprises and possible misfiling

### Missing or wrong hobbies

**No hobby at all** on these, though the content clearly fits one:

| Card | Likely hobby |
|---|---|
| Snow Monster | R/C Adventures |
| Aero-speed Slash | R/C Adventures |
| Rlaarlo 787 | R/C Adventures |
| Falcon 180 | The FPV Era |
| 3D printed Nova Class Starship | 3D printing |
| Folgertech Ft-i3 mega | 3D printing |
| Aliens Collection | (no obvious one) |
| Foam Mandalorian Helmet | (no obvious one) |
| Desk Build | (no obvious one) |
| Tension Biped | (no obvious one) |

**Kerbal Space Program Builds is flagged active**, but its newest file is from 2019. This is the "active hobby with nothing touched in about 2 years" mismatch.

**3D Printers and printing and Traditional Media** are already flagged as possibly stale. Traditional Media's one card, Drawings, says more pieces are to follow, so it may genuinely be active.

### Statuses not in the queue that look off

These were automatic, so they have no decision, but they're worth a look:
- **My Venza** is `in_progress`, but it's your car (bought Mar 2026), so **In use** probably fits better.
- **SteamDeck** is `in_progress`; In use probably fits better here too.

### Built-for

- **Printable787 → Rlaarlo 787:** the real relation is "kept to compare with". That's a `related` link, not built-for, and the migration didn't offer Rlaarlo as a candidate at all. Answer the built_for question with `none` and add the link by hand.
- **The 1984 GI Joe APC** has no card. apc-cabin-gi-joe is a replacement cab built for it, so create the APC card if you want that link.

### Duplicate files

- **6x6 LeafEater APC:** 9 files are byte-identical copies, re-imported on 2026-10-01.
- **QUBD 2Up:** one photo appears 3 times.

### Naming mismatches

- **FT Versawing:** the slug is `ft-spear` and the tags say "FT Spear", but the plans are FT Versa.
- **Foam Mandalorian Helmet:** the slug is `boba-fett-foam-helmet-pattern-1`.

### Bigger questions

- **AlienWhoop and TinyWhoop** holds more than the two members. Its own files include TinyWhoop flights and the AW Zer0 pilot's guides. Once it's a family, TinyWhoop and Zer0 could become member cards of their own.
- **Kerbal craft as Things:** single KSP craft (Star Destroyer, Hustler, Dolphin Submarine) are suggested as `thing` (one build). If you'd rather treat virtual builds as design-only, all three become `project`, and that's one ruling for all of them.
- **Custom GIjoe Skyhawk:** `project` follows the spec's worked example (§7.4), where the card is split into the found model (a thing) and your conversion parts. If you don't plan that split, thing is fine.

### A bug found during the check

`accept_suggested` crashes on the family question: `resolve_decisions(..., accept_suggested=True)` raises `TypeError: unhashable type: 'list'`.
- **Cause:** `core/cards.py` checks `sug not in keys` while the family suggestion is a list.
- **Scope:** this was already the case before this pass.
- **Rest of the queue:** a dry-run bulk accept of the other 121 suggestions applies cleanly.
- **Workaround:** answer the family question on its own.
