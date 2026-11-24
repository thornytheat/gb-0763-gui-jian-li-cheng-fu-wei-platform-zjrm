import sqlite3

import pytest

from app.archive.save import replay, retrieve
from app.inputs import sample
from app.pipeline import inspect

HEADER = 'event_id,run,kind,at,odo_mm,ref,grade'


def csv_of(*rows):
    return (HEADER + '\n' + '\n'.join(rows)).encode()


def ledger_of(raw):
    return inspect(raw, 'ledger')['ledger']


def test_every_row_has_exactly_one_conclusion():
    result = inspect(sample('dirty'), 'ledger')
    ledger, capture = result['ledger'], result['capture']
    lines = [row['line'] for row in ledger['dispositions']]
    assert len(lines) == len(set(lines)) == capture['input_rows'] == 28
    assert set(lines) == ({r['line'] for r in capture['rejects']}
                          | {e['line'] for e in capture['events']}
                          | {r['line'] for r in capture['repeated']})
    assert ledger['counts'] == {'matched': 10, 'projected': 6, 'unmapped': 4,
                                'discarded': 2, 'isolated': 5, 'duplicate': 1}


def test_dirty_causes_trace_to_defects():
    ledger = ledger_of(sample('dirty'))
    by_reason = {cause['reason']: cause for cause in ledger['causes']}
    assert ledger['open_causes'] == 9
    assert by_reason['SIGNAL_GAP']['affects'] == ['loss']
    assert by_reason['SIGNAL_GAP']['gaps'] == [{'chain': 'UP-B', 'start_mm': 2100000, 'end_mm': 2300000}]
    assert by_reason['BREAK']['affects'] == ['break']
    assert by_reason['BREAK']['owner'] == '区间校准岗'
    assert by_reason['UNKNOWN_MARK']['owner'] == '基点复核岗'
    assert by_reason['ID_CONFLICT']['sources'] == [25, 26]
    linked = {row['line']: row['cause'] for row in ledger['dispositions'] if row['cause']}
    assert linked[13] == by_reason['SIGNAL_GAP']['cause_id']
    assert linked[25] == linked[26] == by_reason['ID_CONFLICT']['cause_id']


def test_anchor_tie_owns_downstream_defects():
    raw = csv_of('b0,T1,anchor,2026-01-01T00:00:00Z,0,B0,3',
                 'x1,T1,anchor,2026-01-01T00:01:00Z,100000,B1,3',
                 'x2,T1,anchor,2026-01-01T00:01:30Z,100000,B1,3',
                 'b2,T1,anchor,2026-01-01T00:04:00Z,200000,B2,3',
                 'd1,T1,defect,2026-01-01T00:02:00Z,150000,擦伤,2')
    ledger = ledger_of(raw)
    tie = next(cause for cause in ledger['causes'] if cause['reason'] == 'ANCHOR_TIE')
    assert tie['affects'] == ['d1'] and tie['affect_lines'] == [6]
    assert tie['gaps'] == [{'chain': 'UP-B', 'start_mm': 2000000, 'end_mm': 2400000}]
    assert not [cause for cause in ledger['causes'] if cause['reason'] == 'SIGNAL_GAP']
    rows = {row['line']: row for row in ledger['dispositions']}
    assert rows[6]['cause'] == tie['cause_id'] and rows[6]['status'] == 'unmapped'
    assert rows[3]['cause'] == rows[4]['cause'] == tie['cause_id']


def test_bracketless_defect_attributes_to_nearby_lost_anchor():
    raw = csv_of('d0,T2,anchor,2026-01-01T00:00:00Z,0,D0,3',
                 'x1,T2,anchor,2026-01-01T00:01:00Z,100000,D1,3',
                 'x2,T2,anchor,2026-01-01T00:01:30Z,100000,D1,3',
                 'd9,T2,defect,2026-01-01T00:00:30Z,30000,裂纹,2')
    ledger = ledger_of(raw)
    tie = next(cause for cause in ledger['causes'] if cause['reason'] == 'ANCHOR_TIE')
    assert tie['affects'] == ['d9']
    assert not [cause for cause in ledger['causes'] if cause['reason'] == 'NO_BRACKET']


