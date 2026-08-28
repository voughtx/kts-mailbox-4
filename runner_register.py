#!/usr/bin/env python3
"""TokenHive v3.2 — WORKER (kts-mailbox-2..-5) — 1 token/run.

Trigger: repository_dispatch "register" (hub dispatch karta hai).
Flow: inbox se 1 fresh token ATOMIC claim → 1 UNUSED Indian identity (10k
names.txt) se register → JWT milte hi privacy settings OFF (4 settings) →
JWT → PRIVATE repo (Jts-Brain) tokenhive/tokens_YYYY-MM-DD.txt → identity
used_ids.txt me mark. No token = clean exit.

Uniqueness: used_ids.txt (hub) — same info same run me NAHI, alag repos me
NAHI (422 race pe 3x auto-retry naye identity se).
"""
import json, os, random, re, string, time, urllib.request, urllib.error, base64

GH_PAT = os.environ.get("GH_PAT", "")
HUB_REPO = (os.environ.get("HUB_REPO", "") or "voughtx/kts-mailbox").strip()
PRIVATE_REPO = os.environ.get("PRIVATE_REPO", "voughtx/Jts-Brain")
SIBLINGS = [s.strip() for s in os.environ.get("SIBLINGS", "").split(",") if s.strip()]
RUNNER_ID = os.environ.get("RUNNER_ID", "kts-mailbox")
NAMES_URL = os.environ.get("NAMES_URL", f"https://raw.githubusercontent.com/{HUB_REPO}/main/names.txt")
TOKEN = os.environ.get("TOKEN", "")          # manual dispatch me optional fresh token
MAX_AGE = 240                                 # 4 min fresh turnstile
GAP_PRIVACY_RETRY = 20                        # s
ID_RETRY = 3                                  # 422 pe naye identity se tries
WAVE_SETTLE_S = 75                            # siblings ko claim hone ka waqt
LOCK_FILE = "runner_lock.txt"
LOCK_TTL = 900
UA = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 Chrome/126 Mobile Safari/537.36"
HDRS = {"User-Agent": UA, "Content-Type": "application/json",
        "Origin": "https://kartoons.me", "Referer": "https://kartoons.me/",
        "X-Skip-Challenge": "true"}


def api(extra=None):
    h = {"Authorization": "token " + GH_PAT, "User-Agent": "kts-runner",
         "Accept": "application/vnd.github+json"}
    if extra: h.update(extra)
    return h


def gh_get(repo, path):
    rq = urllib.request.Request(f"https://api.github.com/repos/{repo}/contents/{path}",
                                headers=api())
    try:
        with urllib.request.urlopen(rq, timeout=20) as r:
            d = json.loads(r.read().decode())
            return d.get("sha"), base64.b64decode(d["content"]).decode(errors="ignore")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, ""
        raise


def gh_put(repo, path, content, sha=None):
    data = {"message": "tokenhive update", "content": base64.b64encode(content.encode()).decode()}
    if sha: data["sha"] = sha
    rq = urllib.request.Request(f"https://api.github.com/repos/{repo}/contents/{path}",
                                data=json.dumps(data).encode(), method="PUT",
                                headers=api({"Content-Type": "application/json"}))
    try:
        with urllib.request.urlopen(rq, timeout=20) as r:
            return r.status in (200, 201)
    except urllib.error.HTTPError as e:
        if e.code in (409, 423):
            return False
        raise


def gh_append(repo, path, record, header_if_new=""):
    """atomic append (sha-conditional, 6 retry)."""
    for _ in range(6):
        try:
            sha, old = gh_get(repo, path)
        except Exception:
            sha, old = None, ""
        new = (old.rstrip("\n") + "\n" + record if old.strip() else (header_if_new + record).lstrip("\n"))
        if gh_put(repo, path, new.rstrip("\n") + "\n", sha):
            return True
        time.sleep(1.2 + random.random())
    return False


# ---------------- inbox: token claim ----------------

def fresh_entries(content):
    now = time.time()
    out = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line[:12]:
            ep_s, tok = line.split(":", 1)
            if ep_s.isdigit() and now - int(ep_s) <= MAX_AGE:
                out.append((int(ep_s), tok))
        else:
            out.append((int(now), line))
    out.sort(reverse=True)
    return out


def inbox_lines(content):
    return [l for l in content.splitlines() if l.strip() and not l.strip().startswith("#")]


