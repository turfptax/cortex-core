"""Slice 3g checkpoint 2: drill-down tokens + detail payloads.

A token is a short opaque string that points at one row in the
overseer DB (or, in a future slice, cortex.db). Format is
`<prefix>:<id>`. The drill-down endpoint resolves a token to a rich
detail dict carrying:

  - the primary row
  - type-specific extra context (tags, evidence, sub-summaries)
  - next_tokens — links the caller can drill into next, so an AI
    session can traverse the graph without parsing free text

The point of tokens is to give AI sessions (and the Hub UI) a stable
shorthand for "look deeper at THIS thing." The working_memory artifact
gets sprinkled with these so the next call can be precisely directed.

Token prefixes (single-letter where unambiguous, multi-letter
otherwise):

  q     open_questions
  p     patterns
  d     drift_observations
  g     summaries_gist
  e     summaries_episode
  t     summaries_theme
  r     automation_rollups
  n     future_overseer_notes
  j     overseer_journal
  b     known_blindspots
  dial  dialectic_open
  nar   temporal_narratives          (added 2026-05-27, L99 must-fix #1)
  hj    human_journal_entries        (added 2026-05-27, L99 must-fix #1)
"""

import json


# ── Token model ───────────────────────────────────────────────────


class TokenError(ValueError):
    pass


_PREFIX_TO_TYPE = {
    "q":    "question",
    "p":    "pattern",
    "d":    "drift",
    "g":    "gist",
    "e":    "episode",
    "t":    "theme",
    "r":    "rollup",
    "n":    "future_note",
    "j":    "journal_entry",
    "b":    "blindspot",
    "dial": "dialectic",
    "nar":  "temporal_narrative",      # added 2026-05-27
    "hj":   "human_journal_entry",     # added 2026-05-27
    "gp":   "gist_prompt",             # added 2026-05-27 (Phase 1d)
}

_TABLE_TO_PREFIX = {
    "open_questions":         "q",
    "patterns":               "p",
    "drift_observations":     "d",
    "summaries_gist":         "g",
    "summaries_episode":      "e",
    "summaries_theme":        "t",
    "automation_rollups":     "r",
    "future_overseer_notes":  "n",
    "overseer_journal":       "j",
    "known_blindspots":       "b",
    "dialectic_open":         "dial",
    "temporal_narratives":    "nar",   # added 2026-05-27
    "human_journal_entries":  "hj",    # added 2026-05-27
    "gist_prompts":           "gp",    # added 2026-05-27 (Phase 1d)
}


def parse_token(token: str) -> tuple[str, int]:
    """Parse 'q:42' → ('q', 42). Raises TokenError on malformed input."""
    if not token or not isinstance(token, str):
        raise TokenError("token is empty")
    if ":" not in token:
        raise TokenError(
            "token must look like '<prefix>:<id>' — got: {!r}".format(token)
        )
    prefix, _, rest = token.partition(":")
    prefix = prefix.strip()
    rest = rest.strip()
    if prefix not in _PREFIX_TO_TYPE:
        raise TokenError(
            "unknown token prefix {!r}; valid prefixes: {}".format(
                prefix, ", ".join(sorted(_PREFIX_TO_TYPE)),
            )
        )
    try:
        rid = int(rest)
    except ValueError:
        raise TokenError(
            "token id must be an integer — got: {!r}".format(rest)
        )
    return prefix, rid


def make_token(table: str, row_id) -> str | None:
    """Build a token from a (table_name, row_id) pair. Returns None if
    the table isn't drill-downable."""
    pfx = _TABLE_TO_PREFIX.get(table)
    if pfx is None or row_id is None:
        return None
    return "{}:{}".format(pfx, int(row_id))


# ── Detail dispatch ───────────────────────────────────────────────


def _next(token, label, *, kind=""):
    return {"token": token, "label": label, "kind": kind}


def _truncate(s: str, n: int) -> str:
    if not s:
        return s or ""
    return s if len(s) <= n else (s[:n].rstrip() + "…")


def resolve_detail(db, token: str) -> dict:
    """Look up a token in overseer.db and return a rich detail dict.

    Caller is responsible for catching TokenError and packaging it as
    {"ok": False, "error": ...}.
    """
    prefix, rid = parse_token(token)
    fn = _RESOLVERS.get(prefix)
    if fn is None:
        raise TokenError("no resolver for prefix {!r}".format(prefix))
    payload = fn(db, rid)
    if payload is None:
        return {
            "ok": False,
            "token": token,
            "type": _PREFIX_TO_TYPE[prefix],
            "error": "not found",
        }
    payload["ok"] = True
    payload["token"] = token
    payload["type"] = _PREFIX_TO_TYPE[prefix]
    return payload


# ── Per-type resolvers ────────────────────────────────────────────
#
# Each returns the inner detail dict (without "ok"/"token"/"type" —
# those are added by resolve_detail). Returns None when the row
# doesn't exist.


