# Chimera architecture notes — a cross-session brief

**Not part of Constructicon.** This file has nothing to do with the app in this
repo. It's a personal note, stashed here because this repo is public and the
owner wanted a way to hand a write-up to whatever Claude session he's working
with at his job, on a separate tool called **Chimera** — without needing to
re-explain the whole thing from scratch. If you're a Claude session reading
this because he pointed you at it: hello, this is the context.

Chimera correlates IT-support screenshots against ticket/client/chat data.
The owner and a Claude session at work iterated on its architecture for the
better part of a day; this is a rubber-duck writeup of that same problem,
worked through independently in another Claude session (this one), which
landed on a fairly different framing than "throw a bigger local model at it."
Worth comparing notes.

## What Chimera actually does today (the working part)

A scheduled Claude Code action — not a web app, not a script hitting the raw
API — runs a defined correlation procedure against real business tools:

1. Look for a ticket number in the screenshot(s).
2. If none is obvious, hunt by client name or recent tickets to find one.
3. If a machine name turns up instead, resolve it to a client, then check
   that client's tickets.
4. Once *any* anchor exists (ticket, client, or machine), check the
   relevant Slack channel for chatter/comments related to it.
5. Iterate steps 1-4 until something anchors, or give up — no anchor means
   it never becomes a tracked "issue."
6. Once anchored, explore everything — ticketing system, documentation
   system, Slack, the RMM — and produce a diagnosis: is this a real
   problem? If so, is the ticket already closed, or does it need
   investigating?

This works, and has caught real things the owner missed himself (sometimes
a knowledge gap, sometimes just something noticed in the background). The
problem isn't the logic — it's that it only runs locally, one desktop, one
person, and it's slow enough that it's clearly burning real tokens per run.

## What's been tried to scale it, and why each attempt failed

**Move the same logic into a shared web app, others upload into it.**
Blocked on "how do we do this without putting a live Claude API key into a
web app strangers/coworkers feed images into" — cost, exposure, and not
wanting to host/manage an agent loop server-side all factored in, not
cleanly separated at first.

**Self-host a local model (Ollama) instead of calling Claude.** Consistently
failed — "a forest of errors," sessions not scoping correctly, either the
model chews unboundedly and fills server RAM, or the output quality is
worse than plain Tesseract OCR alone. Narrowing Ollama's job down to just
"find the ticket number" — the simplest-seeming sub-task — *still* failed.

**Diagnosis of the Ollama failure** (from this session, not yet run past
work-Claude): the mistake isn't scope, it's using a model at all for that
piece. Ticket numbers are pattern-matching, not reasoning — regex against
OCR text, once you have a parser per ticketing system in play (multiple
systems, inconsistent zero-padding, messy URLs — several known shapes, not
one fuzzy pattern). Client names are the same shape of problem but fuzzier:
nicknames/slang/abbreviations don't need vision or an LLM either, they need
a maintained alias table plus fuzzy text matching (e.g. `rapidfuzz`)
against Tesseract's OCR output — fuzzy matching also absorbs a good chunk
of OCR noise for free, which a raw-image vision model doesn't buy you over
text-only OCR for this specific sub-problem (the ambiguity is linguistic —
does the model know "Cats-r-us" means a specific client — not visual).

**The actual goal, once stated plainly, wasn't per-user live inference —
it was a bigger shared corpus.** The owner wants coworkers uploading too so
there's more data to catch things any one person might miss. That reframes
the whole architecture: you don't need to scale *Claude doing the
correlation* across users, you need to scale *the dataset being matched
against*. A shared embedding index (one vector per past ticket's
problem/solution text, pooled across the whole team) grows for free with
every upload and needs zero live model inference to search — this repo
(Constructicon) already has exactly this pattern built and running:
`core/similarity.py`, OCR text -> `sentence-transformers`
(`all-MiniLM-L6-v2`) embedding -> cosine similarity, currently used for
"related items." Same shape, different corpus.

**Who/what produces the vector matters, and "Claude pre-vectorizes it"
isn't quite right** — Anthropic doesn't have an embeddings endpoint. Either
run a small local embedding model (same one above, ~80MB, no API key, one
deterministic forward pass — recommended, given the goal is avoiding a
live model in the shared path) or use Voyage AI (Anthropic's embeddings
partner, a hosted call). Whichever is chosen, **every contributor's vector
must come from the exact same model and version** — mixing embedding
spaces doesn't error, it just silently returns nonsense matches.

## The harder question: what if the correlation logic itself needs to run centrally?

Once the owner described the actual target UI/workflow — SSO login (already
exists at work), a two-pane screen (recent uploads by user on the left, a
shared live "issues" table on the right, grouped much like this repo's own
Projects), users dropping in piles of images that get sorted into issues,
and the iterative hunt-then-diagnose loop described above running against
shared company tools (ticketing, docs, Slack, RMM) — that's not a
deterministic problem anymore. It's the same real agent Chimera already
runs locally, just needing to be centrally hosted and shared-state instead
of one desktop's cron job.

For *that* piece specifically (not the ticket-ID/client-name extraction,
which stays deterministic per above), the concrete engineering options:

- **Self-hosted loop, BYOK-per-user**: users provide their own Anthropic
  API key, your backend calls the Messages API via the Tool Runner helper
  (handles the tool-call loop for you) using that user's key. You still
  own and run the loop; this is the closest to what's already
  half-working, just with a real model instead of Ollama swapped in — the
  RAM/session-scoping mess was Ollama's own failure mode, not inherent to
  this shape.
- **Managed Agents** (Anthropic's hosted-agent surface): define one Agent
  config once (model, system prompt, tool definitions for ticket
  search/client lookup/Slack search/RMM/docs), then create a **Session**
  per upload batch with the images mounted as that session's input files.
  Anthropic runs the actual loop and hosts the sandbox where tool calls
  execute — no self-hosted agent loop, no model-hosting/RAM problem at
  all, which is exactly the class of thing that broke the Ollama attempt.
  Credentials for the business tools can be scoped per-session via a vault
  credential if per-user attribution matters; billing can still sit on one
  org-level key while usage is tracked per session/user via metadata.

Given the actual described workflow is real branching tool-use judgment
(not something to flatten into regex/embeddings), Managed Agents reads as
the better fit *specifically because* it removes the operational failure
mode Ollama kept hitting, while keeping genuine model reasoning where it's
actually earning its keep.

SSO, the two-pane UI, and the shared "issues" table are ordinary web-app
concerns underneath all of this — nothing Claude-specific about building
those, they just read/write whatever the agent (or the deterministic
extraction step) already produced.

## One concrete thing this session actually shipped, worth cross-pollinating

While comparing notes on OCR quality, this session rebuilt Constructicon's
own pre-OCR image preprocessing (`core/ocr.py`, issue #237/PR #238):
grayscale -> invert if the image is mean-dark (catches light-text-on-dark
UI screenshots) -> `autocontrast` only, deliberately no fixed contrast
multiplier (a first attempt with one actively hallucinated garbage on
already-clean images) -> resize only at the extremes (upscale under
~700px, downscale over ~2000px, leave everything else untouched). Verified
against real screenshots: clear accuracy wins on dark-mode UI captures, no
regression on already-clean images, and a 6x speed win on a huge phone
photo with no quality cost. If Chimera's OCR path was tuned independently
and landed somewhere different, it's worth comparing the two — one of them
is probably missing a case the other one caught.
