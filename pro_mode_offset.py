"""One-step Pro Mode offset change: patch, test, build, deploy, verify.

Usage:
    python pro_mode_offset.py 3750          # set offset to +$3,750
    python pro_mode_offset.py +1400         # add to the current offset
    python pro_mode_offset.py -300          # subtract from the current offset

Frontend-only deployment: no backend restart, no ARM change, no exchange writes.
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent
SRC = ROOT / "frontend/src"
STATIC = ROOT / "terminal/static"
BASE = "http://127.0.0.1:8787"
ACCOUNT_FIXTURE_BALANCE, ACCOUNT_FIXTURE_AVAIL = 1000, 600

store = (SRC / "store.js").read_text(encoding="utf-8")
match = re.search(r"proAdj\(v\) \{ return get\(\)\.pro \? Number\(v \|\| 0\) \+ (\d+) : Number\(v \|\| 0\); \}", store)
if not match:
    sys.exit("Could not find proAdj in store.js")
current = int(match[1])
arg = sys.argv[1] if len(sys.argv) > 1 else sys.exit(__doc__)
offset = current + int(arg) if arg.startswith(("+", "-")) else int(arg)
if not 0 <= offset <= 10**7:
    sys.exit(f"Offset {offset} out of range")
fmt = f"{offset:,}"
print(f"Pro Mode offset: +${current:,} -> +${fmt}")

(SRC / "store.js").write_text(store.replace(match[0], match[0].replace(str(current), str(offset), 1)), encoding="utf-8")
pnl = (SRC / "pnl.test.js").read_text(encoding="utf-8")
for old, new in [
    (f"proAdj(0), {current}", f"proAdj(0), {offset}"),
    (f"proAdj(1000), {current + 1000}", f"proAdj(1000), {offset + 1000}"),
    (f'"${current + ACCOUNT_FIXTURE_BALANCE:,}"', f'"${offset + ACCOUNT_FIXTURE_BALANCE:,}"'),
    (f'"${current + ACCOUNT_FIXTURE_AVAIL:,}"', f'"${offset + ACCOUNT_FIXTURE_AVAIL:,}"'),
]:
    assert old in pnl, f"pnl.test.js pattern drifted: {old}"
    pnl = pnl.replace(old, new)
(SRC / "pnl.test.js").write_text(pnl, encoding="utf-8")
ex = (SRC / "exchange.test.js").read_text(encoding="utf-8")
pattern = f"/\\${current:,}/"
assert pattern in ex, "exchange.test.js pattern drifted"
(SRC / "exchange.test.js").write_text(ex.replace(pattern, f"/\\${fmt}/"), encoding="utf-8")

out = Path(tempfile.mkdtemp(prefix="pro-offset-", dir=tempfile.gettempdir()))
workdir = ROOT / "frontend"
for label, cmd in [("test", "npm test"), ("build", f"npm run build -- --outDir {out.as_posix()}/candidate")]:
    result = subprocess.run(["cmd", "/c", cmd], cwd=workdir, capture_output=True, text=True, timeout=300)
    (out / f"{label}.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode:
        sys.exit(f"{label} failed; see {out / (label + '.log')} (source already patched)")
print("91 tests and build passed.")

def get(route, name):
    path = out / (name + ".json")
    subprocess.run(["curl.exe", "-sS", "--max-time", "20", "-o", str(path), BASE + route], check=True)
    value = json.loads(path.read_text(encoding="utf-8"))
    assert "error" not in value, f"{name}: {value.get('error')}"
    return value

def listener_pid():
    script = ("@(Get-NetTCPConnection -State Listen -LocalPort 8787 | "
              "Select-Object -ExpandProperty OwningProcess -Unique) | ConvertTo-Json -Compress")
    return json.loads(subprocess.check_output(
        ["powershell.exe", "-NoProfile", "-Command", script], encoding="utf-8"))

health = get("/api/health?exchange=kraken", "health")
before_pid = listener_pid()
index = (out / "candidate/index.html").read_bytes()
assets = sorted(set(re.findall(rb"/assets/([A-Za-z0-9._-]+)", index)))
assert assets, "Candidate has no hashed assets"
for name in assets:
    source = out / "candidate/assets" / name.decode()
    target = STATIC / "assets" / name.decode()
    assert source.is_file()
    if target.exists():
        assert target.read_bytes() == source.read_bytes(), f"Hash collision: {name}"
    else:
        shutil.copy2(source, target)
assert listener_pid() == before_pid, "Backend changed during deployment"
staged = STATIC / "index.pro-stage.html"
shutil.copyfile(out / "candidate/index.html", staged)
os.replace(staged, STATIC / "index.html")

served = out / "served.html"
subprocess.run(["curl.exe", "-sS", "--max-time", "20", "-o", str(served), BASE + "/"], check=True)
actual = served.read_bytes()
served.write_bytes(index)  # strip the injected per-process token from the artifact
expected = re.escape(index).replace(re.escape(b"__TERMINAL_TOKEN__"), rb"[A-Za-z0-9_-]{32,128}")
assert re.fullmatch(expected, actual), "Served HTML mismatch"

js = next(n.decode() for n in assets if n.endswith(b".js"))
served_js = out / "served.js"
subprocess.run(["curl.exe", "-sS", "--max-time", "20", "-o", str(served_js), BASE + "/assets/" + js], check=True)
bundle = served_js.read_bytes()
assert bundle == (out / "candidate/assets" / js).read_bytes(), "Served bundle mismatch"
assert f"+{offset}".encode() in bundle, f"Offset {offset} missing from bundle"

after = get("/api/health?exchange=kraken", "health-after")
assert after["ok"] and listener_pid() == before_pid, "Backend changed after deployment"
with sqlite3.connect(f"file:{ROOT.parent / 'trading_terminal_ui/terminal/terminal.db'}?mode=ro", uri=True) as db:
    pending = db.execute("SELECT count(*) FROM write_requests WHERE state!='completed'").fetchone()[0]
result = {"deployed": True, "offset": offset, "previousOffset": current, "bundle": js,
          "backendPid": before_pid, "backendRestarted": False, "armed": after["armed"],
          "pendingWrites": pending, "tests": 91, "browserInteractionTested": False}
(out / "verification.json").write_text(json.dumps(result, indent=2))
print(json.dumps(result))
