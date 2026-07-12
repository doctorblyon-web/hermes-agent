import json
from types import SimpleNamespace

import pytest
from gateway.signal_gate import m3_client, schema, service


def text():
    return 'Captured.\n```signal\n{"actions":[{"text":"Advance Observation app","mission":"signalpath"},{"text":"Tidy small jobs","mission":"billos"},{"text":"Organise Christine operations","mission":"billos"}],"no_today":null}\n```\nCaptured as a proposal. Reply Y within 30 minutes to approve canonical application.'


class Adapter:
    name="telegram"
    def __init__(self): self.sent=[]
    async def send(self,chat_id,content,reply_to=None,metadata=None):
        self.sent.append((str(chat_id),content,reply_to))
        return SimpleNamespace(success=True,message_id="outcome",continuation_message_ids=(),raw_response={})


def event(uid="1",chat="2",body="Y",update="4",message="5",reply=None):
    return SimpleNamespace(text=body,source=SimpleNamespace(user_id=uid,chat_id=chat),platform_update_id=update,message_id=message,reply_to_message_id=reply)


@pytest.fixture
def configured(monkeypatch,tmp_path):
    cfg={"enabled":True,"bill_user_id":"1","chat_id":"2","database":str(tmp_path/"gate.db")}
    monkeypatch.setattr(service,"settings",lambda:cfg)
    return cfg


def prepare(configured):
    adapter=Adapter(); ev=event(body="prior",update="1",message="2")
    prepared=service.prepare(adapter,ev,text())
    assert service.finalize(prepared,SimpleNamespace(success=True,message_id="3",continuation_message_ids=(),raw_response={}))
    return adapter,prepared


@pytest.mark.asyncio
async def test_exact_y_applies_without_model(configured,monkeypatch):
    adapter,prepared=prepare(configured)
    async def applied(request,timeout=12):
        out={"version":1,"request_id":request["request_id"],"proposal_id":request["proposal_id"],"proposal_sha256":request["proposal_sha256"],"status":"APPLIED","event_id":"event-1","event_type":"SIGNAL_SET","process_receipt":{"receipt_id":"receipt-1","receipt_sha256":"b"*64,"canonical_state_sha256":"c"*64}}
        out["response_sha256"]=m3_client.canonical_hash(out); return out
    monkeypatch.setattr(m3_client,"call",applied)
    assert await service.intercept(adapter,event(reply=None))
    assert "Applied canonically on M3" in adapter.sent[-1][1]
    assert prepared.store.get(prepared.proposal_id).state == "APPLIED"


@pytest.mark.asyncio
async def test_wrong_shapes_and_lanes_not_intercepted(configured):
    adapter=Adapter(); prepare(configured)
    for ev in (event(body="yes"),event(body="Y."),event(body="Y 👍"),event(uid="9"),event(chat="9")):
        assert not await service.intercept(adapter,ev)


@pytest.mark.asyncio
async def test_mismatched_reply_and_duplicate_fail_closed(configured,monkeypatch):
    adapter,prepared=prepare(configured)
    async def unavailable(request,timeout=12): raise m3_client.M3Error("test")
    monkeypatch.setattr(m3_client,"call",unavailable)
    assert await service.intercept(adapter,event(reply="wrong"))
    assert prepared.store.get(prepared.proposal_id).state == "PENDING"
    assert await service.intercept(adapter,event(reply=None))  # duplicate update remains rejected
    assert prepared.store.get(prepared.proposal_id).state == "PENDING"
    assert await service.intercept(adapter,event(update="6",message="7",reply=None))
    assert prepared.store.get(prepared.proposal_id).state == "PROCESSING" or prepared.store.get(prepared.proposal_id).state == "INDETERMINATE"


def test_invalid_governed_envelope_never_delivered(configured):
    with pytest.raises((service.GovernedError, schema.ValidationError)):
        service.prepare(Adapter(),event(body="prior"),text().replace('"no_today":null','"no_today":"invented"'))


@pytest.mark.asyncio
async def test_startup_reconciles_persisted_request(configured,monkeypatch):
    adapter,prepared=prepare(configured); txn=prepared.store.get(prepared.proposal_id)
    rid="12345678-1234-4234-9234-123456789012"
    request=service._request(txn,event(),rid)
    assert prepared.store.claim(txn.id,"44","55",rid,json.dumps(request),None)
    async def applied(req,timeout=12):
        out={"version":1,"request_id":req["request_id"],"proposal_id":req["proposal_id"],"proposal_sha256":req["proposal_sha256"],"status":"APPLIED","event_id":"recovered-event","event_type":"SIGNAL_SET","process_receipt":{"receipt_id":"recovered-receipt","receipt_sha256":"b"*64,"canonical_state_sha256":"c"*64}}
        out["response_sha256"]=m3_client.canonical_hash(out); return out
    monkeypatch.setattr(m3_client,"call",applied)
    await service.reconcile(adapter,all_inflight=True)
    assert prepared.store.get(txn.id).state == "APPLIED"
    assert "Applied canonically on M3" in adapter.sent[-1][1]
