"""Read-only comparison of legacy and bounded comment listing allocations.

Run each mode in a fresh process: py check_comment_memory.py legacy|optimized
Does not start background jobs or invoke platform writes.
"""
import json
import sys
import time
import tracemalloc

from app import app
from db import get_conn
from scrm import _union, _comment_frag, _comment_threads, _with_platform, _load_contents


def main(mode):
    if mode not in ('legacy', 'optimized'):
        raise SystemExit('mode must be legacy or optimized')
    tracemalloc.start()
    started = time.monotonic()
    if mode == 'optimized':
        with app.test_client() as client:
            response = client.get('/scrm/api/comments?size=20')
            if response.status_code != 200:
                raise RuntimeError(f'HTTP {response.status_code}')
            result = response.get_json()
    else:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f'SELECT src_table,src_pk FROM {_union()}')
                matched = {(r['src_table'], r['src_pk']) for r in cur.fetchall()}
                sources = sorted({t for t, _ in matched})
                union = '(' + ' UNION ALL '.join(_comment_frag(t) for t in sources) + ') U'
                cur.execute(f'SELECT * FROM {union}')
                threads = _comment_threads(cur.fetchall(), matched)
                rows = _with_platform([r for thread in threads[:20] for r in thread])
                _load_contents(cur, rows)
                result = {'total': len(threads), 'rows': rows}
                # Include response serialization in both measurements.
                with app.app_context():
                    app.json.response(result).get_data()
    _, peak = tracemalloc.get_traced_memory()
    print(json.dumps({'mode': mode, 'seconds_with_tracemalloc': round(time.monotonic()-started, 3),
                      'python_peak_mb': round(peak / 1024**2, 3), 'total_threads': result['total'],
                      'returned_rows': len(result['rows']),
                      'page_ids': [(r['src_table'], r['src_pk']) for r in result['rows']]}, ensure_ascii=False))


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'optimized')
