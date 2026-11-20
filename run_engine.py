"""Cerry run engine - no GUI.  Runs Python, R and SQL code that you have looked at and approved.

Python / R   a separate process, started in the workspace's outputs folder, with a time limit and a Stop.
             The environment is cleaned: variables whose names contain KEY, TOKEN, SECRET or PASSWORD are not passed on,
             so your API keys are not visible to the script.  This is NOT a sandbox - the script has your user permissions
             (it can read, change and delete your files).  Read the code before you run it.
SQL          SQLite only.  Either a temporary empty database (optionally with the workspace's CSV files loaded as tables),
             or a SQLite file you choose - opened READ-ONLY unless you allow changes.  Several statements are allowed; the
             rows of every SELECT are shown (up to a limit) and can be saved as CSV.  Time limit and Stop work too.
"""
import csv
import glob
import io
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

DEFAULTS = {"timeout": 120, "rscript": "", "python": "", "max_rows": 500, "sql_db": "", "sql_write": False,
            "sql_csv": True, "csv_max_files": 20, "csv_max_rows": 200000, "max_output": 20000}
LANGS = {"python": "py", "py": "py", "python3": "py", "r": "r", "rscript": "r", "sql": "sql", "sqlite": "sql", "sqlite3": "sql"}
EXT = {"py": ".py", "r": ".R", "sql": ".sql"}
LABEL = {"py": "Python", "r": "R", "sql": "SQL (SQLite)"}
SECRET_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")


def language_of(tag):
    return LANGS.get((tag or "").strip().lower())


def clean_env():
    env = {k: v for k, v in os.environ.items() if not any(w in k.upper() for w in SECRET_WORDS)}
    env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", MPLBACKEND="Agg")
    return env


def python_exe(configured=""):
    """The Python that runs scripts.  Cerry usually starts with pythonw.exe, which has no console output - use python.exe."""
    exe = configured or sys.executable
    p = Path(exe)
    if p.name.lower() == "pythonw.exe" and (p.with_name("python.exe")).exists():
        return str(p.with_name("python.exe"))
    return str(exe)


def find_rscript(configured=""):
    if configured and Path(configured).exists():
        return configured
    import shutil
    found = shutil.which("Rscript")
    if found:
        return found
    for pattern in (r"C:\Program Files\R\R-*\bin\Rscript.exe", r"C:\Program Files\R\R-*\bin\x64\Rscript.exe",
                    r"C:\Program Files (x86)\R\R-*\bin\Rscript.exe", "/usr/local/bin/Rscript", "/usr/bin/Rscript",
                    "/Library/Frameworks/R.framework/Resources/bin/Rscript"):
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[-1]
    return None


def snapshot(folder):
    out = {}
    for p in Path(folder).rglob("*"):
        if p.is_file() and "scripts" not in p.relative_to(folder).parts:
            try:
                st = p.stat()
                out[str(p)] = (st.st_mtime_ns, st.st_size)
            except OSError:
                pass
    return out


def clip(text, limit):
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    return text if len(text) <= limit else text[:limit] + f"\n... [{len(text) - limit} more characters cut]"


