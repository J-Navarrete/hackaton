"""One-off: import existing outputs/verdicts/*.json into the SQLite DB."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from web.db import session_scope, _ensure_engine
from web.persist import insert_claim_with_verdict, insert_skipped_claim, replace_claims, upsert_video


VERDICTS_DIR = Path(__file__).parent / "outputs" / "verdicts"


def backfill_one(verdicts_path: Path) -> None:
    data = json.loads(verdicts_path.read_text(encoding="utf-8"))
    video_meta = data["video"]
    verdicts = data.get("verdicts", [])
    skipped = data.get("skipped_claims", [])

    with session_scope() as session:
        v = upsert_video(session, video_meta.get("webpage_url") or "", video_meta)
        replace_claims(session, v.id)

        for verdict_record in verdicts:
            claim_dict = {
                "id": verdict_record.get("id"),
                "claim": verdict_record.get("claim"),
                "speaker": verdict_record.get("speaker"),
                "t_start": verdict_record.get("t_start"),
                "t_end": verdict_record.get("t_end"),
                "claim_type": verdict_record.get("claim_type"),
            }
            insert_claim_with_verdict(session, v.id, claim_dict, verdict_record)

        for sk in skipped:
            insert_skipped_claim(session, v.id, sk)

        session.commit()
        print(f"  {v.id}: {len(verdicts)} veredictos + {len(skipped)} omitidos -> {v.title[:80]}")


def main():
    _ensure_engine()
    files = sorted(VERDICTS_DIR.glob("*.json"))
    print(f"Backfilling {len(files)} videos desde {VERDICTS_DIR}")
    for f in files:
        try:
            backfill_one(f)
        except Exception as e:
            print(f"  [SKIP] {f.name}: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
