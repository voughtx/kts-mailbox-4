#!/usr/bin/env python3
"""KTS PARALLEL RUNNER — claim-based (safe across 5 repos).
Har worker: central inbox se 1 fresh token ATOMIC claim karta hai
(inbox se hata ke = locked). 409-conflict pe retry. Same token 2
repo kabhi use nahi karenge. Result central outbox + status."""
import json, os, random, string, time, urllib.request, urllib.error, base64

TOKEN = os.environ.get("TOKEN", "")          # manual mode
GH_PAT = os.environ.get("GH_PAT", "")
HUB_REPO = os.environ.get("HUB_REPO", "voughtx/kts-mailbox")
PRIVATE_REPO = os.environ.get("PRIVATE_REPO", "voughtx/Jts-Brain")  # JWT yahan (private) save
SIBLINGS = os.environ.get("SIBLINGS", "")    # comma list — sirf hub mode
RUNNER_ID = os.environ.get("RUNNER_ID", "runner")
MAX_AGE = 240
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
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    rq = urllib.request.Request(url, headers=api())
    try:
        with urllib.request.urlopen(rq, timeout=20) as r:
            d = json.loads(r.read().decode())
            return d.get("sha"), base64.b64decode(d["content"]).decode(errors="ignore")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, ""
        raise

def gh_put(repo, path, content, sha=None):
    data = {"message": "update", "content": base64.b64encode(content.encode()).decode()}
    if sha: data["sha"] = sha
    rq = urllib.request.Request(f"https://api.github.com/repos/{repo}/contents/{path}",
                                data=json.dumps(data).encode(), method="PUT",
                                headers=api({"Content-Type": "application/json"}))
    try:
        with urllib.request.urlopen(rq, timeout=20) as r:
            return True
    except urllib.error.HTTPError as e:
        if e.code == 409:
            return False   # conflict — retry fresh state
        raise

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

def fresh_entries(content):
    now = time.time()
    out = []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" in line[:12]:
            ep_s, tok = line.split(":", 1)
            if ep_s.isdigit() and now - int(ep_s) <= MAX_AGE:
                out.append((int(ep_s), tok))
        else:
            out.append((int(now), line))
    out.sort(reverse=True)   # naya pehle
    return out

BATCH = int(os.environ.get("BATCH", "6"))   # ek run me kitne tokens tak

def claim_many():
    """Batch claim: inbox se max BATCH fresh tokens atomic hata ke lao.
    Jo pehle hata lega, wahi use karega — same token 2 repo kabhi nahi."""
    for _ in range(10):
        sha, content = gh_get(HUB_REPO, "inbox.txt")
        entries = fresh_entries(content)
        if not entries:
            return []
        take = entries[:BATCH]
        remove_toks = [t for _, t in take]
        keep = [l for l in content.splitlines() if l.strip()
                and not any(t in l for t in remove_toks)]
        new_c = "\n".join(keep) + ("\n" if keep else "")
        if gh_put(HUB_REPO, "inbox.txt", new_c, sha):
            return [t for _, t in take]
        time.sleep(1.2)
    return []

def register(tok):
    username = "u" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    password = "P@ss" + "".join(random.choices(string.ascii_lowercase + string.digits, k=10)) + "!"
    email = f"{username}@mailinator.com"
    body = json.dumps({"username": username, "password": password,
                       "email": email, "turnstile_token": tok}).encode()
    rq = urllib.request.Request("https://api.kartoons.me/api/auth/register",
                                data=body, method="POST", headers=HDRS)
    try:
        with urllib.request.urlopen(rq, timeout=30) as resp:
            d = json.loads(resp.read().decode())
            dd = d.get("data") or {}
            jwt = dd.get("access_token") or dd.get("token") or dd.get("jwt")
            return {"ok": True, "username": username, "password": password,
                    "email": email, "jwt": jwt or ""}, None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.read().decode()[:150]}"

def append_outbox(acc):
    """Naya JWT PRIVATE repo me date-wise file me save (kabhi public nahi).
    File: tokenhive/tokens_YYYY-MM-DD.txt (har din alag file)."""
    rec = f"\n===\nusername: {acc['username']}\npassword: {acc['password']}\nemail: {acc['email']}\njwt: {acc['jwt']}\nsource: {RUNNER_ID}\n"
    datefile = "tokenhive/tokens_" + time.strftime("%Y-%m-%d", time.gmtime()) + ".txt"
    for _ in range(5):
        try:
            sha, old = gh_get(PRIVATE_REPO, datefile)
        except Exception:
            sha, old = None, ""
        if gh_put(PRIVATE_REPO, datefile, (old + rec).strip() + "\n", sha):
            return True
        time.sleep(1.2)
    return False

def update_status():
    try:
        # status bhi private repo me (public me count bhi mat dikhao)
        statusfile = "tokenhive/runner_status.txt"
        _, oc = gh_get(PRIVATE_REPO, statusfile)
        n = oc.count("username:")
        text = f"last_run: {time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime())}\ntotal_jwts: {n}"
        for _ in range(4):
            sha, _ = gh_get(PRIVATE_REPO, statusfile)
            if gh_put(PRIVATE_REPO, statusfile, text, sha):
                break
            time.sleep(1)
    except Exception as e:
        print("status fail:", e, flush=True)

def main():
    print("runner:", RUNNER_ID, "| IP:", urllib.request.urlopen("https://api.ipify.org", timeout=10).read().decode(), flush=True)

    if TOKEN:
        acc, err = register(TOKEN)
        if err or not acc or not acc.get("jwt"):
            print("FAIL:", err or "no jwt", flush=True)
            raise SystemExit(1)
        print("✅", acc["username"], flush=True)
        append_outbox(acc)
        update_status()
        return

    # HUB mode: SAB workers dispatch (claim system conflict handle karta hai)
    if SIBLINGS:
        for s in [x.strip() for x in SIBLINGS.split(",") if x.strip()]:
            try:
                if dispatch(s):
                    print("dispatched", s, flush=True)
                else:
                    print("dispatch fail", s, flush=True)
            except Exception as e:
                print("dispatch fail", s, e, flush=True)
        time.sleep(3)  # workers ko claim karne ka time

    toks = claim_many()
    if not toks:
        print("no fresh token — done", flush=True)
        return
    print("claimed", len(toks), "tokens — batch me register kar raha hoon", flush=True)
    made = 0
    for tok in toks:
        acc, err = register(tok)
        if err or not acc or not acc.get("jwt"):
            print("  FAIL:", (err or "no jwt")[:100], flush=True)
            continue
        print("  ✅", acc["username"], flush=True)
        append_outbox(acc)
        made += 1
        time.sleep(1.5)
    update_status()
    print(f"batch done: {made}/{len(toks)} JWT", flush=True)

if __name__ == "__main__":
    main()
