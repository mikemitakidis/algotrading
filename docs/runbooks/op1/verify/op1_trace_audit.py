"""Audit an strace -f log of the OP1 session: what ran, what was written, what connected, what was signalled.

usage: python3 op1_trace_audit.py <trace> <OUT dir> <ROOT dir>
Exit 0 when no violation is found. Prints every executed program (with its arguments for git, systemctl,
journalctl, crontab, sqlite3), every write outside OUT, every network connect and every kill.
"""
import os
import re
import sys
from collections import Counter

trace, OUT, ROOT = sys.argv[1], os.path.realpath(sys.argv[2]), os.path.realpath(sys.argv[3])
FORBIDDEN = {"pip", "pip3", "kill", "pkill", "killall", "sudo", "su", "curl", "wget", "nc", "ncat", "ssh", "scp",
             "rsync", "telnet", "systemd-run", "docker", "apt", "apt-get", "rm", "mv", "chmod", "chown", "reboot",
             "shutdown", "service", "at", "nohup"}
SYSTEMCTL_OK = {"show", "list-units", "list-timers", "list-unit-files", "is-active", "is-enabled"}
GIT_OK = {"rev-parse", "log", "remote", "status"}
WRITE_FLAGS = ("O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC", "O_APPEND")
OK_WRITE_PREFIX = (OUT + "/", "/dev/null", "/dev/tty", "/dev/pts/", "/proc/self/fd/")
viol, progs, special, writes_out, connects, kills = [], Counter(), [], Counter(), [], []


def args_of(line):
    m = re.search(r'execve\("([^"]+)", \[(.*?)\]', line)
    if not m:
        return None, []
    return m.group(1), re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(2))


def inside(p):
    p = os.path.realpath(p)
    return p == OUT or p.startswith(OK_WRITE_PREFIX)


for line in open(trace, errors="replace"):
    if "execve(" in line and " = -1 " not in line and "resumed>" not in line:
        path, argv = args_of(line)
        if not path:
            continue
        name = os.path.basename(path)
        progs[name] += 1
        if name in FORBIDDEN:
            viol.append("forbidden program: %s %s" % (name, argv[1:]))
        if name == "systemctl":
            sub = next((a for a in argv[1:] if not a.startswith("-")), None)
            if sub not in SYSTEMCTL_OK:
                viol.append("systemctl %s" % argv[1:])
            special.append(" ".join(argv[:3]))
        if name == "git":
            rest, i = argv[1:], 0
            while i < len(rest) and rest[i] in ("-c", "-C"):
                i += 2
            sub = rest[i] if i < len(rest) else None
            if sub not in GIT_OK:
                viol.append("git %s" % rest)
            special.append("git %s" % sub)
        if name == "crontab" and argv[1:] != ["-l"]:
            viol.append("crontab %s" % argv[1:])
        if name == "sqlite3" and not any("mode=ro" in a or a == "-readonly" for a in argv):
            viol.append("sqlite3 without read-only: %s" % argv[1:])
        if name in ("sqlite3", "journalctl", "crontab"):
            special.append(" ".join(argv[:2]))
    m = re.search(r'\b(open|openat|creat)\((?:AT_FDCWD, )?"([^"]+)"(?:, ([A-Z_|]+))?', line)
    if m and " = -1 " not in line:
        call, p, flags = m.group(1), m.group(2), m.group(3) or ""
        if call == "creat" or any(f in flags for f in WRITE_FLAGS):
            if not p.startswith("/"):
                viol.append("relative write path (review): %s" % line.strip()[:200])
            elif inside(p):
                writes_out[os.path.relpath(os.path.realpath(p), OUT) if p.startswith(OUT) else p] += 1
            else:
                viol.append("write outside OUT: %s (%s)" % (p, flags))
    m = re.search(r'\b(unlink|unlinkat|rename|renameat2?|mkdir|mkdirat|rmdir|truncate|link|linkat|symlink|symlinkat|'
                  r'chmod|fchmodat|chown|fchownat|lchown)\((?:AT_FDCWD|\d+)?,? ?"([^"]+)"', line)
    if m and " = -1 " not in line:
        p = m.group(2)
        if not (p.startswith("/") and inside(p)):
            viol.append("%s outside OUT: %s" % (m.group(1), p))
    if re.search(r'\b(connect|sendto)\(', line):
        fam = re.search(r'sa_family=(AF_\w+)', line)
        if fam:
            connects.append((fam.group(1), line.strip()[:160]))
            if fam.group(1) not in ("AF_UNIX", "AF_NETLINK"):
                viol.append("network connect/send (attempt or success): %s" % line.strip()[:200])
    if re.search(r'\b(kill|tkill|tgkill)\(', line):
        kills.append(line.strip()[:160])
        viol.append("signal sent: %s" % line.strip()[:200])
    if re.search(r'\b(ptrace|mount)\(', line):
        viol.append("privileged call: %s" % line.strip()[:200])

print("programs executed:", ", ".join("%s×%d" % kv for kv in sorted(progs.items())))
print("git/systemctl/sqlite3/journalctl/crontab invocations:", ", ".join(sorted(Counter(special))))
print("files written (all under OUT):", len(writes_out))
print("connects:", Counter(f for f, _ in connects))
print("signals sent:", len(kills))
print("VIOLATIONS: %d" % len(set(viol)))
for v in sorted(set(viol)):
    print("  " + v)
sys.exit(1 if viol else 0)