def kill_tree(proc):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def run_process(lang, code, outdir, cfg, should_stop=lambda: False):
    """Python or R.  -> result dict."""
    outdir = Path(outdir)
    scripts = outdir / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    stem = time.strftime("run_%Y%m%d_%H%M%S")
    script = scripts / (stem + EXT[lang])
    n = 2
    while script.exists():
        script = scripts / f"{stem}_{n}{EXT[lang]}"
        n += 1
    script.write_text(code, encoding="utf-8", newline="\n")
    if lang == "py":
        cmd = [python_exe(cfg.get("python", "")), str(script)]
    else:
        rscript = find_rscript(cfg.get("rscript", ""))
        if not rscript:
            return {"lang": lang, "error": "R was not found. Install R from r-project.org (then restart Cerry), or put the path of "
                    "Rscript.exe into ai_workspace_data/run.json under \"rscript\".", "script": str(script)}
        cmd = [rscript, "--vanilla", str(script)]
    before = snapshot(outdir)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    t0 = time.time()
    timeout = float(cfg.get("timeout", 120))
    try:
        proc = subprocess.Popen(cmd, cwd=str(outdir), env=clean_env(), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags)
    except OSError as e:
        return {"lang": lang, "error": f"Could not start the program: {e}", "script": str(script)}
    box = {}

    def drain(name, stream):
        box[name] = stream.read()
    threads = [threading.Thread(target=drain, args=(n_, s), daemon=True) for n_, s in (("out", proc.stdout), ("err", proc.stderr))]
    for t in threads:
        t.start()
    stopped = timed_out = False
    while proc.poll() is None:
        if should_stop():
            stopped = True
            kill_tree(proc)
            break
        if time.time() - t0 > timeout:
            timed_out = True
            kill_tree(proc)
            break
        time.sleep(0.1)
    try:
        proc.wait(timeout=5)
    except Exception:
        pass
    for t in threads:
        t.join(timeout=3)
    after = snapshot(outdir)
    new = sorted(Path(p).name for p, sig in after.items() if before.get(p) != sig)
    lim = int(cfg.get("max_output", 20000))
    return {"lang": lang, "returncode": proc.returncode, "stdout": clip(box.get("out", b""), lim), "stderr": clip(box.get("err", b""), lim),
            "timed_out": timed_out, "stopped": stopped, "new_files": new, "script": str(script), "seconds": time.time() - t0,
            "timeout": timeout}


# ---------------------------------------------------------------------- SQL
def guess_value(v):
    v = v.strip()
    if v == "":
        return None
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v.replace(",", ".")) if re.fullmatch(r"-?\d+[.,]\d+", v) else v
    except ValueError:
        return v


def table_name(path, used):
    base = re.sub(r"\W+", "_", Path(path).stem).strip("_") or "table"
    if base[0].isdigit():
        base = "t_" + base
    name, n = base, 2
    while name in used:
        name = f"{base}_{n}"
        n += 1
    used.add(name)
    return name


def load_csv_tables(conn, paths, max_files=20, max_rows=200000):
    """CSV files -> tables of an open connection.  -> [(table, file, columns, rows)]"""
    loaded, used = [], set()
    for path in list(paths)[:max_files]:
        try:
            text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|") if text.strip() else csv.excel
            rows = csv.reader(io.StringIO(text), dialect)
            header = next(rows, None)
            if not header:
                continue
            cols, seen = [], set()
            for i, h in enumerate(header):
                c = re.sub(r"\W+", "_", h.strip()).strip("_") or f"col{i + 1}"
                while c.lower() in seen:
                    c += "_"
                seen.add(c.lower())
                cols.append(c)
            name = table_name(path, used)
            conn.execute(f'CREATE TABLE "{name}" (' + ", ".join(f'"{c}"' for c in cols) + ")")
            batch, count = [], 0
            for r in rows:
                batch.append([guess_value(x) for x in (r + [""] * len(cols))[:len(cols)]])
                count += 1
                if count >= max_rows:
                    break
            conn.executemany(f'INSERT INTO "{name}" VALUES (' + ",".join("?" * len(cols)) + ")", batch)
            loaded.append((name, Path(path).name, cols, count))
        except Exception:
            continue
    return loaded


def split_statements(code):
    """Cut SQL text into statements at each ';' that really ends one (quotes, comments and triggers are respected)."""
    out, start = [], 0
    for i, ch in enumerate(code):
        if ch == ";" and sqlite3.complete_statement(code[start:i + 1]):
            out.append(code[start:i + 1].strip())
            start = i + 1
    rest = code[start:].strip()
    if rest:
        out.append(rest)
    return [s for s in out if re.sub(r"(--[^\n]*|/\*.*?\*/|\s)", "", s, flags=re.S).strip(" ;")]


