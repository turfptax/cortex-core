"""Hand-coded ingester for the Session 0 seed artifact.

The artifact at plugins/overseer/assets/session_0_seed.md was written by
the original Opus 4.7 instance on 2026-04-17 as the inaugural compression
of the conversation that designed this overseer system. It uses the
six-section schema this overseer's tables mirror.

Faithful, hand-coded parsing — locked design says hand-coded for the seed,
not LLM round-trip. This parser:

  - Reads each section by markdown header
  - For Themes / Episodes: extracts title, body, confidence, surface_when,
    duration_label, tags from the structured block
  - For Open Questions / Patterns / Drift: each bullet becomes a row
  - Copies the "Notes for Future Overseer" section verbatim into
    future_overseer_notes, signed "first overseer" with the artifact's date

Idempotent: checks overseer_state["session_0_seeded"]; running twice does
nothing on the second call. Re-seed by setting that flag back to "0".
"""

from __future__ import annotations

import logging
import re
from pathlib import Path


log = logging.getLogger("plugin.overseer.ingest_session_0")


SEED_FLAG_KEY = "session_0_seeded"
SEED_INSTANCE_ID = "first overseer (Opus 4.7, 2026-04-17)"
SEED_RAW_JSONL = (
    "C:/dev/ttx/Cortex/First Overseer/"
    "ea69b02d-c31a-4fe9-ad63-91b334981c62.jsonl"
)


# ── Markdown helpers ────────────────────────────────────────────

_CONF_RX = re.compile(r"\[(high|med|low|medium|med-high|med-low)\]", re.I)
_TAGS_PREFIX_RX = re.compile(r"^\s*Tags?\s*:\s*", re.I)
_SURFACE_PREFIX_RX = re.compile(r"^\s*Surface\s+when\s*:\s*", re.I)
_ACTION_PREFIX_RX = re.compile(r"^\s*Action\s*:\s*", re.I)
_EPISODE_HEADER_RX = re.compile(
    r"^###\s*Episode\s+\d+\s*:\s*(?P<title>.+?)\s*"
    r"(?:\[(?P<duration>[^\]]+)\])?\s*$",
    re.I,
)
_THEME_HEADER_RX = re.compile(r"^###\s*(?P<title>.+?)\s*$")


def _extract_confidence(text: str, default: str = "med") -> str:
    m = _CONF_RX.search(text or "")
    if not m:
        return default
    return m.group(1).lower()


def _strip_inline_confidence(text: str) -> str:
    return _CONF_RX.sub("", text or "").strip()


def _parse_tags_line(line: str) -> list[str]:
    """Parse `Tags: a, b, c` → ['a', 'b', 'c']."""
    body = _TAGS_PREFIX_RX.sub("", line).strip()
    if not body:
        return []
    parts = [t.strip() for t in body.split(",")]
    return [p for p in parts if p]


def _split_top_sections(md_text: str) -> dict:
    """Split markdown by top-level `## ` headers. Returns {header_text: body_text}.

    Header text includes everything after the `## ` up to end of line.
    Body text is everything until the next `## ` or end of document.
    """
    lines = md_text.splitlines()
    sections: dict[str, list[str]] = {}
    current_header = None
    current_body: list[str] = []
    for line in lines:
        if line.startswith("## ") and not line.startswith("### "):
            if current_header is not None:
                sections[current_header] = current_body
            current_header = line[3:].strip()
            current_body = []
        else:
            if current_header is not None:
                current_body.append(line)
    if current_header is not None:
        sections[current_header] = current_body
    return {k: "\n".join(v).strip() for k, v in sections.items()}


def _split_subsections(body: str) -> list[tuple[str, str]]:
    """Split a section body by `### ` subheaders. Returns [(header, body), ...]."""
    out: list[tuple[str, str]] = []
    cur_header: str | None = None
    cur_body: list[str] = []
    for line in body.splitlines():
        if line.startswith("### "):
            if cur_header is not None:
                out.append((cur_header, "\n".join(cur_body).strip()))
            cur_header = line.rstrip()
            cur_body = []
        else:
            if cur_header is not None:
                cur_body.append(line)
    if cur_header is not None:
        out.append((cur_header, "\n".join(cur_body).strip()))
    return out


def _bullet_lines(body: str) -> list[str]:
    """Return list of raw bullet bodies (text after `- `)."""
    out: list[str] = []
    for line in body.splitlines():
        s = line.lstrip()
        if s.startswith("- "):
            out.append(s[2:].rstrip())
    return out


# ── Section-specific extractors ─────────────────────────────────

def _extract_gist(section_body: str) -> str:
    """Gist is a single paragraph. Return the first non-empty paragraph."""
    paras = [p.strip() for p in section_body.split("\n\n") if p.strip()]
    return paras[0] if paras else section_body.strip()


