#!/usr/bin/env python3
"""Helper for the senel-e2e workflow.

Subcommands cover UI automation (uiautomator dump + input tap), the public
SMS Gateway cloud API, webhook.site, Doze control and results.json bookkeeping.
Everything is stdlib-only so it runs on a bare GitHub runner.
"""
import argparse
import base64
import glob
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

OUT = os.path.abspath(os.environ.get("E2E_OUT", "e2e-out"))
# Credentials live outside the uploaded artifact directory.
STATE_FILE = os.path.abspath(
    os.environ.get("E2E_STATE", os.path.join(OUT, "..", "e2e-state.json"))
)
RESULTS_FILE = os.path.join(OUT, "results.json")
UI_DIR = os.path.join(OUT, "ui")
PKG = "me.capcom.smsgateway"
API = "https://api.sms-gate.app/3rdparty/v1"
WEBHOOK_SITE = "https://webhook.site"
TO_NUMBER = "+4512345678"
UA = "senel-e2e-test/1.0"


# ---------------------------------------------------------------- utilities

def log(msg):
    # stderr, so command substitution in run.sh only captures the real output
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", file=sys.stderr, flush=True)


def sh(cmd, timeout=60):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           errors="replace")
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def adb(*args, timeout=60):
    return sh(["adb", *args], timeout)


def adb_shell(cmd, timeout=60):
    _, out, _ = adb("shell", cmd, timeout=timeout)
    return out.strip()


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def state():
    return load_json(STATE_FILE, {})


def state_set(**kw):
    s = state()
    s.update(kw)
    save_json(STATE_FILE, s)


def record(test, **fields):
    res = load_json(RESULTS_FILE, {})
    res.setdefault(test, {}).update(fields)
    save_json(RESULTS_FILE, res)


def parse_value(v):
    try:
        return json.loads(v)
    except Exception:
        return v


def iso_to_ts(s):
    if not s:
        return None
    try:
        s = re.sub(r"\.(\d{6})\d+", r".\1", s.replace("Z", "+00:00"))
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def whsite_to_ts(s):
    # webhook.site created_at is "YYYY-MM-DD HH:MM:SS" in UTC
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return None


# ---------------------------------------------------------------- HTTP

