"""Look-alike candidate groups from perceptual hashes (#759): the pure part, stdlib only.

No database, no file access, no third-party import: core/lookalikes.py (the service op) and
scripts/measure_lookalikes_759.py (the read-only measurement on a copy of prod) both call it, the
script inside the app image with nothing but the repo's code and the standard library.

The hash is the one the OCR pipeline already stores, `capture_events.perceptual_hash` (imagehash's
64-bit pHash as 16 hex digits, core/similarity.py compute_perceptual_hash). Nothing new is hashed.

Two images are a CANDIDATE PAIR when their hashes differ in at most MAX_DISTANCE of 64 bits, unless:
  * they are byte-identical (the same stored bytes: #615's fold, core/duplicates.py, handles them);
  * they sit in the same revision chain (#477: a v1 and its v2 are versions, never copies);
  * the owner already said "Not the same" about that exact pair (remembered per pair).
A GROUP is a connected component of the remaining pairs. Because a "Not the same" answer removes only
its pairs, a group that later gains a new look-alike comes back holding just the new member and the
items it matches (the question is only about the newcomer).
"""

MAX_DISTANCE = 6
"""Hamming distance (bits of 64) at or under which two images are a look-alike CANDIDATE. Conservative on
purpose: core/similarity.py's "similar" suggestions use 8, which also catches different shots of one
scene; a resized or recompressed copy of the same photo usually lands at 0-4, a light crop or re-export
a little higher. 6 keeps those and leaves out most merely similar pictures. Every candidate is still
shown to a person before anything is folded; scripts/measure_lookalikes_759.py prints the distance
histogram on a prod copy so the number can be set from measurement, not guesswork."""

MIN_HASH_BITS = 3
"""#587 item 2: a 64-bit hash with fewer (or, mirrored, more) set bits than this is degenerate."""


def is_degenerate_hash(hex_hash):
    """True for a missing/unparseable hash or one with near-zero entropy: at most MIN_HASH_BITS - 1
    bits set, or at most as many clear (all ones, one lone zero bit). A nearly uniform image hashes to
    such a value and would "match" every other blank-ish image. (Lives here so the stdlib-only
    measurement script can use it; core/similarity.py re-exports it.)"""
    if not hex_hash:
        return True
    try:
        bits = bin(int(hex_hash, 16)).count("1")
    except ValueError:  # silent-ok: an unparseable hash is "no hash", the documented meaning here
        return True
    return bits < MIN_HASH_BITS or (64 - bits) < MIN_HASH_BITS


def hash_int(hex_hash):
    """The hash as an int, or None when it is degenerate / unparseable / not 64 bits."""
    if is_degenerate_hash(hex_hash) or len(hex_hash) != 16:
        return None
    return int(hex_hash, 16)


def distance(a, b):
    """Hamming distance between two 64-bit hash ints."""
    return bin(a ^ b).count("1")


def pair(a, b):
    """The canonical (sorted) form of an unordered slug pair."""
    return (a, b) if a <= b else (b, a)


def chain_index(revision_pairs):
    """{slug: chain id} for every slug in a revision chain, from {old_slug: new_slug} (db.revision_pairs)."""
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for old, new in revision_pairs.items():
        ra, rb = find(old), find(new)
        if ra != rb:
            parent[ra] = rb
    return {s: find(s) for s in parent}


def _chunks(max_distance):
    """Bit ranges splitting 64 bits into max_distance + 1 slices: by the pigeonhole principle two hashes
    within max_distance bits agree exactly on at least one slice, so only hashes sharing a slice value
    are ever compared (fast at any archive size, and still exact)."""
    n = max_distance + 1
    width, extra = divmod(64, n)
    out, start = [], 0
    for i in range(n):
        w = width + (1 if i < extra else 0)
        out.append((start, (1 << w) - 1))
        start += w
    return out


def candidate_pairs(hashes, max_distance=MAX_DISTANCE):
    """{(a, b): distance} for every pair of slugs whose hashes (ints, from hash_int) are within
    `max_distance` bits. `hashes` = {slug: int}."""
    if max_distance < 0 or max_distance > 63:
        raise ValueError("max_distance must be 0..63")
    found = {}
    for shift, mask in _chunks(max_distance):
        buckets = {}
        for slug, h in hashes.items():
            buckets.setdefault((h >> shift) & mask, []).append(slug)
        for members in buckets.values():
            if len(members) < 2:
                continue
            members.sort()
            for i, a in enumerate(members):
                ha = hashes[a]
                for b in members[i + 1:]:
                    if (a, b) in found:
                        continue
                    d = distance(ha, hashes[b])
                    if d <= max_distance:
                        found[(a, b)] = d
    return found


def components(edges):
    """Connected components (each a sorted list of slugs, largest first) of an iterable of pairs."""
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    groups = {}
    for x in parent:
        groups.setdefault(find(x), []).append(x)
    return sorted((sorted(g) for g in groups.values()), key=lambda g: (-len(g), g))


def build(hashes, *, chains=None, dismissed=(), identical=None, max_distance=MAX_DISTANCE):
    """The candidate groups. `hashes` {slug: hex}; `chains` {slug: chain id} (chain_index);
    `dismissed` a set of pair() tuples the owner answered "Not the same"; `identical(a, b)` -> True when
    the two stored files are byte-identical (only asked for distance-0 pairs: identical bytes always
    hash the same).

    Returns {hashed, degenerate, pairs: {pair: distance} (the remaining ones), excluded_pairs:
    {byte_identical, revision_chain, dismissed}, groups: [{members, pairs: {pair: d}, answered:
    [pair...]}], excluded_groups: {byte_identical, revision_chain, dismissed, several}} where an
    excluded group is a group of ALL candidate pairs that vanished entirely, counted by its reason."""
    chains = chains or {}
    dismissed = set(dismissed)
    ints, degenerate = {}, 0
    for slug, h in hashes.items():
        v = hash_int(h)
        if v is None:
            degenerate += 1
        else:
            ints[slug] = v
    raw = candidate_pairs(ints, max_distance)
    keep, why_out = {}, {}
    excl = {"byte_identical": 0, "revision_chain": 0, "dismissed": 0}
    for (a, b), d in raw.items():
        reason = None
        if a in chains and chains.get(a) == chains.get(b):
            reason = "revision_chain"
        elif d == 0 and identical is not None and identical(a, b):
            reason = "byte_identical"
        elif (a, b) in dismissed:
            reason = "dismissed"
        if reason:
            excl[reason] += 1
            why_out[(a, b)] = reason
        else:
            keep[(a, b)] = d
    groups = []
    for members in components(keep):
        ms = set(members)
        groups.append({
            "members": members,
            "pairs": {p: d for p, d in keep.items() if p[0] in ms},
            "answered": sorted(p for p in dismissed if p[0] in ms and p[1] in ms and p in raw),
        })
    excluded_groups = {"byte_identical": 0, "revision_chain": 0, "dismissed": 0, "several": 0}
    kept_slugs = {s for g in groups for s in g["members"]}
    for members in components(raw):
        if any(s in kept_slugs for s in members):
            continue
        ms = set(members)
        reasons = {r for p, r in why_out.items() if p[0] in ms}
        excluded_groups[reasons.pop() if len(reasons) == 1 else "several"] += 1
    return {"hashed": len(ints), "degenerate": degenerate, "pairs": keep, "excluded_pairs": excl,
            "groups": groups, "excluded_groups": excluded_groups}
