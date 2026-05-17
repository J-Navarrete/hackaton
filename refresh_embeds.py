"""One-off: regenerate embed_html for all videos with the latest build_embed_html().

Safe to run while server is stopped. Does NOT touch claims, verdicts or votes.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from web.db import session_scope, _ensure_engine, Video
from web.persist import build_embed_html


def main():
    _ensure_engine()
    with session_scope() as s:
        videos = s.query(Video).all()
        for v in videos:
            v.embed_html = build_embed_html(v)
            print(f'  {v.id} [{v.platform}] -> {("embed" if v.embed_html else "fallback")}')
        s.commit()
    print(f'\nRefreshed {len(videos)} videos.')


if __name__ == "__main__":
    main()
