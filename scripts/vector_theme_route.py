"""Vector theme routing: link unreachable gists into the abstraction
graph by meaning (F1 coverage push, 2026-06-11).

The looper's keyword passes (looper:kw-route:v1-v4) drove coverage
11.8% -> 42.3%. This is the vector successor: each theme's title+body
is embedded locally and its semantic neighbors over ALL gists become
theme_gists links. Themes only - evidence_for_question rows carry
semantic judgments (supports/complicates/reframes) a cosine score
cannot honestly make; those stay with the overseer's own judgment.

Provenance: linked_by='desktop:vec-route:v1', relevance='vec:<sim>'.

Run ON the Pi (needs llama-embed on :8082 + sqlite-vec):
    sudo python3 vector_theme_route.py            # dry run
    sudo python3 vector_theme_route.py --commit   # write links

Idempotent: existing (theme_id, gist_id) pairs are skipped.
"""
import json
import sqlite3
import struct
import sys
import urllib.request

DB = "/home/turfptax/cortex-core/plugins/overseer/data/overseer.db"
EMBED_URL = "http://127.0.0.1:8082/v1/embeddings"
MIN_SIM = 0.62
MAX_LINKS_PER_THEME = 60
KNN_K = 200
LINKED_BY = "desktop:vec-route:v1"


def embed(texts):
    req = urllib.request.Request(
        EMBED_URL,
        data=json.dumps({"input": [t[:1600] for t in texts]}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    out = [None] * len(texts)
    for item in data["data"]:
        out[item["index"]] = item["embedding"]
    return out


def main():
    commit = "--commit" in sys.argv
    db = sqlite3.connect(DB)
    db.enable_load_extension(True)
    import sqlite_vec
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.row_factory = sqlite3.Row

    themes = db.execute(
        "SELECT id, title, body FROM summaries_theme").fetchall()
    existing = {(r["theme_id"], r["gist_id"]) for r in db.execute(
        "SELECT theme_id, gist_id FROM theme_gists")}
    print("themes: {} | existing links: {}".format(
        len(themes), len(existing)))

    anchors = ["{}. {}".format(t["title"] or "", t["body"] or "")
               for t in themes]
    vecs = embed(anchors)

    total_new = 0
    for theme, vec in zip(themes, vecs):
        if vec is None:
            continue
        blob = struct.pack("{}f".format(len(vec)), *vec)
        hits = db.execute(
            "SELECT gist_id, distance FROM vec_gists "
            "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (blob, KNN_K)).fetchall()
        added = 0
        samples = []
        for h in hits:
            if added >= MAX_LINKS_PER_THEME:
                break
            sim = 1.0 - h["distance"]
            if sim < MIN_SIM:
                break  # ordered by distance; rest are below floor
            pair = (theme["id"], h["gist_id"])
            if pair in existing:
                continue
            if commit:
                db.execute(
                    "INSERT INTO theme_gists (theme_id, gist_id, "
                    "relevance, linked_by) VALUES (?, ?, ?, ?)",
                    (theme["id"], h["gist_id"],
                     "vec:{:.2f}".format(sim), LINKED_BY))
            existing.add(pair)
            added += 1
            if len(samples) < 2:
                g = db.execute(
                    "SELECT body FROM summaries_gist WHERE id = ?",
                    (h["gist_id"],)).fetchone()
                samples.append("    g:{} sim={:.2f} {}".format(
                    h["gist_id"], sim,
                    (g["body"] or "")[:80].encode(
                        "ascii", "replace").decode()))
        total_new += added
        if added:
            print("t:{} '{}' +{} links".format(
                theme["id"],
                (theme["title"] or "")[:40].encode(
                    "ascii", "replace").decode(),
                added))
            for s in samples:
                print(s)
    if commit:
        db.commit()
    print("{}: {} new links".format(
        "COMMITTED" if commit else "DRY RUN", total_new))


if __name__ == "__main__":
    main()