def run_sql(code, cfg, csv_files=(), should_stop=lambda: False):
    t0 = time.time()
    db_path = (cfg.get("sql_db") or "").strip()
    write = bool(cfg.get("sql_write"))
    timeout = float(cfg.get("timeout", 120))
    max_rows = int(cfg.get("max_rows", 500))
    res = {"lang": "sql", "sets": [], "messages": [], "tables": [], "stopped": False, "timed_out": False, "error": ""}
    try:
        if db_path:
            if not Path(db_path).exists():
                return dict(res, error=f"The database file does not exist: {db_path}")
            conn = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode={'rw' if write else 'ro'}", uri=True)
            res["messages"].append(f"Database: {db_path} ({'changes allowed' if write else 'read-only'})")
        else:
            conn = sqlite3.connect(":memory:")
            if cfg.get("sql_csv", True) and csv_files:
                res["tables"] = load_csv_tables(conn, csv_files, int(cfg.get("csv_max_files", 20)), int(cfg.get("csv_max_rows", 200000)))
            res["messages"].append("Temporary database" + (f" with {len(res['tables'])} table(s) from CSV files" if res["tables"] else " (empty)"))
    except sqlite3.Error as e:
        return dict(res, error=f"Could not open the database: {e}")
    state = {"why": ""}

    def watchdog():
        if should_stop():
            state["why"] = "stopped"
            return 1
        if time.time() - t0 > timeout:
            state["why"] = "timeout"
            return 1
        return 0
    conn.set_progress_handler(watchdog, 20000)
    try:
        for n, stmt in enumerate(split_statements(code), 1):
            try:
                cur = conn.execute(stmt)
                if cur.description:
                    cols = [d[0] for d in cur.description]
                    rows = cur.fetchmany(max_rows + 1)
                    more = len(rows) > max_rows
                    res["sets"].append({"n": n, "sql": stmt, "columns": cols, "rows": rows[:max_rows], "more": more})
                else:
                    if cur.rowcount is not None and cur.rowcount >= 0 and re.match(r"\s*(insert|update|delete|replace)", stmt, re.I):
                        res["messages"].append(f"Statement {n}: {cur.rowcount} row(s) changed")
                    else:
                        res["messages"].append(f"Statement {n}: done")
            except sqlite3.Error as e:
                if state["why"]:
                    break
                res["error"] = f"Statement {n}: {e}\n{stmt[:200]}"
                break
        if write and db_path and not res["error"] and not state["why"]:
            conn.commit()
    finally:
        res["stopped"], res["timed_out"] = state["why"] == "stopped", state["why"] == "timeout"
        conn.close()
    res["seconds"] = time.time() - t0
    return res


def format_table(columns, rows, limit_width=60):
    def cell(v):
        s = "NULL" if v is None else str(v).replace("\n", " ")
        return s if len(s) <= limit_width else s[:limit_width - 1] + "\u2026"
    body = [[cell(v) for v in r] for r in rows]
    widths = [max([len(c)] + [len(r[i]) for r in body]) for i, c in enumerate(columns)]
    line = lambda vals: " | ".join(v.ljust(widths[i]) for i, v in enumerate(vals))
    return "\n".join([line(columns), "-+-".join("-" * w for w in widths)] + [line(r) for r in body])


def rows_to_csv(columns, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(columns)
    w.writerows(rows)
    return buf.getvalue()


def format_result(res, max_output=20000):
    """The result as readable text (also what is sent back to the model)."""
    if res.get("error") and "returncode" not in res and not res.get("sets") and not res.get("messages"):
        return "Error: " + res["error"]
    parts = []
    if res["lang"] == "sql":
        parts += res.get("messages", [])
        if res.get("tables"):
            parts.append("Tables: " + "; ".join(f"{t} ({r} rows, columns: {', '.join(c)})" for t, f, c, r in res["tables"]))
        for s in res["sets"]:
            parts.append(f"\n--- result of statement {s['n']}: {len(s['rows'])} row(s)" + (" (more rows were cut)" if s["more"] else ""))
            parts.append(format_table(s["columns"], s["rows"]))
        if res.get("timed_out"):
            parts.append("Stopped: the time limit was reached.")
        if res.get("stopped"):
            parts.append("Stopped by you.")
        if res.get("error"):
            parts.append("Error: " + res["error"])
    else:
        head = ("Timed out after %gs." % res["timeout"]) if res["timed_out"] else ("Stopped by you." if res["stopped"] else f"Exit code {res['returncode']}.")
        parts.append(head + f"  ({res['seconds']:.1f}s)")
        if res["stdout"].strip():
            parts.append("--- output ---\n" + res["stdout"].rstrip())
        if res["stderr"].strip():
            parts.append("--- errors ---\n" + res["stderr"].rstrip())
        if res["new_files"]:
            parts.append("New or changed files: " + ", ".join(res["new_files"]))
    return clip("\n".join(parts), max_output)