def http(method, url, body=None, auth=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if auth:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        req.add_header("Authorization", "Basic " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode(errors="replace")
            try:
                return r.status, json.loads(raw) if raw.strip() else None
            except ValueError:
                return r.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except Exception as e:  # network error, timeout
        return 0, str(e)


def creds():
    s = state()
    return s["username"], s["password"]


def api(method, path, body=None, timeout=30):
    return http(method, API + path, body, creds(), timeout)


def wh_requests():
    uuid = state().get("wh_uuid")
    if not uuid:
        return []
    st, data = http("GET", f"{WEBHOOK_SITE}/token/{uuid}/requests?sorting=newest&per_page=100")
    if st != 200 or not isinstance(data, dict):
        return []
    return data.get("data") or []


def wh_find(event, needle=None, after_ts=None):
    for r in wh_requests():
        content = r.get("content") or ""
        try:
            body = json.loads(content)
        except ValueError:
            continue
        if body.get("event") != event:
            continue
        if needle and needle not in content:
            continue
        created = whsite_to_ts(r.get("created_at"))
        if after_ts and created and created < after_ts - 2:
            continue
        return r, body
    return None, None


# ---------------------------------------------------------------- UI

def next_seq():
    os.makedirs(UI_DIR, exist_ok=True)
    path = os.path.join(UI_DIR, ".seq")
    n = int(load_json(path, 0)) + 1
    save_json(path, n)
    return n


def screenshot(path):
    with open(path, "wb") as f:
        try:
            subprocess.run(["adb", "exec-out", "screencap", "-p"], stdout=f, timeout=30)
        except subprocess.TimeoutExpired:
            pass


def dump(name):
    """Dump the UI hierarchy and take a screenshot. Returns the XML root or None."""
    base = os.path.join(UI_DIR, f"{next_seq():03d}-{name}")
    xml = None
    for _ in range(4):
        adb_shell("rm -f /sdcard/window_dump.xml")
        adb("shell", "uiautomator dump /sdcard/window_dump.xml", timeout=45)
        _, txt, _ = adb("exec-out", "cat /sdcard/window_dump.xml", timeout=30)
        if "<hierarchy" in txt:
            xml = txt[txt.index("<"):]
            break
        time.sleep(2)
    screenshot(base + ".png")
    if xml is None:
        log(f"ui dump '{name}' failed")
        return None
    with open(base + ".xml", "w") as f:
        f.write(xml)
    try:
        return ET.fromstring(xml.encode("utf-8"))
    except ET.ParseError as e:
        log(f"ui dump '{name}' parse error: {e}")
        return None


def match(n, rid=None, text=None, contains=False):
    if rid:
        node_id = n.get("resource-id") or ""
        if node_id not in (rid, f"{PKG}:id/{rid}"):
            return False
    if text is not None:
        cands = [(n.get("text") or "").strip().lower(), (n.get("content-desc") or "").strip().lower()]
        t = text.lower()
        if contains:
            if not any(t in c for c in cands):
                return False
        elif t not in cands:
            return False
    return True


def find(root, rid=None, text=None, contains=False):
    if root is None:
        return None
    for n in root.iter("node"):
        if match(n, rid, text, contains):
            return n
    return None


def center(n):
    x1, y1, x2, y2 = map(int, re.findall(r"-?\d+", n.get("bounds")))
    return (x1 + x2) // 2, (y1 + y2) // 2


def dismiss_anr(root):
    """Tap 'Wait' on a system 'isn't responding' dialog if one blocks the screen."""
    n = find(root, rid="android:id/aerr_wait") or find(root, text="Wait")
    if n is not None and find(root, text="isn't responding", contains=True) is not None:
        x, y = center(n)
        log(f"dismissing ANR dialog at {x},{y}")
        adb_shell(f"input tap {x} {y}")
        time.sleep(2)
        return True
    return False


def ui_find(name, rid=None, text=None, contains=False, timeout=20):
    deadline = time.time() + timeout
    root = None
    while True:
        root = dump(name)
        if root is not None and dismiss_anr(root):
            continue
        n = find(root, rid, text, contains)
        if n is not None or time.time() > deadline:
            return root, n
        time.sleep(2)


def ui_tap(name, rid=None, text=None, contains=False, timeout=20):
    _, n = ui_find(name, rid, text, contains, timeout)
    if n is None:
        log(f"tap '{name}': element not found (id={rid} text={text})")
        return False
    x, y = center(n)
    adb_shell(f"input tap {x} {y}")
    log(f"tap '{name}': id={n.get('resource-id')} text={n.get('text')!r} at {x},{y}")
    time.sleep(1.5)
    return True


def ui_wait_text(name, rid, reject=(), pattern=None, timeout=60):
    """Wait until the node's text is non-empty, not rejected and matches pattern."""
    deadline = time.time() + timeout
    while True:
        root = dump(name)
        if root is not None and dismiss_anr(root):
            continue
        n = find(root, rid=rid)
        txt = (n.get("text") or "").strip() if n is not None else ""
        ok = bool(txt) and txt.lower() not in [r.lower() for r in reject]
        if ok and pattern:
            ok = re.fullmatch(pattern, txt) is not None
        if ok:
            return txt
        if time.time() > deadline:
            return None
        time.sleep(3)


# ---------------------------------------------------------------- logcat

def logcat_text():
    parts = []
    for p in sorted(glob.glob(os.path.join(OUT, "logcat-*.txt"))):
        with open(p, errors="replace") as f:
            parts.append(f.read())
    return "\n".join(parts)


def logcat_after_marker(marker):
    txt = logcat_text()
    tag = f"E2E-MARK {marker}"
    i = txt.rfind(tag)
    return txt[i:] if i >= 0 else txt


def mark(marker):
    adb_shell(f"log -t E2E {shlex.quote('E2E-MARK ' + marker)}")


def grep_lines(text, pattern, limit=40):
    rx = re.compile(pattern)
    return [l for l in text.splitlines() if rx.search(l)][:limit]


# ---------------------------------------------------------------- Doze

def deep_state():
    return adb_shell("dumpsys deviceidle get deep")


def doze_enter(whitelist):
    adb_shell("dumpsys deviceidle enable all")
    adb_shell(f"dumpsys deviceidle whitelist {'+' if whitelist else '-'}{PKG}")
    adb_shell("dumpsys battery unplug")
    adb_shell("input keyevent 223")
    time.sleep(3)
    force_out = adb_shell("dumpsys deviceidle force-idle")
    time.sleep(1)
    deep = deep_state()
    light = adb_shell("dumpsys deviceidle get light")
    wl = adb_shell("dumpsys deviceidle whitelist")
    wake = grep_lines(adb_shell("dumpsys power"), r"mWakefulness=", 1)
    info = {
        "force_idle_output": force_out,
        "deep_state": deep,
        "light_state": light,
        "app_whitelisted": PKG in wl,
        "wakefulness": wake[0].strip() if wake else None,
    }
    log(f"doze enter: {info}")
    return info


def doze_exit():
    adb_shell("dumpsys deviceidle unforce")
    adb_shell("dumpsys battery reset")
    adb_shell("input keyevent 224")
    time.sleep(1)
    adb_shell("wm dismiss-keyguard")
    log(f"doze exit: deep={deep_state()}")


def doze_check(info):
    deep = deep_state()
    info.setdefault("deep_samples", 0)
    info["deep_samples"] += 1
    if deep != "IDLE":
        info["reasserts"] = info.get("reasserts", 0) + 1
        info.setdefault("non_idle_states_seen", []).append(deep)
        log(f"doze left IDLE (state={deep}), re-forcing")
        adb_shell("dumpsys battery unplug")
        adb_shell("input keyevent 223")
        adb_shell("dumpsys deviceidle force-idle")
    else:
        info["idle_samples"] = info.get("idle_samples", 0) + 1


# ---------------------------------------------------------------- commands

def cmd_snap(a):
    dump(a.name)


def cmd_tap(a):
    ok = ui_tap(a.name, a.id, a.text, a.contains, a.timeout)
    sys.exit(0 if ok else 1)


def cmd_checked(a):
    """Print checked state of a switch; optionally tap until it matches --want."""
    for attempt in range(3):
        _, n = ui_find(a.name, rid=a.id, timeout=a.timeout)
        if n is None:
            print("missing")
            sys.exit(1)
        checked = n.get("checked") == "true"
        if a.want is None or checked == (a.want == "true"):
            print("true" if checked else "false")
            sys.exit(0)
        x, y = center(n)
        adb_shell(f"input tap {x} {y}")
        log(f"toggled {a.id} at {x},{y}")
        time.sleep(2)
    print("mismatch")
    sys.exit(1)


def cmd_type(a):
    if not ui_tap(a.name, rid=a.id, timeout=a.timeout):
        sys.exit(1)
    value = a.value if a.value is not None else state().get(a.state_key, "")
    adb_shell(f"input text {shlex.quote(value)}")
    time.sleep(1)


def cmd_read_registration(a):
    """Wait for the cloud credentials to appear on the home screen and store them."""
    user = ui_wait_text(f"{a.prefix}-username", "textRemoteUsername",
                        reject=["not registered", "n/a", "…", ""], pattern=r"[A-Za-z0-9_\-]{4,}",
                        timeout=a.timeout)
    pwd = ui_wait_text(f"{a.prefix}-password", "textRemotePassword",
                       reject=["n/a", "…", "••••••••"], timeout=15)
    dev = ui_wait_text(f"{a.prefix}-deviceid", "textRemoteDeviceId",
                       reject=["n/a", "…"], timeout=15)
    log(f"registration UI: username={user} password={'<set>' if pwd else None} device_id={dev}")
    out = {f"{a.prefix}_username": user, f"{a.prefix}_device_id_ui": dev,
           f"{a.prefix}_password_shown": bool(pwd)}
    if a.store and user and pwd:
        state_set(username=user, password=pwd)
    state_set(**out)
    print(json.dumps(out))
    sys.exit(0 if user else 1)


def cmd_devices(a):
    st, data = api("GET", "/devices")
    now = time.time()
    devs = []
    if st == 200 and isinstance(data, list):
        for d in data:
            seen = iso_to_ts(d.get("lastSeen"))
            devs.append({
                "id": d.get("id"),
                "name": d.get("name"),
                "createdAt": d.get("createdAt"),
                "lastSeen": d.get("lastSeen"),
                "lastSeen_age_s": round(now - seen, 1) if seen else None,
            })
    with open(os.path.join(OUT, f"devices-{a.label}.json"), "w") as f:
        json.dump({"status": st, "devices": devs, "raw": data if st != 200 else None}, f, indent=2)
    if not a.test:
        print(json.dumps({"status": st, "devices": devs}))
        return
    # Pick the device the UI showed; fall back to the only/newest device on the account.
    want = state().get(a.expect_key) if a.expect_key else None
    dev = next((d for d in devs if want and d["id"] == want), None)
    if dev is None and devs and not want:
        dev = max(devs, key=lambda d: iso_to_ts(d.get("createdAt")) or 0)
    if dev is not None:
        state_set(**{a.store_key: dev["id"]})
    record(a.test, devices_status=st, device_count=len(devs), device_found=dev is not None,
           device_id=dev["id"] if dev else None, device_lastSeen=dev["lastSeen"] if dev else None,
           device_lastSeen_age_s=dev["lastSeen_age_s"] if dev else None,
           ui_device_id=want)
    print(f"FOUND {dev['lastSeen_age_s']}" if dev else f"MISSING status={st} devices={devs}")


def cmd_wh_create(a):
    st, data = http("POST", f"{WEBHOOK_SITE}/token")
    if st not in (200, 201) or not isinstance(data, dict) or not data.get("uuid"):
        log(f"webhook.site token creation failed: {st} {str(data)[:200]}")
        sys.exit(1)
    state_set(wh_uuid=data["uuid"])
    print(f"{WEBHOOK_SITE}/{data['uuid']}")


def cmd_webhooks_register(a):
    url = f"{WEBHOOK_SITE}/{state()['wh_uuid']}"
    ids, results = [], {}
    for ev in ["sms:received", "sms:sent", "sms:delivered"]:
        st, data = api("POST", "/webhooks", {"url": url, "event": ev})
        results[ev] = st
        if isinstance(data, dict) and data.get("id"):
            ids.append(data["id"])
        log(f"register webhook {ev}: {st} {str(data)[:200]}")
    state_set(webhook_ids=ids)
    st, listed = api("GET", "/webhooks")
    ours = [w for w in listed if w.get("url") == url] if isinstance(listed, list) else []
    record("T3", status="pass" if len(ours) == 3 else "fail",
           post_status=results, listed_count=len(ours),
           events=sorted(w.get("event") for w in ours))
    print(json.dumps({"post": results, "listed": len(ours)}))


def cmd_webhooks_delete(a):
    url = f"{WEBHOOK_SITE}/{state().get('wh_uuid')}"
    st, listed = api("GET", "/webhooks")
    deleted = {}
    for w in listed if isinstance(listed, list) else []:
        if w.get("url") == url:
            dst, _ = api("DELETE", f"/webhooks/{w['id']}")
            deleted[w["id"]] = dst
    st2, after = api("GET", "/webhooks")
    remaining = [w for w in after if w.get("url") == url] if isinstance(after, list) else None
    record("cleanup", deleted=deleted, remaining=len(remaining) if remaining is not None else None)
    log(f"deleted webhooks: {deleted}, remaining={remaining}")


def cmd_measure_send(a):
    s = state()
    device_id = a.device_id or s.get(a.device_key)
    if not device_id:
        record(a.test, status="fail", error=f"no device id ({a.device_key})")
        log(f"{a.test}: no device id")
        return
    doze = None
    if a.doze:
        doze = doze_enter(a.doze == "whitelist")
        if doze["deep_state"] != "IDLE":
            log(f"{a.test}: WARNING deep state is {doze['deep_state']}, not IDLE")
    text = f"Senel e2e {a.test} dummy test message {int(time.time())}"
    t0 = time.time()
    st, resp = api("POST", "/messages?skipPhoneValidation=true",
                   {"textMessage": {"text": text}, "deviceId": device_id, "phoneNumbers": [TO_NUMBER]})
    if st not in (200, 201, 202) or not isinstance(resp, dict) or not resp.get("id"):
        record(a.test, status="fail", error=f"POST /messages -> {st} {str(resp)[:300]}")
        log(f"{a.test}: POST failed {st} {resp}")
        if a.doze:
            doze_exit()
        return
    mid = resp["id"]
    log(f"{a.test}: posted message {mid} to device {device_id} (initial state {resp.get('state')})")
    first_seen, last, states_map, polls_failed = {}, None, None, 0
    wh = {}
    next_wh = next_doze = 0
    while time.time() - t0 < a.timeout:
        now = time.time()
        st, m = api("GET", f"/messages/{mid}")
        if st == 200 and isinstance(m, dict):
            last = m.get("state")
            states_map = m.get("states") or states_map
            if last and last not in first_seen:
                first_seen[last] = round(now - t0, 1)
                log(f"{a.test}: state {last} at +{first_seen[last]}s")
        else:
            polls_failed += 1
        if now >= next_wh:
            for ev in ("sms:sent", "sms:delivered", "sms:failed"):
                if ev in wh:
                    continue
                r, body = wh_find(ev, needle=mid)
                if r:
                    created = whsite_to_ts(r.get("created_at"))
                    wh[ev] = {"observed_s": round(now - t0, 1),
                              "created_at": r.get("created_at"),
                              "arrival_s": round(created - t0, 1) if created else None}
                    log(f"{a.test}: webhook {ev} arrived at +{wh[ev]['arrival_s']}s (seen +{wh[ev]['observed_s']}s)")
            next_wh = now + 4
        if doze is not None and now >= next_doze:
            doze_check(doze)
            next_doze = now + 10
        done_state = any(k in first_seen for k in ("Sent", "Delivered"))
        if (done_state and "sms:sent" in wh) or "Failed" in first_seen:
            break
        time.sleep(2)
    if doze is not None:
        doze["deep_state_at_end"] = deep_state()
        doze_exit()

    def first(*keys):
        vals = [first_seen[k] for k in keys if k in first_seen]
        return min(vals) if vals else None

    rel_states = {}
    for k, v in (states_map or {}).items():
        ts = iso_to_ts(v)
        rel_states[k] = round(ts - t0, 1) if ts else v
    t_processed = first("Processed", "Sent", "Delivered", "Failed")
    t_sent = first("Sent", "Delivered")
    status = "pass" if t_sent is not None else "fail"
    res = dict(status=status, message_id=mid, device_id=device_id, timeout_s=a.timeout,
               final_state=last, t_processed_s=t_processed, t_sent_s=t_sent,
               first_seen_s=first_seen, device_state_timestamps_rel_s=rel_states,
               webhooks=wh, webhook_sent_s=(wh.get("sms:sent") or {}).get("arrival_s"),
               failed_polls=polls_failed)
    if doze is not None:
        res["doze"] = doze
    record(a.test, **res)
    log(f"{a.test}: RESULT {status} processed={t_processed}s sent={t_sent}s "
        f"webhook_sent={res['webhook_sent_s']}s final={last}")


def cmd_measure_inbound(a):
    doze = doze_enter(a.doze == "whitelist") if a.doze else None
    t0 = time.time()
    rc, out, err = adb("emu", "sms", "send", a.sender, a.body)
    log(f"{a.test}: emu sms send -> rc={rc} {out.strip()} {err.strip()}")
    found = None
    next_doze = 0
    while time.time() - t0 < a.timeout:
        now = time.time()
        r, body = wh_find("sms:received", needle=a.body, after_ts=t0)
        if r:
            created = whsite_to_ts(r.get("created_at"))
            found = {"observed_s": round(now - t0, 1), "created_at": r.get("created_at"),
                     "arrival_s": round(created - t0, 1) if created else None,
                     "payload_keys": sorted((body.get("payload") or {}).keys())}
            log(f"{a.test}: sms:received webhook at +{found['arrival_s']}s")
            break
        if doze is not None and now >= next_doze:
            doze_check(doze)
            next_doze = now + 10
        time.sleep(4)
    if doze is not None:
        doze["deep_state_at_end"] = deep_state()
        doze_exit()
    res = dict(status="pass" if found else "fail", emu_rc=rc, emu_out=(out + err).strip()[:200],
               timeout_s=a.timeout, webhook=found,
               webhook_received_s=found["arrival_s"] if found else None)
    if doze is not None:
        res["doze"] = doze
    record(a.test, **res)
    log(f"{a.test}: RESULT {res['status']} webhook_received={res['webhook_received_s']}s")


def cmd_logcat_wait(a):
    deadline = time.time() + a.timeout
    while True:
        lines = grep_lines(logcat_after_marker(a.marker), a.pattern, 20)
        if lines or time.time() > deadline:
            break
        time.sleep(3)
    for l in lines:
        print(l)
    sys.exit(0 if lines else 1)


def cmd_logcat_grep(a):
    text = logcat_after_marker(a.marker) if a.marker else logcat_text()
    for l in grep_lines(text, a.pattern, a.limit):
        print(l)


def cmd_mark(a):
    mark(a.marker)


def cmd_record(a):
    fields = {}
    for kv in a.set or []:
        k, _, v = kv.partition("=")
        fields[k] = parse_value(v)
    for kv in a.set_file or []:
        k, _, path = kv.partition("=")
        try:
            with open(path, errors="replace") as f:
                fields[k] = [l.rstrip("\n") for l in f if l.strip()][:40]
        except OSError:
            fields[k] = None
    record(a.test, **fields)


def cmd_summary(a):
    res = load_json(RESULTS_FILE, {})
    order = ["T1", "T2", "T3", "T4", "T5a", "T5b", "T6", "T7off", "T7on", "T8", "cleanup"]
    lines = ["| Test | Status | Processed (s) | Sent (s) | Webhook (s) | Notes |",
             "|---|---|---|---|---|---|"]
    for t in order + sorted(k for k in res if k not in order):
        r = res.get(t)
        if r is None:
            continue
        wh = r.get("webhook_sent_s", r.get("webhook_received_s"))
        notes = []
        if "doze" in r:
            d = r["doze"]
            notes.append(f"deep={d.get('deep_state')} wl={d.get('app_whitelisted')} reasserts={d.get('reasserts', 0)}")
        if r.get("error"):
            notes.append(str(r["error"])[:80])
        if r.get("final_state"):
            notes.append(f"final={r['final_state']}")
        lines.append(f"| {t} | {r.get('status', '-')} | {r.get('t_processed_s', '-')} | "
                     f"{r.get('t_sent_s', '-')} | {wh if wh is not None else '-'} | {'; '.join(notes)} |")
    text = "\n".join(lines)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write("## Senel e2e results\n\n" + text + "\n")


def cmd_state_get(a):
    print(state().get(a.key, ""))


def cmd_new_device(a):
    """Find a device id on the account other than the old one; store it."""
    old = state().get(a.old_key)
    deadline = time.time() + a.timeout
    while True:
        st, data = api("GET", "/devices")
        if st == 200 and isinstance(data, list):
            others = [d for d in data if d.get("id") != old]
            if others:
                newest = max(others, key=lambda d: iso_to_ts(d.get("createdAt")) or 0)
                state_set(**{a.new_key: newest["id"]})
                print(json.dumps({"old": old, "new": newest["id"], "all": [d.get("id") for d in data],
                                  "lastSeen": newest.get("lastSeen")}))
                return
        if time.time() > deadline:
            print(json.dumps({"status": st, "devices": data if isinstance(data, list) else str(data)[:200]}))
            sys.exit(1)
        time.sleep(5)


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("snap"); s.add_argument("name"); s.set_defaults(f=cmd_snap)
    s = sub.add_parser("tap"); s.add_argument("name"); s.add_argument("--id"); s.add_argument("--text")
    s.add_argument("--contains", action="store_true"); s.add_argument("--timeout", type=int, default=20)
    s.set_defaults(f=cmd_tap)
    s = sub.add_parser("checked"); s.add_argument("name"); s.add_argument("--id", required=True)
    s.add_argument("--want", choices=["true", "false"]); s.add_argument("--timeout", type=int, default=20)
    s.set_defaults(f=cmd_checked)
    s = sub.add_parser("type"); s.add_argument("name"); s.add_argument("--id", required=True)
    s.add_argument("--value"); s.add_argument("--state-key"); s.add_argument("--timeout", type=int, default=20)
    s.set_defaults(f=cmd_type)
    s = sub.add_parser("read-registration"); s.add_argument("--prefix", required=True)
    s.add_argument("--store", action="store_true"); s.add_argument("--timeout", type=int, default=90)
    s.set_defaults(f=cmd_read_registration)
    s = sub.add_parser("devices"); s.add_argument("--label", default="x"); s.add_argument("--test")
    s.add_argument("--expect-key"); s.add_argument("--store-key", default="device_id")
    s.set_defaults(f=cmd_devices)
    s = sub.add_parser("wh-create"); s.set_defaults(f=cmd_wh_create)
    s = sub.add_parser("webhooks-register"); s.set_defaults(f=cmd_webhooks_register)
    s = sub.add_parser("webhooks-delete"); s.set_defaults(f=cmd_webhooks_delete)
    s = sub.add_parser("measure-send"); s.add_argument("--test", required=True)
    s.add_argument("--device-id"); s.add_argument("--device-key", default="device_id")
    s.add_argument("--timeout", type=int, default=180)
    s.add_argument("--doze", choices=["whitelist", "nowhitelist"]); s.set_defaults(f=cmd_measure_send)
    s = sub.add_parser("measure-inbound"); s.add_argument("--test", required=True)
    s.add_argument("--timeout", type=int, default=600); s.add_argument("--sender", default="4512345678")
    s.add_argument("--body", default="Ja tak")
    s.add_argument("--doze", choices=["whitelist", "nowhitelist"]); s.set_defaults(f=cmd_measure_inbound)
    s = sub.add_parser("logcat-wait"); s.add_argument("--marker", required=True)
    s.add_argument("--pattern", required=True); s.add_argument("--timeout", type=int, default=60)
    s.set_defaults(f=cmd_logcat_wait)
    s = sub.add_parser("logcat-grep"); s.add_argument("--marker"); s.add_argument("--pattern", required=True)
    s.add_argument("--limit", type=int, default=40); s.set_defaults(f=cmd_logcat_grep)
    s = sub.add_parser("mark"); s.add_argument("marker"); s.set_defaults(f=cmd_mark)
    s = sub.add_parser("record"); s.add_argument("test"); s.add_argument("--set", action="append")
    s.add_argument("--set-file", action="append"); s.set_defaults(f=cmd_record)
    s = sub.add_parser("summary"); s.set_defaults(f=cmd_summary)
    s = sub.add_parser("state-get"); s.add_argument("key"); s.set_defaults(f=cmd_state_get)
    s = sub.add_parser("new-device"); s.add_argument("--old-key", default="device_id")
    s.add_argument("--new-key", default="device_id_t8"); s.add_argument("--timeout", type=int, default=60)
    s.set_defaults(f=cmd_new_device)

    a = p.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
