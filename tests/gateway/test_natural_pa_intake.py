from types import SimpleNamespace

import pytest

from gateway.natural_pa_intake import service


def event(text="Save this dashboard idea. Make finishing it a task.", update=100):
    source = SimpleNamespace(platform=SimpleNamespace(value="telegram"), chat_type="dm", user_id="1", chat_id="1")
    return SimpleNamespace(text=text, source=source, platform_update_id=update, message_id=str(update), media_urls=[], media_types=[])


@pytest.mark.parametrize("ordinal,day", [("2nd", 2), ("12th", 12), ("20th", 20), ("21st", 21), ("22nd", 22), ("23rd", 23), ("26th", 26), ("31st", 31)])
def test_ordinal_dates_are_preserved(ordinal, day):
    assert service._parse_ordinal_date(f"the {ordinal} of August")["day"] == day


@pytest.mark.asyncio
async def test_non_action_keeps_baseline_route(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "settings", lambda: {"enabled": True, "bill_user_id": "1", "chat_id": "1", "database": str(tmp_path / "pa.sqlite")})
    async def propose(_):
        return {"actions": [], "clarification": None}
    monkeypatch.setattr(service, "_propose", propose)
    assert await service.intercept(event("I want to talk about canaries later")) is None


@pytest.mark.asyncio
async def test_multi_intent_uses_bounded_handlers_and_replays(monkeypatch, tmp_path):
    cfg = {"enabled": True, "bill_user_id": "1", "chat_id": "1", "database": str(tmp_path / "pa.sqlite")}
    monkeypatch.setattr(service, "settings", lambda: cfg)
    async def propose(_):
        return {"clarification": None, "actions": [
            {"kind": "capture", "title": "Dashboard idea", "text": "dashboard idea", "due_at": None, "person": None, "project": None, "next_action": None, "priorities": []},
            {"kind": "task", "title": "Finish dashboard", "text": "finish it", "due_at": None, "person": None, "project": "Christine", "next_action": "finish dashboard", "priorities": []},
            {"kind": "daily_priorities", "title": "Priorities", "text": "", "due_at": None, "person": None, "project": None, "next_action": None, "priorities": ["Finish dashboard"]},
        ]}
    monkeypatch.setattr(service, "_propose", propose)
    from gateway.raw_capture_gate import service as raw
    calls = []
    async def capture(ev):
        calls.append(ev.text)
        return "Captured canonically on M3 as cap_test. Receipt receipt.txt was verified."
    monkeypatch.setattr(raw, "intercept", capture)
    first = await service.intercept(event())
    second = await service.intercept(event())
    assert "Saved thought: cap_test" in first
    assert "Task created: Finish dashboard" in first
    assert "Today's priorities: Finish dashboard" in first
    assert second == first
    assert calls == ["/capture dashboard idea"]


@pytest.mark.asyncio
async def test_more_than_three_priorities_does_not_mutate(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "settings", lambda: {"enabled": True, "bill_user_id": "1", "chat_id": "1", "database": str(tmp_path / "pa.sqlite")})
    async def propose(_):
        return {"clarification": "Which three should control today?", "actions": []}
    monkeypatch.setattr(service, "_propose", propose)
    assert await service.intercept(event(update=101)) == "Which three should control today?"


@pytest.mark.asyncio
async def test_reminder_preserves_context_defaults_time_and_flags_calendar_conflict(monkeypatch, tmp_path):
    text = "Christine, remind me to book a flight to Adelaide. I need to do that tomorrow when I've got time. It's for Wednesday the 26th January."
    monkeypatch.setattr(service, "settings", lambda: {"enabled": True, "bill_user_id": "1", "chat_id": "1", "database": str(tmp_path / "pa.sqlite")})
    async def propose(_):
        return {"clarification": None, "actions": [{"kind": "reminder", "title": "Book a flight to Adelaide", "text": "Book a flight", "due_at": "2026-07-15T13:30:00+10:00", "person": None, "project": None, "next_action": None, "priorities": []}]}
    monkeypatch.setattr(service, "_propose", propose)
    monkeypatch.setattr(service, "_schedule_reminder", lambda action, event: "cron_test")
    result = await service.intercept(event(text, update=102))
    assert "2026-07-15T09:00:00+10:00 (time defaulted to 9:00 am Sydney)" in result
    assert "Do you mean Tuesday 26 January 2027 or Wednesday 27 January 2027?" in result
    from gateway.natural_pa_intake.store import Store
    row = Store(tmp_path / "pa.sqlite").db.execute("SELECT source_text,details_json FROM item").fetchone()
    assert row[0] == text
    assert '"stated_date": "2027-01-26"' in row[1]


