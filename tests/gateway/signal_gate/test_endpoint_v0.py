import hashlib
import json
import os
import subprocess
import concurrent.futures
import uuid
from pathlib import Path

ROOT=Path(__file__).resolve().parents[3]
ENDPOINT=ROOT/"deploy/signal_v0/m3/signal-set-endpoint"

def canon(x): return json.dumps(x,sort_keys=True,separators=(",",":"),ensure_ascii=False)
def sha(x): return hashlib.sha256(x.encode()).hexdigest()

def staged(tmp_path):
    billos=tmp_path/"BillOS"; (billos/"events").mkdir(parents=True); (billos/"bin").mkdir(); (billos/"receipts/process-event").mkdir(parents=True); (billos/"state/signal").mkdir(parents=True)
    processor=billos/"bin/process-event"
    processor.write_text("#!/bin/sh\neid=$2\necho state > '"+str(billos/"state/signal/current_signal.json")+"'\necho receipt > '"+str(billos/"receipts/process-event")+"/'$eid'.test.txt'\n")
    processor.chmod(0o755)
    source=ENDPOINT.read_text().replace('/Users/billlyon/VaultHub/BillVault/Agent-Shared/BillOS',str(billos)).replace('/Users/billlyon/.local/state/billos-signal-endpoint/requests.sqlite',str(tmp_path/'requests.sqlite'))
    script=tmp_path/"endpoint"; script.write_text(source); script.chmod(0o755); return script,billos

def request(rid=None):
    signal={"actions":[{"text":"Advance Observation app","mission":"signalpath"},{"text":"Tidy small jobs","mission":"billos"},{"text":"Organise Christine operations","mission":"billos"}],"no_today":None}
    return {"version":1,"operation":"SIGNAL_SET","request_id":rid or str(uuid.uuid4()),"proposal_id":str(uuid.uuid4()),"proposal_sha256":sha(canon(signal)),"approved_by":{"platform":"telegram","user_id":"1","chat_id":"2","proposal_message_id":"3","approval_update_id":"4","approval_message_id":"5"},"signal":signal}

def run(script,body):
    return subprocess.run([str(script)],input=canon(body),text=True,capture_output=True,timeout=4)

def test_endpoint_applies_once_and_replays_identical_receipt(tmp_path):
    script,billos=staged(tmp_path); req=request(); first=run(script,req); second=run(script,req)
    assert first.returncode == second.returncode == 0
    one,two=json.loads(first.stdout),json.loads(second.stdout)
    assert one == two and one["status"] == "APPLIED"
    assert len(list((billos/"events").glob("SIGNAL_SET-*.yml"))) == 1

def test_endpoint_rejects_conflict_and_oversize(tmp_path):
    script,_=staged(tmp_path); original=request(); assert json.loads(run(script,original).stdout)["status"] == "APPLIED"
    changed=json.loads(json.dumps(original)); changed["signal"]["actions"][0]["text"]="Different"; changed["proposal_sha256"]=sha(canon(changed["signal"]))
    assert json.loads(run(script,changed).stdout)["error"]["code"] == "idempotency_conflict"
    huge=subprocess.run([str(script)],input="x"*9000,text=True,capture_output=True,timeout=4)
    assert json.loads(huge.stdout)["error"]["code"] == "request_too_large"

def test_concurrent_same_request_has_one_event_and_one_receipt(tmp_path):
    script,billos=staged(tmp_path); req=request()
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _:run(script,req),range(4)))
    payloads=[json.loads(x.stdout) for x in results]
    assert all(x == payloads[0] for x in payloads)
    assert len(list((billos/"events").glob("SIGNAL_SET-*.yml"))) == 1
    assert len(list((billos/"receipts/process-event").glob("*.txt"))) == 1

def test_forced_key_is_restrict_and_fixed_command():
    option=(ROOT/"deploy/signal_v0/authorized_keys.option").read_text()
    assert option.startswith('restrict,no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty,command="/Users/billlyon/bin/signal-set-endpoint"')
    assert "signal-set " not in option and "process-event" not in option

def test_original_ssh_commands_cannot_select_an_executable():
    option=(ROOT/"deploy/signal_v0/authorized_keys.option").read_text()
    forced=option.split('command="',1)[1].split('"',1)[0]
    for requested in ("sh", "signal-set", "process-event --event x", "cat /etc/passwd"):
        assert forced == "/Users/billlyon/bin/signal-set-endpoint"
        assert requested != forced
