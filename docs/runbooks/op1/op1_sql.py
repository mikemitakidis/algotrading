"""OP1 fallback when the sqlite3 command-line tool is not installed on the VPS (installing it would be a write).

    python3 -I op1_sql.py backup <database> <copy>     read-only online backup (mode=ro; immutable=1 only for a
                                                       WAL-format file without a -wal file)
    python3 -I op1_sql.py run <copy> <file.sql>        run a report file read-only (.print lines are echoed)
    python3 -I op1_sql.py check <copy>                 PRAGMA quick_check on the copy (read-only)

Standard library only. The source database is opened read-only; ``run`` opens the COPY read-only too.
"""
import os
import sqlite3
import sys


def _wal_format(path):
    with open(path, "rb") as fh:
        head = fh.read(20)
    return len(head) == 20 and head[18] == 2


def _uri(path):
    p = os.path.realpath(path).replace("%", "%25").replace("?", "%3f").replace("#", "%23")
    immutable = _wal_format(path) and not os.path.exists(path + "-wal")
    return "file:%s?mode=ro%s" % (p, "&immutable=1" if immutable else "")


def backup(src, dst):
    s = sqlite3.connect(_uri(src), uri=True)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


def run(db, sql_path):
    con = sqlite3.connect(_uri(db), uri=True)
    buf = []
    for line in open(sql_path, encoding="utf-8"):
        st = line.strip()
        if st.startswith(".print"):
            print(st[len(".print"):].strip().strip("'"))
            continue
        if st.startswith("--") or not st:
            continue
        buf.append(line)
        if st.endswith(";"):
            q = "".join(buf)
            buf = []
            try:
                cur = con.execute(q)
                rows = cur.fetchall()
                if cur.description:
                    print("\t".join(c[0] for c in cur.description))
                    for r in rows:
                        print("\t".join("" if v is None else str(v) for v in r))
            except sqlite3.Error as exc:
                print("Error: %s" % exc)
    con.close()


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) == 3 and a[0] == "backup":
        backup(a[1], a[2])
    elif len(a) == 3 and a[0] == "run":
        run(a[1], a[2])
    elif len(a) == 2 and a[0] == "check":
        c = sqlite3.connect(_uri(a[1]), uri=True)
        print(c.execute("PRAGMA quick_check").fetchone()[0])
        c.close()
    else:
        print(__doc__)
        sys.exit(2)