def _resolve_question(db, qid):
    row = db.get_question(qid)
    if not row:
        return None
    tags = db.get_tags_for("open_questions", qid)
    evidence = db.list_evidence_for_question(qid, limit=50)
    next_tokens = []
    for ev in evidence:
        tok = make_token(ev.get("evidence_table"), ev.get("evidence_id"))
        if not tok:
            continue
        contrib = ev.get("contribution") or "evidence"
        body = ev.get("evidence_body") or ev.get("reason") or ""
        next_tokens.append(_next(
            tok,
            "{}: {}".format(contrib, _truncate(body, 80)),
            kind="evidence",
        ))
    return {
        "primary": row,
        "tags": tags,
        "context": {
            "evidence_count": len(evidence),
            "evidence": evidence,
        },
        "next_tokens": next_tokens,
    }


def _resolve_pattern(db, pid):
    row = db.get_pattern(pid)
    if not row:
        return None
    tags = db.get_tags_for("patterns", pid)
    next_tokens = []
    rp = row.get("raw_pointer_id")
    if rp:
        tok = make_token("summaries_gist", rp)
        if tok:
            next_tokens.append(_next(tok, "source gist", kind="source"))
    return {
        "primary": row,
        "tags": tags,
        "context": {},
        "next_tokens": next_tokens,
    }


def _resolve_drift(db, did):
    row = db.get_drift(did)
    if not row:
        return None
    tags = db.get_tags_for("drift_observations", did)
    next_tokens = []
    rp = row.get("raw_pointer_id")
    if rp:
        tok = make_token("summaries_gist", rp)
        if tok:
            next_tokens.append(_next(tok, "source gist", kind="source"))
    return {
        "primary": row,
        "tags": tags,
        "context": {},
        "next_tokens": next_tokens,
    }


def _resolve_gist(db, gid):
    row = db.get_gist(gid)
    if not row:
        return None
    tags = db.get_tags_for("summaries_gist", gid)
    # Find any open_questions this gist was filed against.
    filed_for = db.questions_for_evidence("summaries_gist", gid)
    next_tokens = []
    # Phase 1d (2026-05-27): expose the prompt version that produced
    # this gist so external AIs (and the refinement loop) can drill
    # from a gist into the prompt that generated it.
    pv = row.get("prompt_version_id")
    if pv:
        next_tokens.append(_next(
            "gp:{}".format(int(pv)),
            "prompt that generated this gist",
            kind="generator_prompt",
        ))
    for q in filed_for:
        tok = make_token("open_questions", q.get("id"))
        if tok:
            next_tokens.append(_next(
                tok,
                "filed against: {}".format(_truncate(q.get("question") or "", 80)),
                kind="filed_against",
            ))
    return {
        "primary": row,
        "tags": tags,
        "context": {
            "filed_for_questions": filed_for,
            "prompt_version_id": pv,
        },
        "next_tokens": next_tokens,
    }


def _resolve_episode(db, eid):
    row = db.get_episode(eid)
    if not row:
        return None
    return {
        "primary": row,
        "tags": db.get_tags_for("summaries_episode", eid),
        "context": {},
        "next_tokens": [],
    }


def _resolve_theme(db, tid):
    row = db.get_theme(tid)
    if not row:
        return None
    return {
        "primary": row,
        "tags": db.get_tags_for("summaries_theme", tid),
        "context": {},
        "next_tokens": [],
    }


def _resolve_rollup(db, rid):
    row = db.get_rollup_by_id(rid)
    if not row:
        return None
    next_tokens = []
    if row.get("gist_id"):
        tok = make_token("summaries_gist", row["gist_id"])
        if tok:
            next_tokens.append(_next(tok, "linked gist", kind="source"))
    sample_ids = row.get("sample_session_ids")
    parsed_sample = []
    if sample_ids:
        try:
            parsed_sample = json.loads(sample_ids)
        except Exception:
            parsed_sample = []
    return {
        "primary": row,
        "tags": [],
        "context": {
            "sample_session_ids": parsed_sample,
        },
        "next_tokens": next_tokens,
    }


def _resolve_future_note(db, nid):
    row = db.get_future_note(nid)
    if not row:
        return None
    return {
        "primary": row,
        "tags": [],
        "context": {},
        "next_tokens": [],
    }


def _resolve_journal_entry(db, jid):
    row = db.get_journal_entry(jid)
    if not row:
        return None
    referenced = []
    raw = row.get("referenced_artifacts")
    if raw:
        try:
            referenced = json.loads(raw)
        except Exception:
            referenced = []
    next_tokens = []
    if isinstance(referenced, list):
        for art in referenced:
            if not isinstance(art, dict):
                continue
            tbl = art.get("table") or art.get("evidence_table")
            aid = art.get("id") or art.get("evidence_id")
            tok = make_token(tbl, aid)
            if tok:
                next_tokens.append(_next(
                    tok,
                    art.get("label") or "referenced",
                    kind="reference",
                ))
    return {
        "primary": row,
        "tags": [],
        "context": {"referenced_artifacts": referenced},
        "next_tokens": next_tokens,
    }


