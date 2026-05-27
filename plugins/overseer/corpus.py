"""Cortex corpus search — substring search across the overseer's
interpretive tables with layered returns.

Extracted from the original _http_search_corpus body (Phase 1, 2026-05-27)
so the same logic can be called from BOTH the HTTP route
(`POST /plugins/overseer/search`, surfaced as the MCP `cortex_search`
tool to external AIs) AND the overseer's own chat tool palette
(`chat_tools.dispatch_tool` → `cortex_search`). Previously overseer
had per-table read tools (`get_recent_patterns`, `search_notes`,
etc.) but no unified cross-kind search — which the 24h activity-bundle
audit caught as a real asymmetry: the persona's "audit-before-claim"
rule demands fetching, but overseer had no F1 search to fetch with.

Layered returns shape per `three_layer_architecture_design_seed.md`:
  abstractions  — Layer 1 (themes, patterns, drift, questions,
                  blindspots, future_notes, journal, narratives,
                  episodes, human journal entries)
  gists         — Layer 2 (summaries_gist; each carries `raw_id`
                  when its period_label points back at an
                  imported_sessions row)
  raw_refs      — Layer 3 pointers (derived from gist period_labels;
                  one entry per unique raw_id with a fetch note)
"""
from __future__ import annotations

import logging

log = logging.getLogger("plugin.overseer.corpus")


# Map: kind_key → (table, body_columns, token_prefix, kind_label)
SEARCH_TARGETS: dict = {
    "gist":      ("summaries_gist",          ["body"],
                  "g",     "gist"),
    "theme":     ("summaries_theme",         ["title", "body"],
                  "t",     "theme"),
    "episode":   ("summaries_episode",       ["title", "body"],
                  "e",     "episode"),
    "pattern":   ("patterns",                ["name", "body"],
                  "p",     "pattern"),
    "drift":     ("drift_observations",      ["body", "direction"],
                  "d",     "drift"),
    "note":      ("future_overseer_notes",   ["body"],
                  "n",     "future_note"),
    "journal":   ("overseer_journal",        ["body"],
                  "j",     "journal_entry"),
    "narrative": ("temporal_narratives",     ["narrative"],
                  "nar",   "temporal_narrative"),
    "question":  ("open_questions",          ["question", "body"],
                  "q",     "question"),
    "blindspot": ("known_blindspots",        ["body", "rationale"],
                  "b",     "blindspot"),
    "human":     ("human_journal_entries",   ["text"],
                  "hj",    "human_journal_entry"),
}


_ABSTRACTION_KINDS = {
    "theme", "episode", "pattern", "drift", "future_note",
    "journal_entry", "temporal_narrative", "question",
    "blindspot", "human_journal_entry",
}


def _as_int(value, default, max_value=None):
    """Bounded int coercion with sane defaults."""
    try:
        n = int(value) if value is not None else default
    except Exception:
        return default
    if max_value is not None:
        n = min(n, max_value)
    return max(0, n)