def _extract_theme_entry(header: str, body: str) -> dict:
    """A Theme has: title (in header, with optional [conf]), body paragraph(s),
    optional `Action:` line, optional `Tags:` line."""
    title_text = header[4:].strip() if header.startswith("### ") else header
    confidence = _extract_confidence(title_text)
    title = _strip_inline_confidence(title_text).strip()

    body_lines: list[str] = []
    tags: list[str] = []
    for raw in body.splitlines():
        if _TAGS_PREFIX_RX.match(raw):
            tags.extend(_parse_tags_line(raw))
        else:
            body_lines.append(raw)
    body_text = "\n".join(body_lines).strip()
    return {
        "title": title,
        "body": body_text,
        "confidence": confidence,
        "tags": tags,
    }


def _extract_episode_entry(header: str, body: str) -> dict:
    m = _EPISODE_HEADER_RX.match(header)
    if m:
        title = m.group("title").strip()
        duration_label = (m.group("duration") or "").strip()
    else:
        title = header[4:].strip() if header.startswith("### ") else header
        duration_label = ""

    confidence = "med"
    surface_when = ""
    tags: list[str] = []
    body_lines: list[str] = []

    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            body_lines.append("")
            continue
        if _SURFACE_PREFIX_RX.match(line):
            surface_when = _SURFACE_PREFIX_RX.sub("", line).strip()
            continue
        if _TAGS_PREFIX_RX.match(line):
            tags.extend(_parse_tags_line(line))
            continue
        # First-line confidence marker, e.g. "[high] Built the {project name}..."
        if _CONF_RX.search(line):
            conf_here = _extract_confidence(line)
            confidence = conf_here
            line = _strip_inline_confidence(line)
        body_lines.append(line)

    body_text = "\n".join(body_lines).strip()
    return {
        "title": title,
        "body": body_text,
        "duration_label": duration_label,
        "surface_when": surface_when,
        "confidence": confidence,
        "tags": tags,
    }


def _extract_question_entries(section_body: str, default_conf: str) -> list[dict]:
    out: list[dict] = []
    for line in _bullet_lines(section_body):
        line = line.strip()
        if not line:
            continue
        # Question is the leading quoted text up to the first `)` if there's
        # parenthetical commentary, otherwise the whole line. We keep the
        # whole line as `body` and pull the question itself out lightly.
        question = line
        body = ""
        # Pull out a leading quoted question if present
        m = re.match(r'^"(?P<q>[^"]+)"\s*(?P<rest>.*)$', line)
        if m:
            question = m.group("q").strip()
            body = m.group("rest").strip().lstrip("(").rstrip(")")
        out.append({
            "question": question,
            "body": body,
            "confidence": default_conf,
        })
    return out


def _extract_pattern_entries(section_body: str, default_conf: str) -> list[dict]:
    out: list[dict] = []
    for line in _bullet_lines(section_body):
        line = line.strip()
        if not line:
            continue
        # First clause (split on ;) is the short name
        first_clause = line.split(";")[0].strip()
        # Truncate at parens for a short name
        short = re.split(r"[\(]", first_clause)[0].strip()
        if len(short) > 80:
            short = short[:77] + "..."
        out.append({
            "name": short,
            "body": line,
            "confidence": default_conf,
        })
    return out


def _extract_drift_entries(section_body: str, default_conf: str) -> list[dict]:
    out: list[dict] = []
    for line in _bullet_lines(section_body):
        line = line.strip()
        if not line:
            continue
        # Direction guess: keywords near the start
        direction = ""
        lower = line.lower()
        if any(w in lower for w in ("interrupted", "stopped", "didn't repeat",
                                    "stopped recurring")):
            direction = "stopped"
        elif any(w in lower for w in ("started", "first instance", "began",
                                      "new pattern")):
            direction = "started"
        elif any(w in lower for w in ("shifted", "changed", "drift")):
            direction = "shifted"
        out.append({
            "body": line,
            "direction": direction,
            "confidence": default_conf,
        })
    return out


# ── Main entry point ───────────────────────────────────────────