def claim_1(tok_override=None):
    if tok_override:
        return tok_override
    for _ in range(10):
        sha, content = gh_get(HUB_REPO, "inbox.txt")
        entries = fresh_entries(content)
        if not entries:
            return None
        want = entries[0][1]
        keep = [l for l in content.splitlines()
                if l.strip() and l.strip() not in ("#" + l.strip())
                and not (l.strip().endswith(want) or l.strip() == want)]
        # expired lines bhi clean karte jaao (v3.1 jaisa)
        new_c = "\n".join(keep) + "\n"
        if gh_put(HUB_REPO, "inbox.txt", new_c, sha):
            return want
        time.sleep(1.2 + random.random())
    return None


def inbox_fresh_count():
    try:
        _, content = gh_get(HUB_REPO, "inbox.txt")
        return len(fresh_entries(content))
    except Exception:
        return 0


def wave_marker():
    line = "# wave " + time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + " by " + RUNNER_ID
    sha, content = gh_get(HUB_REPO, "inbox.txt")
    new_c = content.rstrip("\n") + "\n" + line + "\n"
    return gh_put(HUB_REPO, "inbox.txt", new_c, sha)


# ---------------- names: 10k Indian identities ----------------

_NAMES = []

def load_names():
    global _NAMES
    if _NAMES:
        return _NAMES
    try:
        rq = urllib.request.Request(NAMES_URL, headers={"User-Agent": "kts-runner"})
        content = urllib.request.urlopen(rq, timeout=25).read().decode(errors="ignore")
    except Exception as e:
        print("names load err:", str(e)[:80], flush=True)
        return []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("S.No.") or line.startswith("="):
            continue
        parts = [p.strip() for p in line.split("\t")]
        if len(parts) >= 6:
            uname, full = parts[4], parts[1]
        elif len(parts) == 1 and "@" in line:
            uname = line.split("@")[0]
            full = " ".join(w.capitalize() for w in uname.replace(".", " ").replace("_", " ").split()[:2]) or uname
        else:
            continue
        if re.fullmatch(r"[a-zA-Z][a-zA-Z0-9._-]{2,}", uname):
            uname = uname.replace(".", "_")
            _NAMES.append((uname, full))
    return _NAMES


def used_ids():
    try:
        _, c = gh_get(HUB_REPO, "used_ids.txt")
        return set(l.strip() for l in c.splitlines() if l.strip())
    except Exception:
        return set()


def pick_identity(used):
    names = load_names()
    fresh = [n for n in names if n[0] not in used]
    if not fresh:
        return None
    return random.choice(fresh)


def mark_used(uname):
    gh_append(HUB_REPO, "used_ids.txt", uname)


# ---------------- register + privacy + save ----------------

def register(tok, uname, full_name):
    password = "P@ss" + "".join(random.choices(string.ascii_letters + string.digits, k=14)) + "!"
    email = f"{uname}@gmail.com"
    body = json.dumps({"username": uname, "password": password,
                       "email": email, "turnstile_token": tok}).encode()
    rq = urllib.request.Request("https://api.kartoons.me/api/auth/register",
                                data=body, method="POST", headers=HDRS)
    try:
        with urllib.request.urlopen(rq, timeout=30) as resp:
            d = json.loads(resp.read().decode())
            dd = d.get("data") or {}
            jwt = dd.get("access_token") or dd.get("token") or dd.get("jwt")
            return {"ok": True, "username": uname, "full_name": full_name,
                    "password": password, "email": email,
                    "jwt": jwt or "", "status": resp.status}, None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.read().decode()[:300]}"


def prep_privacy(jwt):
    """4 settings OFF (KTS _prep_privacy wala exact body).
    Returns: 'ok' | 'banned' | 'pending'"""
    body = json.dumps({"watchlist_public": False, "activity_public": False,
                       "currently_watching_public": False, "watch_time_public": False}).encode()
    for attempt in range(2):
        try:
            rq = urllib.request.Request("https://api.kartoons.me/api/user/privacy",
                                        data=body, method="PUT",
                                        headers={**HDRS, "Authorization": "Bearer " + jwt})
            with urllib.request.urlopen(rq, timeout=20) as r:
                b = r.read().decode()[:200]
                if r.status == 200 and '"success":true' in b.replace(" ", ""):
                    return "ok"
                low = b.lower()
                if r.status in (401, 403) and ("banned" in low or "scraping" in low):
                    return "banned"
        except urllib.error.HTTPError as e:
            b = e.read().decode()[:200]
            low = b.lower()
            if e.code in (401, 403) and ("banned" in low or "scraping" in low):
                return "banned"
            print(f"  privacy HTTP {e.code} (attempt {attempt+1})", flush=True)
        except Exception as e:
            print("  privacy err:", str(e)[:60], flush=True)
        if attempt == 0:
            time.sleep(GAP_PRIVACY_RETRY)
    return "pending"


