import sqlite3, os, sys, shutil, datetime

src = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'data', 'apex_events.db'))
if not os.path.exists(src):
    print('Source DB not found:', src)
    sys.exit(1)

ts = datetime.datetime.now().strftime('%Y%m%d%H%M%S')
bak = src + f'.bak.{ts}'
shutil.copy2(src, bak)
print('Backup created:', bak)

con = sqlite3.connect(src)
try:
    res = list(con.execute("PRAGMA integrity_check"))
    print('integrity_check result (first 5 rows):', res[:5])
    if len(res) == 1 and res[0][0] == 'ok':
        print('DB reports OK — no recovery needed')
        sys.exit(0)

    rec = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'data', 'apex_events_recovered.db'))
    if os.path.exists(rec):
        rec = os.path.splitext(rec)[0] + f'_{ts}.db'
    print('Attempting to recover into:', rec)
    try:
        dump = '\n'.join(con.iterdump())
        with sqlite3.connect(rec) as dest:
            dest.executescript(dump)
        print('Recovery completed — recovered DB at', rec)
    except Exception as e:
        print('Recovery failed during iterdump/import:', e)
        sys.exit(2)
finally:
    con.close()

sys.exit(0)
