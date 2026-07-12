import asyncio
import json
import threading
import time
import uuid
from types import SimpleNamespace

from gateway.signal_gate import m3_client, schema
from gateway.signal_gate.store import Store


def display(no_today="null", text="Advance Observation app", mission="signalpath"):
    return "Captured.\n```signal\n" + json.dumps({"actions":[{"text":text,"mission":mission},{"text":"Tidy small jobs","mission":"billos"},{"text":"Organise Christine operations","mission":"billos"}],"no_today":json.loads(no_today)}) + "\n```\nCaptured as a proposal. Reply Y within 30 minutes to approve canonical application."


def test_exact_schema_and_stable_hash():
    proposal = schema.parse_display(display())
    assert proposal.no_today is None
    assert schema.canonical_json(proposal) == schema.canonical_json(schema.parse_display(display()))


def test_schema_objective_rejections():
    for kwargs in ({"no_today":'"invented"'}, {"text":" bad"}, {"text":"bad|mission"}, {"text":"#hidden"}, {"text":"NOT TODAY: x"}, {"mission":"other"}):
        try:
            schema.parse_display(display(**kwargs))
        except schema.ValidationError:
            pass
        else:
            raise AssertionError(kwargs)
    assert schema.parse_display("ordinary answer") is None


def prepared(store):
    proposal=schema.parse_display(display()); canonical=schema.canonical_json(proposal)
    pid=store.prepare(bill_user_id="1",chat_id="2",source_update_id="10",source_message_id="20",display_sha256=schema.sha256(display()),proposal_sha256=schema.sha256(canonical),proposal_json=canonical)
    assert store.arm(pid,"30")
    return pid

def claim(store,pid,update="99",message="40",reply=None):
    rid=str(uuid.uuid4()); body=json.dumps({"request_id":rid})
    return store.claim(pid,update,message,rid,body,reply)


def test_supersession_expiry_and_terminal_immutability(tmp_path):
    store=Store(tmp_path/"s.db"); first=prepared(store); second=prepared(store)
    assert store.get(first).state == "SUPERSEDED"
    assert store.get(second).state == "PENDING"
    assert not store.transition(first,"SUPERSEDED","PENDING")
    with store.connect() as db: db.execute("UPDATE proposal SET expires_at=? WHERE id=?",(time.time()-1,second))
    assert store.eligible("1","2") == [] and store.get(second).state == "EXPIRED"


def test_atomic_claim_and_update_dedup(tmp_path):
    store=Store(tmp_path/"s.db"); pid=prepared(store); wins=[]
    def attempt(): wins.append(claim(store,pid))
    threads=[threading.Thread(target=attempt) for _ in range(8)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert sum(bool(x) for x in wins) == 1
    assert store.get(pid).state == "PROCESSING"
    assert not claim(store,pid)


def test_reply_reference_mismatch_fails_closed(tmp_path):
    store=Store(tmp_path/"s.db"); pid=prepared(store)
    assert claim(store,pid,"100","41","wrong") is None
    assert store.get(pid).state == "PENDING"


def test_indeterminate_recovery_and_no_processing_to_pending(tmp_path):
    store=Store(tmp_path/"s.db"); pid=prepared(store); rid=claim(store,pid,"101","42")
    assert rid and store.indeterminate(pid,"timeout")
    assert store.get(pid).request_id == rid
    assert not store.transition(pid,"INDETERMINATE","PENDING")
    with store.connect() as db: db.execute("UPDATE proposal SET updated_at=0 WHERE id=?",(pid,))
    assert [x.id for x in store.stale_inflight(1)] == [pid]


def test_verified_m3_response_required():
    req={"request_id":"r","proposal_id":"p","proposal_sha256":"a"*64}
    body={**req,"version":1,"status":"APPLIED","event_id":"e","event_type":"SIGNAL_SET","process_receipt":{"receipt_id":"x","receipt_sha256":"b"*64,"canonical_state_sha256":"c"*64}}
    body["response_sha256"]=m3_client.canonical_hash(body)
    assert m3_client.verify_response(body,req)["status"] == "APPLIED"
    body["proposal_id"]="wrong"
    try: m3_client.verify_response(body,req)
    except m3_client.M3Error: pass
    else: raise AssertionError("unverified response accepted")


def test_query_not_in_ssh_argv(monkeypatch):
    captured={}
    class Proc:
        returncode=0
        async def communicate(self,body):
            captured["stdin"]=body
            req=json.loads(body); out={**{k:req[k] for k in ("request_id","proposal_id","proposal_sha256")},"version":1,"status":"REJECTED","applied":False,"error":{"code":"test","message":"test"}}
            out["response_sha256"]=m3_client.canonical_hash(out)
            return json.dumps(out).encode(),b""
    async def create(*argv,**kwargs): captured["argv"]=argv; return Proc()
    monkeypatch.setattr(asyncio,"create_subprocess_exec",create)
    req={"request_id":"secret-request","proposal_id":"secret-proposal","proposal_sha256":"a"*64,"signal":{"actions":[],"no_today":None}}
    asyncio.run(m3_client.call(req))
    assert "secret-request" not in " ".join(captured["argv"])
    assert b"secret-request" in captured["stdin"]