def save_token(acc, privacy_state):
    datefile = "tokenhive/tokens_" + time.strftime("%Y-%m-%d", time.gmtime()) + ".txt"
    rec = (f"\n===\nusername: {acc['username']}\nfull_name: {acc['full_name']}\n"
           f"password: {acc['password']}\nemail: {acc['email']}\n"
           f"jwt: {acc['jwt']}\nprivacy: {privacy_state}\n"
           f"source: {RUNNER_ID}\n")
    return gh_append(PRIVATE_REPO, datefile, rec)


def update_status():
    try:
        datefile = "tokenhive/tokens_" + time.strftime("%Y-%m-%d", time.gmtime()) + ".txt"
        sha, today = gh_get(PRIVATE_REPO, datefile)
        n = today.count("username:")
        gh_put(PRIVATE_REPO, "tokenhive/runner_status.txt",
               f"last_run: {time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime())}\n"
               f"runner: {RUNNER_ID}\ntoday_jwts: {n}", None)
    except Exception as e:
        print("status fail:", str(e)[:60], flush=True)


# ---------------- lock (hub only) ----------------

def acquire_lock():
    run_id = os.environ.get("GITHUB_RUN_ID", str(os.getpid()))
    for _ in range(3):
        try:
            sha, content = gh_get(HUB_REPO, LOCK_FILE)
            held = False
            if content.strip():
                try:
                    if float(content.strip().split("|")[0]) > time.time():
                        held = True
                except Exception:
                    pass
            if held:
                print("[lock] doosra hub run active — exit", flush=True)
                return False
            if gh_put(HUB_REPO, LOCK_FILE, f"{time.time() + LOCK_TTL}|{run_id}|{time.time()}", sha):
                return True
        except Exception as e:
            print("lock err:", str(e)[:60], flush=True)
        time.sleep(5)
    return False


def release_lock():
    try:
        sha, _ = gh_get(HUB_REPO, LOCK_FILE)
        if sha:
            gh_put(HUB_REPO, LOCK_FILE, "", sha)
    except Exception:
        pass


# ---------------- main ----------------

def do_register_cycle(tok):
    """1 token + 1 identity → register → privacy → save → used. Returns True on success."""
    used = used_ids()
    for attempt in range(ID_RETRY):
        ident = pick_identity(used)
        if not ident:
            print("names exhausted", flush=True)
            return False
        uname, full = ident
        used.add(uname)
        print(f"try {attempt+1}/{ID_RETRY}: {uname} ({full})...", flush=True)
        acc, err = register(tok, uname, full)
        if err or not acc or not acc.get("jwt"):
            print("  register fail:", (err or "no jwt")[:160], flush=True)
            if err and "422" in err:
                continue          # naye identity se retry (username collision)
            return False
        priv = prep_privacy(acc["jwt"])
        print(f"  ✅ {uname} | privacy: {priv}", flush=True)
        if priv == "banned":
            print("  ❌ token banned register-time — save NAHI hoga", flush=True)
            mark_used(uname)
            return False
        if save_token(acc, priv):
            mark_used(uname)
            update_status()
            return True
        print("  ⚠ save fail — token log me hai (manually save karna)", flush=True)
        print("  DATA: " + json.dumps({k: acc[k] for k in ("username", "password", "email", "jwt", "full_name")}, flush=True))
        return False
    return False


def dispatch(repo):
    try:
        data = json.dumps({"event_type": "register"}).encode()
        rq = urllib.request.Request(f"https://api.github.com/repos/{repo}/dispatches",
                                    data=data, method="POST",
                                    headers=api({"Content-Type": "application/json"}))
        with urllib.request.urlopen(rq, timeout=20) as r:
            return r.status == 204
    except Exception:
        return False


def main():
    print(f"WORKER {RUNNER_ID} | IP:", urllib.request.urlopen("https://api.ipify.org", timeout=10).read().decode().strip(), flush=True)

    if TOKEN:
        # manual mode
        do_register_cycle(TOKEN)
        return

    tok = claim_1()
    if not tok:
        print("no fresh token in inbox — done", flush=True)
        return
    print("claimed 1 token", flush=True)
    do_register_cycle(tok)
    print("done", flush=True)
