"""Unified note-saving helper — writes to file + DB in one call."""

import os
from datetime import datetime


def save_note(text, notes_dir, db, source="voice", note_type="note",
              session_id=None, tags="", project=""):
    """Save a note to both a text file and the Cortex DB.

    Returns (file_path, row_id) on success, (None, None) on empty input.
    """
    text = text.strip()
    if not text:
        return None, None

    # Write to file
    file_path = None
    os.makedirs(notes_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(notes_dir, f"{stamp}.txt")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        file_path = path
    except OSError:
        pass

    # Write to DB
    row_id = None
    try:
        row_id = db.insert_note(
            content=text,
            source=source,
            note_type=note_type,
            session_id=session_id,
            tags=tags,
            project=project,
        )
    except Exception:
        pass

    return file_path, row_id