def test_late_arrival_rewrites_causes_and_impact():
    early = csv_of('b0,T3,anchor,2026-01-01T00:00:00Z,0,B0,3',
                   'b2,T3,anchor,2026-01-01T00:04:00Z,200000,B2,3',
                   'd1,T3,defect,2026-01-01T00:02:30Z,125000,擦伤,2')
    backfilled = csv_of('b0,T3,anchor,2026-01-01T00:00:00Z,0,B0,3',
                        'b2,T3,anchor,2026-01-01T00:04:00Z,200000,B2,3',
                        'd1,T3,defect,2026-01-01T00:02:30Z,125000,擦伤,2',
                        'b1,T3,anchor,2026-01-01T00:02:00Z,100000,B1,3')
    before = ledger_of(early)
    gap = next(cause for cause in before['causes'] if cause['reason'] == 'SIGNAL_GAP')
    assert gap['affects'] == ['d1']
    after = ledger_of(backfilled)
    assert not [cause for cause in after['causes'] if cause['reason'] == 'SIGNAL_GAP']
    row = next(row for row in after['dispositions'] if row['event_id'] == 'd1')
    assert row['status'] == 'projected'
    late = next(row for row in after['dispositions'] if row['event_id'] == 'b1')
    assert late['status'] == 'matched' and '迟到 120 s' in late['detail']


def test_cause_ids_stable_across_recompute():
    first = ledger_of(sample('dirty'))
    second = ledger_of(sample('dirty'))
    assert [cause['cause_id'] for cause in first['causes']] == [cause['cause_id'] for cause in second['causes']]


def test_rerun_adds_no_primary_causes(client):
    first = client.post('/api/runs', json={'sample': 'dirty'})
    second = client.post('/api/runs', json={'sample': 'dirty'})
    assert first.json['run_id'] == second.json['run_id']
    assert (first.json['reused'], second.json['reused']) == (False, True)
    assert first.json['summary']['open_causes'] == second.json['summary']['open_causes'] == 9
    assert len(client.get('/api/runs').json['runs']) == 1
    snapshot = client.get('/api/runs/' + first.json['run_id']).json
    assert len(snapshot['stages']['ledger']['causes']) == 9


def test_failed_archive_retry_keeps_records(archive_app):
    archive = archive_app.track_archive
    with archive.transaction() as db:
        db.execute("CREATE TRIGGER fail_run BEFORE INSERT ON runs BEGIN SELECT RAISE(ABORT, 'fault'); END")
    with pytest.raises(sqlite3.IntegrityError):
        replay(archive, sample('dirty'))
    with archive.transaction() as db:
        db.execute('DROP TRIGGER fail_run')
    result = replay(archive, sample('dirty'))
    assert not result['reused'] and result['summary']['open_causes'] == 9
    stored = retrieve(archive, result['run_id'])
    assert len(stored['stages']['ledger']['causes']) == 9


def test_ledger_page_and_api(client):
    page = client.get('/work/ledger').get_data(as_text=True)
    assert '主因' in page and '处置结论' in page
    assert client.get('/api/ledger?sample=dirty').json['open_causes'] == 9
    clean = client.post('/api/ledger', json={'sample': 'clean'}).json
    assert clean['open_causes'] == 0 and clean['counts'] == {'matched': 8, 'projected': 5}


def test_evidence_packet_carries_ledger(client):
    run_id = client.post('/api/runs', json={'sample': 'dirty'}).json['run_id']
    client.post(f'/api/runs/{run_id}/issue')
    packet = client.get(f'/api/runs/{run_id}/export').json['evidence']
    assert packet['ledger']['open_causes'] == 9
