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


def event(uid="1",chat="2",body="Y",update="4",message="5",reply=None,chat_type="dm"):
    return SimpleNamespace(text=body,source=SimpleNamespace(user_id=uid,chat_id=chat,chat_type=chat_type),platform_update_id=update,message_id=message,reply_to_message_id=reply)


def applied_response(request,event_id="event-1"):
    out={"version":1,"request_id":request["request_id"],"proposal_id":request["proposal_id"],"proposal_sha256":request["proposal_sha256"],"status":"APPLIED","event_id":event_id,"event_type":"SIGNAL_SET","process_receipt":{"receipt_id":event_id+".receipt.txt","receipt_event_id":event_id,"receipt_sha256":"b"*64,"canonical_state_sha256":"c"*64}}
    out["response_sha256"]=m3_client.canonical_hash(out)
    return out


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
        return applied_response(request)
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
async def test_non_private_chat_types_fail_closed(configured,monkeypatch):
    adapter=Adapter(); calls=[]
    async def called(*args,**kwargs): calls.append(1)
    monkeypatch.setattr(m3_client,"call",called)
    for chat_type in ("group","supergroup","channel"):
        assert await service.intercept(adapter,event(chat_type=chat_type,update=chat_type))
        with pytest.raises(service.GovernedError):
            service.prepare(adapter,event(body="prior",chat_type=chat_type),text())
    assert calls == []


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
        return applied_response(req,"recovered-event")
    monkeypatch.setattr(m3_client,"call",applied)
    await service.reconcile(adapter,all_inflight=True)
    assert prepared.store.get(txn.id).state == "APPLIED"
    assert "Applied canonically on M3" in adapter.sent[-1][1]


@pytest.mark.asyncio
async def test_prepared_cannot_be_approved(configured,monkeypatch):
    adapter=Adapter(); prepared=service.prepare(adapter,event(body="prior"),text()); calls=[]
    async def called(*args,**kwargs): calls.append(1)
    monkeypatch.setattr(m3_client,"call",called)
    assert prepared.store.get(prepared.proposal_id).state == "PREPARED"
    assert await service.intercept(adapter,event(update="prepared-y"))
    assert prepared.store.get(prepared.proposal_id).state == "PREPARED"
    assert calls == []


def test_failed_telegram_send_becomes_delivery_failed(configured):
    adapter=Adapter(); prepared=service.prepare(adapter,event(body="prior"),text())
    failed=SimpleNamespace(success=False,message_id=None,continuation_message_ids=(),raw_response={})
    assert not service.finalize(prepared,failed)
    assert prepared.store.get(prepared.proposal_id).state == "DELIVERY_FAILED"


@pytest.mark.asyncio
async def test_zero_eligible_rejects_without_m3(configured,monkeypatch):
    adapter=Adapter(); calls=[]
    async def called(*args,**kwargs): calls.append(1)
    monkeypatch.setattr(m3_client,"call",called)
    assert await service.intercept(adapter,event(update="zero"))
    assert calls == []
    assert "No single unexpired" in adapter.sent[-1][1]


@pytest.mark.asyncio
async def test_multiple_eligible_rejects_without_m3(configured,monkeypatch):
    adapter,prepared=prepare(configured); calls=[]
    with prepared.store.connect() as db:
        db.execute("INSERT INTO proposal SELECT 'second',state,created_at+1,updated_at,expires_at,bill_user_id,chat_id,source_update_id,source_message_id,'other-message',display_sha256,proposal_sha256,proposal_json,NULL,NULL,NULL,NULL,NULL,NULL,NULL FROM proposal WHERE id=?",(prepared.proposal_id,))
    async def called(*args,**kwargs): calls.append(1)
    monkeypatch.setattr(m3_client,"call",called)
    assert await service.intercept(adapter,event(update="multiple"))
    assert calls == []
    assert "No single unexpired" in adapter.sent[-1][1]


@pytest.mark.asyncio
async def test_duplicate_y_after_applied_creates_no_second_request(configured,monkeypatch):
    adapter,prepared=prepare(configured); requests=[]
    async def applied(req,timeout=12): requests.append(req); return applied_response(req)
    monkeypatch.setattr(m3_client,"call",applied)
    assert await service.intercept(adapter,event(update="first-y"))
    assert prepared.store.get(prepared.proposal_id).state == "APPLIED"
    assert await service.intercept(adapter,event(update="second-y",message="second-message"))
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_local_applied_failure_stays_reconcilable_then_announces_once(configured,monkeypatch):
    adapter,prepared=prepare(configured); requests=[]; original=service.Store.applied; attempts=[]
    async def applied(req,timeout=12): requests.append(req); return applied_response(req)
    def flaky(self,*args,**kwargs):
        attempts.append(1)
        return False if len(attempts)==1 else original(self,*args,**kwargs)
    monkeypatch.setattr(m3_client,"call",applied)
    monkeypatch.setattr(service.Store,"applied",flaky)
    assert await service.intercept(adapter,event(update="local-fail"))
    assert prepared.store.get(prepared.proposal_id).state == "INDETERMINATE"
    assert "could not durably record APPLIED" in adapter.sent[-1][1]
    assert not any(message.startswith("Applied canonically") for _,message,_ in adapter.sent)
    await service.reconcile(adapter,all_inflight=True)
    assert prepared.store.get(prepared.proposal_id).state == "APPLIED"
    assert len(requests) == 2 and requests[0]["request_id"] == requests[1]["request_id"]
    assert sum(message.startswith("Applied canonically") for _,message,_ in adapter.sent) == 1


@pytest.mark.asyncio
async def test_lost_response_reconciles_same_request_without_duplicate_mutation(configured,monkeypatch,tmp_path):
    from tests.gateway.signal_gate.test_endpoint_v0 import staged, run
    adapter,prepared=prepare(configured); endpoint,billos=staged(tmp_path); calls=[]
    async def lossy(req,timeout=12):
        calls.append(req["request_id"])
        result=run(endpoint,req)
        assert result.returncode == 0
        if len(calls)==1:
            raise m3_client.M3Error("simulated_lost_response")
        return json.loads(result.stdout)
    monkeypatch.setattr(m3_client,"call",lossy)
    assert await service.intercept(adapter,event(update="lost-response"))
    assert prepared.store.get(prepared.proposal_id).state == "INDETERMINATE"
    await service.reconcile(adapter,all_inflight=True)
    assert prepared.store.get(prepared.proposal_id).state == "APPLIED"
    assert calls[0] == calls[1]
    assert len(list((billos/"events").glob("SIGNAL_SET-*.yml"))) == 1
    assert len(list((billos/"receipts/process-event").glob("*.txt"))) == 1
    assert (billos/"mutation.log").read_text().splitlines() == ["mutation"]