@pytest.mark.asyncio
async def test_ambiguous_birthday_does_not_block_other_intentions_and_resumes(monkeypatch, tmp_path):
    long_text = """So Christine, what is the actual date then? I need to book that flight in December and what time does Virgin fly?
Secondly my three priorities for today:
1. I finished my PA testing so ensure you're working.
2. Move the observation app forward.
3. Set all the dashboard layout.
Can you remind me tomorrow to look at the dashboard setup? Can you remind me about my mother's birthday?"""
    cfg = {"enabled": True, "bill_user_id": "1", "chat_id": "1", "database": str(tmp_path / "pa.sqlite")}
    monkeypatch.setattr(service, "settings", lambda: cfg)
    async def propose(_):
        return {"clarification": "When is your mother's birthday?", "actions": []}
    monkeypatch.setattr(service, "_propose", propose)
    monkeypatch.setattr(service, "_schedule_reminder", lambda action, event: "cron_dashboard")
    import tools.web_tools
    monkeypatch.setattr(tools.web_tools, "web_search_tool", lambda query, limit=5: '{"success":true,"results":[]}')
    from gateway.natural_pa_intake.store import Store
    seed = Store(tmp_path / "pa.sqlite")
    seed.record_inbound(199, 199, "I need to book a flight for the second Tuesday in December.", None)
    seed.finish(199, {"reply": ""})
    first = await service.intercept(event(long_text, update=200))
    assert "Today's priorities:" in first
    assert "Dashboard" in first or "dashboard" in first
    assert "Tuesday 8 December 2026" in first
    assert "Flight research retained" in first
    assert "Need one clarification" in first
    assert "clar_" in first
    second = await service.intercept(event("It's on the 22nd of August but I need to be in Adelaide for that.", update=201))
    assert "22 August" in second
    assert "need to be in Adelaide" in second
    assert "When should I remind you" in second
    db = Store(tmp_path / "pa.sqlite").db
    assert [r[0] for r in db.execute("SELECT text FROM daily_priority ORDER BY position")] == [
        "I finished my PA testing so ensure you're working",
        "Move the observation app forward",
        "Set all the dashboard layout",
    ]
    assert db.execute("SELECT count(*) FROM action WHERE source_update_id='200'").fetchone()[0] == 5
    context = db.execute("SELECT context_json FROM pending_clarification WHERE kind='birthday'").fetchone()[0]
    assert '"day": 22' in context and "Adelaide" in context


