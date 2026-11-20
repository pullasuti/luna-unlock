#!/usr/bin/env python3
"""luna-unlock.py — hardened factory_mode unlock/relock for the Smart Clock.

Same proven flow as luna-unlock.py (park -> setenv factory_mode N ->
saveenv -> reset), but self-verifying at every phase so the user never has
to "just try again":

  1. park with a fast poller (arm BEFORE power-on; the cold window is the
     ~750 ms try_auto_burn wait)
  2. channel warm-up: `version` must answer "success" (kills the known
     first-command flake as a confounder)
  3. setenv reply-checked  (3 attempts, must answer "success")
  4. saveenv reply-checked (3 attempts, must answer "success")
  5. value verification: scan the env's DRAM working copy (sparse 64 B
     samples, dense reads around hits — the copy's address differs per
     clock, so nothing is hardcoded) for `factory_mode=<expected>`
  6. `reset` ONLY if 3+4+5 all passed; otherwise the window is held and the
     script exits non-zero with the failed step named

Post-boot proof (run on a machine with adb):
    adb connect <clock>:5555 && adb -s <clock>:5555 shell cat /proc/cmdline
  locked   -> tail "androidboot.debuggable=0 androidboot.secure=1
               androidboot.rpmb_state=0"
  unlocked -> tail absent

Requirements: Python 3, pyamlboot and pyusb installed, and USB access to
the clock's burn mode (device 1b8e:c003).

Usage:
    python3 luna-unlock.py --unlock            # factory_mode 0
    python3 luna-unlock.py --lock              # factory_mode 1
    python3 luna-unlock.py --status            # scan + report, no write
    --wait SECONDS   park timeout (default 300)
"""
import argparse
import sys
import time

try:
    from pyamlboot import pyamlboot
except ImportError:
    sys.exit("pyamlboot is required: "
             "pip install pyusb "
             "git+https://github.com/superna9999/pyamlboot.git "
             "(the PyPI release is too old)")

for _need in ("identify", "bulkCmd", "bulkCmdStat", "readSimpleMemory"):
    if not hasattr(pyamlboot.AmlogicSoC, _need):
        sys.exit("pyamlboot found, but too old (missing %s). The PyPI "
                 "release lacks it - install the git version: "
                 "pip install git+https://github.com/superna9999/pyamlboot.git"
                 % _need)

VID, PID = 0x1b8e, 0xc003
# env DRAM copy hunt ranges: both observed clocks (retail and eng firmware)
# keep the copy near 0x1ec9xxxx, so the hot zone is scanned first and the
# full range only on a miss (drift safety net).
HOT_LO, HOT_HI = 0x1ec00000, 0x1ed00000
FULL_LO, FULL_HI = 0x1e800000, 0x1f000000
ANCHOR = 0x1ec9f000  # dense reads try the hits nearest here first


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def park(wait_s):
    t0 = time.time()
    while time.time() - t0 < wait_s:
        try:
            c = pyamlboot.AmlogicSoC(VID, PID)
            c.identify()
            return c
        except Exception:
            time.sleep(0.1)
    return None


def cmd(soc, cc, wait=1.5):
    """Send one bulkcmd, return the trimmed status reply."""
    for att in range(3):
        try:
            soc.bulkCmd(cc, read_status=False)
            time.sleep(wait)
            try:
                r = bytes(soc.bulkCmdStat(timeout=700)).split(b"\x00")[0]
            except Exception:
                r = b"(none)"
            if b"success" in r or att == 2:
                return r
            time.sleep(0.4)
        except Exception:
            time.sleep(0.4)
    return b"(send-failed)"


def rd64(soc, addr):
    for _ in range(3):
        try:
            return bytes(soc.readSimpleMemory(addr, 64))
        except Exception:
            time.sleep(0.15)
    return b""