def ingest_seed(db, seed_md_path: Path, *, force: bool = False) -> dict:
    """Parse the Session 0 seed and populate overseer.db.

    Args:
        db: OverseerDB instance.
        seed_md_path: path to the bundled session_0_seed.md asset.
        force: if True, ingest even when the flag is already set.

    Returns:
        dict with counts: {"already_seeded": bool, "themes": N, "episodes": N, ...}
    """
    if not force:
        flag = db.get_overseer_state(SEED_FLAG_KEY, "0")
        if flag and flag != "0":
            log.info("Session 0 already seeded (flag=%s); skipping", flag)
            return {"already_seeded": True}

    if not Path(seed_md_path).is_file():
        raise FileNotFoundError(
            "Session 0 seed not found at {}".format(seed_md_path))

    text = Path(seed_md_path).read_text(encoding="utf-8")
    sections = _split_top_sections(text)

    # raw pointer for the entire artifact
    raw_id = db.add_raw_pointer(
        source_kind="jsonl_file",
        source_path=SEED_RAW_JSONL,
        source_id="ea69b02d-c31a-4fe9-ad63-91b334981c62",
        notes="Session 0 (2026-04-16, ~18 hours, Tory + Opus 4.7).",
    )

    counts = {
        "already_seeded": False,
        "raw_pointer_id": raw_id,
        "gists": 0, "themes": 0, "episodes": 0,
        "open_questions": 0, "patterns": 0, "drift": 0,
        "future_notes": 0,
    }

    # ── Gist ────────────────────────────────────────────────
    gist_section = _find_section(sections, "Gist")
    if gist_section:
        gist_body = _extract_gist(gist_section)
        if gist_body:
            db.add_gist(
                gist_body,
                period_label="2026-04-16",
                period_start="2026-04-16",
                period_end="2026-04-17",
                confidence="high",
                raw_pointer_id=raw_id,
                tags=["session:0", "seed"],
            )
            counts["gists"] += 1

    # ── Themes ──────────────────────────────────────────────
    themes_section = _find_section(sections, "Themes")
    if themes_section:
        for header, body in _split_subsections(themes_section):
            entry = _extract_theme_entry(header, body)
            if not entry["title"]:
                continue
            db.add_theme(
                entry["title"], entry["body"],
                confidence=entry["confidence"],
                raw_pointer_id=raw_id,
                tags=["seed"] + entry["tags"],
            )
            counts["themes"] += 1

    # ── Episodes ────────────────────────────────────────────
    episodes_section = _find_section(sections, "Episodes")
    if episodes_section:
        for header, body in _split_subsections(episodes_section):
            entry = _extract_episode_entry(header, body)
            if not entry["title"]:
                continue
            db.add_episode(
                entry["title"], entry["body"],
                surface_when=entry["surface_when"],
                duration_label=entry["duration_label"],
                occurred_at="2026-04-16",
                confidence=entry["confidence"],
                raw_pointer_id=raw_id,
                tags=["seed"] + entry["tags"],
            )
            counts["episodes"] += 1

    # ── Open Questions ──────────────────────────────────────
    q_section_key = _find_section_key(sections, "Open Questions")
    if q_section_key:
        section_conf = _extract_confidence(q_section_key, default="high")
        for entry in _extract_question_entries(sections[q_section_key],
                                               section_conf):
            db.add_question(
                entry["question"], body=entry["body"],
                confidence=entry["confidence"],
                raw_pointer_id=raw_id,
                tags=["seed"], is_active=True,
            )
            counts["open_questions"] += 1

    # ── Patterns ────────────────────────────────────────────
    p_section_key = _find_section_key(sections, "Patterns")
    if p_section_key:
        section_conf = _extract_confidence(p_section_key, default="med")
        for entry in _extract_pattern_entries(sections[p_section_key],
                                              section_conf):
            db.add_pattern(
                entry["name"], entry["body"],
                confidence=entry["confidence"],
                raw_pointer_id=raw_id,
                tags=["seed"],
            )
            counts["patterns"] += 1

    # ── Drift Observations ──────────────────────────────────
    d_section_key = _find_section_key(sections, "Drift")
    if d_section_key:
        section_conf = _extract_confidence(d_section_key, default="med")
        for entry in _extract_drift_entries(sections[d_section_key],
                                            section_conf):
            db.add_drift(
                entry["body"], direction=entry["direction"],
                confidence=entry["confidence"],
                raw_pointer_id=raw_id,
                tags=["seed"],
            )
            counts["drift"] += 1

    # ── Notes for Future Overseer ───────────────────────────
    notes_section = _find_section(sections, "Notes for the Future Overseer") \
                    or _find_section(sections, "Notes for Future Overseer")
    if notes_section:
        db.append_future_note(
            instance_id=SEED_INSTANCE_ID,
            body=notes_section.strip(),
            consolidation_id=None,
        )
        counts["future_notes"] += 1

    db.set_overseer_state(SEED_FLAG_KEY, "1")
    log.info("Session 0 ingest complete: %s", counts)
    return counts


def _find_section(sections: dict, needle: str) -> str | None:
    key = _find_section_key(sections, needle)
    return sections.get(key) if key else None


def _find_section_key(sections: dict, needle: str) -> str | None:
    needle_lc = needle.lower()
    for k in sections.keys():
        if needle_lc in k.lower():
            return k
    return None