@pytest.mark.asyncio
async def test_birthday_timing_reply_resolves_pending_before_fresh_intake(monkeypatch, tmp_path):
    cfg = {"enabled": True, "bill_user_id": "1", "chat_id": "1", "database": str(tmp_path / "pa.sqlite")}
    monkeypatch.setattr(service, "settings", lambda: cfg)
    async def should_not_propose(_):
        raise AssertionError("a matching pending clarification must be resumed first")
    monkeypatch.setattr(service, "_propose", should_not_propose)
    monkeypatch.setattr(service, "_schedule_reminder", lambda action, event: "cron_birthday")
    from gateway.natural_pa_intake.store import Store
    store = Store(tmp_path / "pa.sqlite")
    store.record_inbound(300, 300, "I need to be in Adelaide for my mother's birthday.", None)
    original_action = store.put_action(300, 0, "birthday", "needs_clarification", {"text": "birthday"})
    item_id = store.add_item("birthday", "Bill's mother's birthday", "birthday", {"birthday_date": {"day": 2, "month": "August"}}, 300, 0)
    pending_id = store.add_pending(300, original_action, "birthday", "When should I remind you to organise your mother's birthday on 2 August?", {"item_id": item_id, "birthday_date": {"day": 2, "month": "August"}})
    store.record_inbound(301, 301, "My mother's birthday is 2 August and I need to fly there the weekend before.", None)
    store.update_pending(pending_id, {"item_id": item_id, "birthday_date": {"day": 2, "month": "August"}}, "When should I remind you to organise your mother's birthday on 2 August?", 301)

    reply = await service.intercept(event("Do it on Friday this week.", update=302))
    assert "Reminder created for Friday" in reply
    assert "Adelaide" in reply and "2 August" in reply and "weekend before" in reply
    assert "rem_" in reply and "2026-07-17T09:00:00+10:00" in reply
    row = store.db.execute("SELECT state,reply_update_id FROM pending_clarification WHERE id=?", (pending_id,)).fetchone()
    assert tuple(row) == ("resolved", "302")
    reminder = store.db.execute("SELECT source_update_id,details_json FROM item WHERE kind='reminder'").fetchone()
    assert reminder[0] == "302"
    assert '"destination": "Adelaide"' in reminder[1] and '"travel_timing": "weekend before"' in reminder[1]

    recalled = await service.intercept(event("What did I just ask you to do on Friday?", update=303))
    assert "organise travel to Adelaide" in recalled
    assert "2 August" in recalled and "weekend before" in recalled
    assert "rem_" in recalled


@pytest.mark.asyncio
async def test_new_year_request_is_isolated_and_preserves_all_travel_intents(monkeypatch, tmp_path):
    text = ("Christine, what day is New Year's Christmas this year? I need to be in Switzerland the week before "
            "then I need to be in Sydney for New Year's Eve. Remind me to organize a trip, bookings, flying with "
            "Qantas. We should probably do that on Monday or Sunday.")
    cfg = {"enabled": True, "bill_user_id": "1", "chat_id": "1", "database": str(tmp_path / "pa.sqlite")}
    monkeypatch.setattr(service, "settings", lambda: cfg)
    async def contaminated_proposal(_):
        return {"clarification": None, "actions": [{"kind": "date_resolution", "title": "Old Adelaide date", "text": "second Tuesday in December", "due_at": "2026-12-08", "person": None, "project": "Adelaide", "next_action": None, "priorities": []}]}
    monkeypatch.setattr(service, "_propose", contaminated_proposal)
    import tools.web_tools
    monkeypatch.setattr(tools.web_tools, "web_search_tool", lambda query, limit=5: '{"success":true,"results":[]}')

    reply = await service.intercept(event(text, update=400))
    assert "8 December" not in reply and "Adelaide" not in reply
    assert "Christmas Day 2026 is Friday 25 December 2026" in reply
    assert "New Year's Eve 2026 is Thursday 31 December 2026" in reply
    assert "Flight research retained" in reply
    assert "Sunday 19 July or Monday 20 July" in reply
    from gateway.natural_pa_intake.store import Store
    store = Store(tmp_path / "pa.sqlite")
    source = store.db.execute("SELECT source_text FROM intake WHERE source_update_id='400'").fetchone()[0]
    assert source == text
    items = list(store.db.execute("SELECT kind,title,source_text,details_json FROM item WHERE source_update_id='400' ORDER BY action_index"))
    assert len(items) == 4
    assert all(row[2] == text for row in items)
    combined = " ".join(row[1] + " " + row[3] for row in items)
    assert "Switzerland" in combined and "Sydney" in combined and "Qantas" in combined
    pending = store.db.execute("SELECT state,context_json FROM pending_clarification WHERE source_update_id='400'").fetchone()
    assert pending[0] == "pending" and '"request_update_id": "400"' in pending[1]