def _scan_range(soc, lo, hi, needle):
    """Sparse 64 B samples every 4 KB, then dense 64 KB reads around the
    printable hits (nearest to ANCHOR first). Returns (found, address)."""
    t0 = time.time()
    log("scan: sampling 0x%x-0x%x ..." % (lo, hi))
    hits = []
    a = lo
    while a < hi:
        b = rd64(soc, a)
        if b:
            if needle in b:          # lucky direct hit in the sample itself
                return True, a
            if sum(32 <= c < 127 for c in b) > 40 and b"=" in b:
                hits.append(a)
        a += 0x1000
    log("scan: %d printable hit(s) in %.1fs" % (len(hits), time.time() - t0))
    for i, h in enumerate(sorted(hits, key=lambda x: abs(x - ANCHOR)), 1):
        log("scan: reading around hit %d/%d @ 0x%x ..." % (i, len(hits), h))
        buf = b""
        base = max(lo, h - 0x8000)
        for off in range(0, 0x10000, 64):
            chunk = rd64(soc, base + off)
            if not chunk:
                break
            buf += chunk
            if needle in buf:
                return True, base + buf.index(needle)
        if needle in buf:
            return True, base + buf.index(needle)
    return False, None


def scan_env(soc, needle):
    """Hunt `needle` in the env's DRAM copy: hot zone first, full range
    (with progress output) only if that misses."""
    found, at = _scan_range(soc, HOT_LO, HOT_HI, needle)
    if not found:
        log("scan: not in the hot zone - widening to the full range "
            "0x%x-0x%x (this can take a while) ..." % (FULL_LO, FULL_HI))
        found, at = _scan_range(soc, FULL_LO, FULL_HI, needle)
    return found, at


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--unlock", action="store_true")
    g.add_argument("--lock", action="store_true")
    g.add_argument("--status", action="store_true")
    ap.add_argument("--wait", type=int, default=300)
    ap.add_argument("--yes", action="store_true",
                    help="skip the lock confirmation prompt (for non-tty use)")
    args = ap.parse_args()
    target = 0 if args.unlock else 1

    if args.lock:
        log("NOTE: locking disables adb (USB and network). Make sure ssh "
            "access works BEFORE you rely on the locked state - our dropbear "
            "(key-only) keeps running when locked, adb does not.")
        if args.yes:
            log("--yes given: proceeding without confirmation")
        elif not sys.stdin.isatty():
            log("REFUSING: --lock needs confirmation but stdin is not a "
                "terminal. Re-run interactively or pass --yes.")
            return 1
        else:
            try:
                ans = input("Type LOCK to proceed, anything else aborts: ")
            except EOFError:
                ans = ""
            if ans.strip() != "LOCK":
                log("aborted by user")
                return 1
            log("confirmed")

    log("waiting for burn window (cut power now if not parked)...")
    soc = park(args.wait)
    if not soc:
        log("FAIL: no window in %d s" % args.wait)
        return 1
    log("parked")

    # phase 2: channel warm-up
    for i in range(5):
        r = cmd(soc, "version")
        if b"success" in r:
            log("warm-up ok (try %d)" % (i + 1))
            break
        time.sleep(0.5)
    else:
        log("FAIL: channel warm-up never answered success")
        return 1

    if args.status:
        for v in (0, 1):
            found, at = scan_env(soc, b"factory_mode=%d" % v)
            if found:
                log("status: factory_mode=%d (env copy @ 0x%x)" % (v, at))
                return 0
        log("status: factory_mode string not found in scan range")
        return 1

    # phase 3: setenv
    r = cmd(soc, "setenv factory_mode %d" % target)
    if b"success" not in r:
        log("FAIL: setenv factory_mode %d -> %r (window held, NOT resetting)"
            % (target, r))
        return 1
    log("setenv factory_mode %d ok" % target)

    # phase 4: saveenv
    r = cmd(soc, "saveenv")
    if b"success" not in r:
        log("FAIL: saveenv -> %r (window held, NOT resetting)" % r)
        return 1
    log("saveenv ok")

    # phase 5: value readback
    found, at = scan_env(soc, b"factory_mode=%d" % target)
    if not found:
        log("FAIL: factory_mode=%d not found in env copy — NOT resetting"
            % target)
        return 1
    log("verified factory_mode=%d in env copy @ 0x%x" % (target, at))

    # phase 6: all gates passed -> reboot
    cmd(soc, "reset", 3)
    if target == 1:
        log("reset sent — rebooting LOCKED. Post-boot proof: adb is expected "
            "to be GONE (port 5555 refused, USB and network). That refusal "
            "IS the confirmation; do not try adb.")
    else:
        log("reset sent — rebooting UNLOCKED. Post-boot proof: adb answers "
            "over USB or network (5555); /proc/cmdline shows "
            "androidboot.debuggable=1 androidboot.secure=0.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