def search_corpus(db, q: str, *,
                  kinds: str = "",
                  limit_per_kind: int = 5,
                  limit_total: int = 40,
                  days: int = 0,
                  surface: str = "mcp:cortex_search",
                  caller_id: str | None = None,
                  record_pulls: bool = True) -> dict:
    """Substring search across the interpretive corpus.

    See module docstring for shape + layered-returns semantics.

    Args:
      db: OverseerDB instance.
      q: substring (case-insensitive, min 2 chars enforced here).
      kinds: comma-separated subset of SEARCH_TARGETS keys (empty = all).
      limit_per_kind: cap per kind (default 5, max 50).
      limit_total: total hard cap across all kinds (default 40, max 200).
      days: restrict to artifacts within last N days (0 = no limit).
      surface: pull_event attribution tag — distinguishes
        `mcp:cortex_search` (external AIs via HTTP route) from
        `chat:cortex_search` (overseer's own chat tool call) from
        future surfaces like `vault:human-browse`.
      caller_id: free-form attribution recorded on each pull_event.
      record_pulls: log a pull_event per hit. Default True; pass False
        for read-only previews that shouldn't influence refinement-loop
        signals.

    Returns dict with: ok, query, kinds_searched, hits, abstractions,
    gists, raw_refs, total, truncated.
    """
    if not q or len(q) < 2:
        return {"ok": False, "error": "q must be at least 2 characters"}

    if kinds:
        requested = [k.strip() for k in kinds.split(",") if k.strip()]
        kinds_to_search = [k for k in requested if k in SEARCH_TARGETS]
        if not kinds_to_search:
            return {"ok": False,
                    "error": ("no recognized kinds in 'kinds' param; "
                              "valid: " + ",".join(sorted(SEARCH_TARGETS)))}
    else:
        kinds_to_search = list(SEARCH_TARGETS.keys())

    limit_per_kind = _as_int(limit_per_kind, 5, max_value=50)
    limit_total = _as_int(limit_total, 40, max_value=200)
    days = _as_int(days, 0, max_value=3650)

    hits: list = []
    like_param = f"%{q}%"
    truncated = False

    for kind_key in kinds_to_search:
        table, body_cols, prefix, kind_label = SEARCH_TARGETS[kind_key]
        where_parts = [f"{c} LIKE ? COLLATE NOCASE" for c in body_cols]
        params: list = [like_param] * len(body_cols)
        extra_where = ""
        if days:
            try:
                table_cols = {r[1] for r in db._conn.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()}
            except Exception:
                table_cols = set()
            time_col = None
            for cand in ("created_at", "written_at",
                         "observed_at", "pulled_at"):
                if cand in table_cols:
                    time_col = cand
                    break
            if time_col:
                extra_where = (f" AND {time_col} >= "
                               f"datetime('now', ?)")
                params.append(f"-{int(days)} days")
        sql = (
            f"SELECT * FROM {table} WHERE ("
            + " OR ".join(where_parts) + ")"
            + extra_where
            + " ORDER BY id DESC LIMIT ?"
        )
        params.append(int(limit_per_kind))
        try:
            rows = db._conn.execute(sql, params).fetchall()
        except Exception as e:
            log.warning("search target %s failed: %s", table, e)
            continue
        for r in rows:
            row = dict(r)
            snippet = ""
            ql = q.lower()
            for c in body_cols:
                val = (row.get(c) or "")
                if not val:
                    continue
                idx = val.lower().find(ql)
                if idx == -1:
                    continue
                start = max(0, idx - 80)
                end = min(len(val), idx + len(q) + 120)
                snippet = ("…" if start > 0 else "") + \
                    val[start:end] + \
                    ("…" if end < len(val) else "")
                break
            if not snippet:
                for c in body_cols:
                    v = row.get(c) or ""
                    if v:
                        snippet = v[:200] + ("…" if len(v) > 200 else "")
                        break
            token = None
            if prefix:
                token = "{}:{}".format(prefix, row.get("id"))
            created_at = (row.get("created_at")
                          or row.get("written_at")
                          or row.get("observed_at")
                          or "")
            extras = {}
            for cand in ("period_label", "kind", "confidence",
                         "name", "instance_id", "direction"):
                if row.get(cand):
                    extras[cand] = row[cand]
            hits.append({
                "token": token,
                "kind": kind_label,
                "artifact_table": table,
                "artifact_id": row.get("id"),
                "snippet": snippet,
                "created_at": created_at,
                "extras": extras,
            })
            if len(hits) >= limit_total:
                truncated = True
                break
        if truncated:
            break

    # Record pull_events (best-effort; never raises).
    if record_pulls and hits:
        for h in hits:
            try:
                db.record_pull_event(
                    artifact_table=h["artifact_table"],
                    artifact_id=h["artifact_id"],
                    surface=surface,
                    query_text=q,
                    caller_id=caller_id,
                )
            except Exception as e:
                log.warning("record_pull_event failed: %s", e)

    # Layered returns per three_layer_architecture_design_seed.md.
    abstractions: list = []
    gists_out: list = []
    raw_refs: list = []
    seen_raw_ids: set = set()
    for h in hits:
        kind = h.get("kind", "")
        if kind == "gist":
            period_label = (h.get("extras") or {}).get(
                "period_label") or ""
            raw_id = period_label if ":" in period_label else None
            gist_out = dict(h)
            if raw_id:
                gist_out["raw_id"] = raw_id
                if raw_id not in seen_raw_ids:
                    raw_refs.append({
                        "raw_id": raw_id,
                        "linked_gist_token": h.get("token"),
                        "note": ("Layer 3 raw source. Drill via "
                                 "imported_sessions; Slice 13 "
                                 "sensitivity rules apply at "
                                 "fetch time."),
                    })
                    seen_raw_ids.add(raw_id)
            gists_out.append(gist_out)
        elif kind in _ABSTRACTION_KINDS:
            abstractions.append(h)
        else:
            # Unknown kind — degrade to abstractions so nothing
            # disappears from the layered view.
            abstractions.append(h)

    return {
        "ok": True,
        "query": q,
        "kinds_searched": kinds_to_search,
        "hits": hits,
        "abstractions": abstractions,
        "gists": gists_out,
        "raw_refs": raw_refs,
        "total": len(hits),
        "truncated": truncated,
    }
