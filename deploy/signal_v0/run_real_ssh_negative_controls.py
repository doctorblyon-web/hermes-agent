#!/usr/bin/python3
"""One-shot, non-mutating real-SSH negative-control runner for SIGNAL V0."""
import json
import os
import shlex
import socket
import subprocess
import time

HOST = "billlyon@100.122.219.24"
KEY = "/Users/willlyon/.ssh/billos_signal_v0"
SSH = ["/usr/bin/ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]
DEDICATED = SSH + ["-i", KEY, "-o", "IdentitiesOnly=yes"]
SNAPSHOT = r'''import hashlib,json,pathlib
root=pathlib.Path("/Users/billlyon/VaultHub/BillVault/Agent-Shared/BillOS")
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() and p.is_file() else None
digest=lambda xs:hashlib.sha256("\n".join(str(p)+":"+str(sha(p)) for p in xs if p.is_file()).encode()).hexdigest()
groups={"signal_events":sorted((root/"events").glob("SIGNAL_SET-*.yml")),"signal_receipts":sorted((root/"receipts/process-event").glob("SIGNAL_SET-*.txt")),"missions":sorted((root/"state/missions").glob("*")),"dashboard":sorted((root/"state").glob("*.yaml"))+sorted((root/"state/signal").glob("*"))}
files=[root/"events/processed.log",root/"ledger/state_mutations.jsonl",root/"state/signal/current_signal.json",root/"state/nudges.yaml"]
print(json.dumps({"files":{str(p):sha(p) for p in files},"groups":{k:{"count":len([p for p in v if p.is_file()]),"digest":digest(v)} for k,v in groups.items()}},sort_keys=True))'''


def snapshot():
    remote = "/usr/bin/python3 -c " + shlex.quote(SNAPSHOT)
    result = subprocess.run(SSH + [HOST, remote], capture_output=True, text=True, timeout=10, check=True)
    return json.loads(result.stdout)


def forced(name, command, payload=b""):
    result = subprocess.run(DEDICATED + ["-T", HOST, command], input=payload, capture_output=True, timeout=10)
    body = json.loads(result.stdout)
    assert body["status"] == "REJECTED" and body["applied"] is False
    return {"name": name, "exit": result.returncode, "code": body["error"]["code"]}


before = snapshot()
results = []
for name, command in (
    ("hostname", "hostname"),
    ("shell", "sh"),
    ("sed", "sed -n 1p /etc/hosts"),
    ("signal-set", "/Users/billlyon/bin/signal-set"),
    ("process-event", "/Users/billlyon/VaultHub/BillVault/Agent-Shared/BillOS/bin/process-event --event x"),
    ("alternate", "/bin/echo alternate"),
):
    results.append(forced(name, command))
results.append(forced("malformed-json", "ignored", b"{"))
results.append(forced("oversized-json", "ignored", b"x" * 9000))
unknown = {"version": 1, "operation": "OTHER", "request_id": "x", "proposal_id": "y", "proposal_sha256": "z", "approved_by": {}, "signal": {}}
results.append(forced("unknown-operation", "ignored", json.dumps(unknown).encode()))

pty = subprocess.run(DEDICATED + ["-tt", HOST, "hostname"], input=b"", capture_output=True, timeout=10)
assert pty.returncode != 0 and b"PTY allocation request failed" in pty.stderr
results.append({"name": "pty", "exit": pty.returncode, "code": "rejected"})

forward = subprocess.Popen(DEDICATED + ["-T", "-o", "ExitOnForwardFailure=yes", "-L", "127.0.0.1:49386:127.0.0.1:3851", HOST, "hostname"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
time.sleep(0.5)
probe = socket.socket(); probe.settimeout(1); forward_result = probe.connect_ex(("127.0.0.1", 49386)); probe.close()
if forward_result == 0:
    probe = socket.create_connection(("127.0.0.1", 49386), timeout=1)
    probe.sendall(b"GET / HTTP/1.0\r\n\r\n")
    try:
        forwarded_data = probe.recv(64)
    except (ConnectionError, TimeoutError, socket.timeout):
        forwarded_data = b""
    probe.close()
    assert not forwarded_data.startswith(b"HTTP/")
forward.stdin.close(); forward.wait(timeout=10)
results.append({"name": "port-forward", "exit": forward.returncode, "code": "unreachable"})

env = dict(os.environ); env["DISPLAY"] = ":99"
forward_env = subprocess.Popen(DEDICATED + ["-A", "-X", "-T", HOST, "hostname"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
time.sleep(0.5)
inspect = 'import subprocess; x=[v for v in subprocess.check_output(["/bin/ps","eww","-ax"],text=True,errors="replace").splitlines() if "signal-set-endpoint" in v and "python3 -c" not in v]; import json; print(json.dumps({"count":len(x),"agent":any("SSH_AUTH_SOCK=" in v for v in x),"display":any("DISPLAY=" in v for v in x),"xauthority":any("XAUTHORITY=" in v for v in x)}))'
checked = subprocess.run(SSH + [HOST, "/usr/bin/python3 -c " + shlex.quote(inspect)], capture_output=True, text=True, timeout=10, check=True)
forwarding = json.loads(checked.stdout)
assert forwarding == {"count": 1, "agent": False, "display": False, "xauthority": False}
forward_env.stdin.close(); forward_env.wait(timeout=10)
results.extend([{"name": "agent-forward", "exit": 0, "code": "absent"}, {"name": "x11-forward", "exit": 0, "code": "absent"}])

after = snapshot()
assert after == before
print(json.dumps({"controls": results, "canonical_unchanged": True, "before": before, "after": after}, sort_keys=True))