def _resolve_blindspot(db, bid):
    row = db.get_blindspot(bid)
    if not row:
        return None
    return {
        "primary": row,
        "tags": [],
        "context": {},
        "next_tokens": [],
    }


def _resolve_dialectic(db, did):
    row = db.get_dialectic(did)
    if not row:
        return None
    next_tokens = []
    art_tbl = row.get("artifact_type")
    art_id = row.get("artifact_id")
    tok = make_token(art_tbl, art_id) if art_tbl and art_id else None
    if tok:
        next_tokens.append(_next(
            tok, "the artifact under dispute", kind="source",
        ))
    return {
        "primary": row,
        "tags": [],
        "context": {},
        "next_tokens": next_tokens,
    }


def _resolve_temporal_narrative(db, nid):
    """Added 2026-05-27 (L99 must-fix #1). Resolve a temporal narrative
    by row id — daily/weekly/monthly/yearly artifact.

    Drilling provides the full narrative body. next_tokens stay empty
    because temporal narratives don't carry artifact-level back-links
    in the schema today; the sibling narratives (same kind, adjacent
    periods) could be added later if external AIs ask for them.
    """
    rows = db._conn.execute(
        "SELECT * FROM temporal_narratives WHERE id = ?",
        (int(nid),),
    ).fetchone()
    if not rows:
        return None
    row = dict(rows)
    return {
        "primary": row,
        "tags": [],
        "context": {
            "kind": row.get("kind"),
            "period_label": row.get("period_label"),
            "period_start": row.get("period_start"),
            "period_end": row.get("period_end"),
        },
        "next_tokens": [],
    }


def _resolve_human_journal_entry(db, hid):
    """Added 2026-05-27 (L99 must-fix #1). Resolve a human journal
    entry by row id — Tory's own writing.

    Sensitivity: human journal is Tory's first-person voice. Treat
    as internal by default; callers should respect Slice 13 rules
    if a future render-time gate is added.
    """
    rows = db._conn.execute(
        "SELECT * FROM human_journal_entries WHERE id = ?",
        (int(hid),),
    ).fetchone()
    if not rows:
        return None
    row = dict(rows)
    return {
        "primary": row,
        "tags": [],
        "context": {
            "entry_type": row.get("entry_type"),
        },
        "next_tokens": [],
    }


def _resolve_gist_prompt(db, pid):
    """Added 2026-05-27 (Phase 1d). Resolve a gist_prompts row by id.

    Returns the prompt body, version label, active flag, signal counts
    (gists_generated / gists_pulled_past), and a sample of recent
    gists generated by this prompt. next_tokens drill into those
    sample gists so external AIs can audit "what kind of summaries
    did this prompt produce?"
    """
    rows = db._conn.execute(
        "SELECT * FROM gist_prompts WHERE id = ?", (int(pid),)
    ).fetchone()
    if not rows:
        return None
    row = dict(rows)
    # Sample of gists generated by this prompt (last 5).
    sample_rows = db._conn.execute(
        "SELECT id, body, period_label, created_at FROM summaries_gist "
        "WHERE prompt_version_id = ? ORDER BY id DESC LIMIT 5",
        (int(pid),),
    ).fetchall()
    samples = [dict(r) for r in sample_rows]
    next_tokens = []
    for s in samples:
        tok = make_token("summaries_gist", s["id"])
        if tok:
            next_tokens.append(_next(
                tok,
                "gist from this prompt: {}".format(
                    _truncate(s.get("body") or "", 60)),
                kind="generated_gist",
            ))
    return {
        "primary": row,
        "tags": [],
        "context": {
            "version_label": row.get("version_label"),
            "is_active": bool(row.get("is_active")),
            "gists_generated": row.get("gists_generated"),
            "gists_pulled_past": row.get("gists_pulled_past"),
            "sample_gists": samples,
        },
        "next_tokens": next_tokens,
    }


_RESOLVERS = {
    "q":    _resolve_question,
    "p":    _resolve_pattern,
    "d":    _resolve_drift,
    "g":    _resolve_gist,
    "e":    _resolve_episode,
    "t":    _resolve_theme,
    "r":    _resolve_rollup,
    "n":    _resolve_future_note,
    "j":    _resolve_journal_entry,
    "b":    _resolve_blindspot,
    "dial": _resolve_dialectic,
    "nar":  _resolve_temporal_narrative,    # added 2026-05-27
    "hj":   _resolve_human_journal_entry,   # added 2026-05-27
    "gp":   _resolve_gist_prompt,           # added 2026-05-27 (Phase 1d)
}
