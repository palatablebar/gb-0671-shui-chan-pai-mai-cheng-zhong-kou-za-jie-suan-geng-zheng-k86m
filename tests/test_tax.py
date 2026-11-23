import json
from decimal import Decimal

import pytest

from app import tax_ledger
from app.coerce import money
from app.config import ROOT
from app.engine import evaluate
from app.errors import BusinessError
from app.tax import cumulative_tax, rule_from, build_chains

CONTEXT = json.loads((ROOT / 'sample_data/context.json').read_text())


def _month_rule():
    return rule_from(CONTEXT)


def _clean_lines():
    return (ROOT / 'sample_data/clean.csv').read_text(encoding='utf-8-sig').splitlines()


def _without_v2_csv():
    lines = _clean_lines()
    kept = [line for line in lines[1:] if ',2,1,' not in line]
    return '\n'.join([lines[0]] + kept) + '\n'


def _by_owner(state, owner):
    return next(o for o in state['tax']['owners'] if o['owner_id'] == owner)


# ---------- 计税曲线与增量语义 ----------

def test_cumulative_tax_curve():
    rule = _month_rule()
    assert cumulative_tax('0', rule) == Decimal('0.00')
    assert cumulative_tax('3000', rule) == Decimal('0.00')
    assert cumulative_tax('5000', rule) == Decimal('20.00') - Decimal('10.00')
    assert cumulative_tax('12000', rule) == Decimal('60.00')
    assert cumulative_tax('15000', rule) == Decimal('90.00')


def test_dirty_owner_net_payable(sample):
    sh01 = _by_owner(sample, 'SH01')
    assert sh01['turnover'] == '11381.21'
    assert sh01['cumulative_basis'] == '11381.21'
    assert sh01['tax_computed'] == '53.81'
    assert sh01['tax_pending'] == '53.81'   # 纯计算尚未申报
    assert sh01['tax_withheld'] == '0.00'
    assert sh01['declared_status'] == '未申报'
    assert sh01['net_payable'] == '10158.90'
    assert sh01['status'] == '可支付'
    assert sample['tax']['status'] == '可结算'
    assert sample['tax']['checks']['money_balanced'] is True


def test_increments_telescope_to_month_total(sample):
    for owner in sample['tax']['owners']:
        entries = [e for e in sample['tax']['entries'] if e['owner_id'] == owner['owner_id']]
        total = sum((Decimal(e['delta']) for e in entries), Decimal('0'))
        assert money(total) == owner['tax_computed']
        assert owner['tax_computed'] == money(cumulative_tax(owner['cumulative_basis'], _month_rule()))


def test_correction_only_charges_margin(sample):
    correction = next(e for e in sample['tax']['entries']
                      if e['owner_id'] == 'SH01' and e['kind'] == 'correction')
    # 00001 从 1168.50 更正到 1285.35，跨档只补边际税
    assert correction['basis_before'] == '11264.36'
    assert correction['basis_after'] == '11381.21'
    assert correction['delta'] == '1.17'
    assert correction['decision'] == '应扣'


def test_entry_ids_stable_under_input_order():
    forward = evaluate({'dataset': 'dirty'})
    reverse = evaluate({'dataset': 'dirty'})
    ids = lambda s: sorted(e['entry_id'] for e in s['tax']['entries'])
    assert ids(forward) == ids(reverse)


# ---------- 暂缓条件 ----------

def test_missing_taxpayer_holds_owner():
    taxpayers = dict(CONTEXT['taxpayers'])
    taxpayers.pop('SH02')
    state = evaluate({'dataset': 'clean', 'overrides': {'taxpayers': taxpayers}})
    sh02 = _by_owner(state, 'SH02')
    assert sh02['status'] == '暂缓'
    assert 'MISSING_TAXPAYER' in sh02['hold_codes']
    assert state['tax']['status'] == '暂缓'


def test_invalid_taxpayer_holds_owner():
    taxpayers = json.loads(json.dumps(CONTEXT['taxpayers']))
    taxpayers['SH01']['status'] = '注销'
    state = evaluate({'dataset': 'clean', 'overrides': {'taxpayers': taxpayers}})
    assert _by_owner(state, 'SH01')['status'] == '暂缓'


def test_open_chain_blocks_with_evidence():
    lines = _clean_lines()
    stale_gross = '00001v3gross,00001,gross,130,kg,黄鱼,12.30,0.05,3,1,1168.50'
    stale_tare = '00001v3tare,00001,tare,15,kg,黄鱼,12.30,0.05,3,1,0'
    state = evaluate({'csv_text': '\n'.join(lines + [stale_gross, stale_tare]) + '\n'})
    sh01 = _by_owner(state, 'SH01')
    assert sh01['status'] == '暂缓'
    assert 'CHAIN_OPEN' in sh01['hold_codes']
    blocked = [e for e in state['tax']['entries'] if e['kind'] == 'blocked']
    assert blocked and blocked[0]['ticket'] == '00001'
    assert state['tax']['checks']['row_balanced'] is True  # 不丢行


def test_owner_ticket_mismatch_holds():
    state = evaluate({'dataset': 'clean'})
    # 人为制造户级与票级不一致：只改一个生效票金额
    state['final'][0]['amount'] = money(Decimal(state['final'][0]['amount']) + Decimal('1'))
    from app.tax import build
    build(state)
    assert any(o['status'] == '暂缓' for o in state['tax']['owners'])


# ---------- 申报账本：幂等、重跑、失败保留 ----------

def _declare_state(tmp_path, state):
    path = str(tmp_path / 'ledger.sqlite')
    return path, tax_ledger.declare(path, state['manifest']['run_id'], state)


def test_declare_is_idempotent(tmp_path, sample):
    path, first = _declare_state(tmp_path, sample)
    assert first['applied'] == 21
    again = tax_ledger.declare(path, sample['manifest']['run_id'], sample)
    assert again['applied'] == 0
    assert again['replayed'] == 21
    rows = tax_ledger.load(path, '2026-11')
    assert len(rows) == 21
    total = sum((Decimal(r['delta']) for r in rows.values()), Decimal('0'))
    assert money(total) == '117.09'


def test_after_declare_owner_shows_withheld(tmp_path):
    path = str(tmp_path / 'ledger.sqlite')
    state = evaluate({'dataset': 'dirty'})
    tax_ledger.declare(path, state['manifest']['run_id'], state)
    refreshed = evaluate({'dataset': 'dirty'})
    tax_ledger.overlay(refreshed, tax_ledger.load(path, '2026-11'))
    sh01 = _by_owner(refreshed, 'SH01')
    assert sh01['tax_withheld'] == '53.81'
    assert sh01['tax_pending'] == '0.00'
    assert sh01['declared_tax'] == '53.81'
    assert sh01['declared_status'] == '已全部申报'
    assert all(e['applied_status'] != '待申报' for e in refreshed['tax']['entries'])


def test_rerun_does_not_double_charge(tmp_path):
    path = str(tmp_path / 'ledger.sqlite')
    first = evaluate({'dataset': 'dirty'})
    tax_ledger.declare(path, first['manifest']['run_id'], first)
    rerun = evaluate({'dataset': 'dirty'})
    result = tax_ledger.declare(path, rerun['manifest']['run_id'], rerun)
    assert result['applied'] == 0
    assert len(tax_ledger.load(path, '2026-11')) == 21


def test_month_end_correction_keeps_declared_and_charges_margin(tmp_path):
    path = str(tmp_path / 'ledger.sqlite')
    early = evaluate({'csv_text': _without_v2_csv()})
    tax_ledger.declare(path, early['manifest']['run_id'], early)
    declared_before = tax_ledger.load(path, '2026-11')
    sh01_before = sum((Decimal(r['delta']) for r in declared_before.values()
                       if r['owner_id'] == 'SH01'), Decimal('0'))
    assert money(sh01_before) == '52.64'

    late = evaluate({'dataset': 'clean'})
    tax_ledger.overlay(late, tax_ledger.load(path, '2026-11'))
    sh01 = _by_owner(late, 'SH01')
    assert sh01['declared_tax'] == '52.64'      # 已申报税单不改写
    assert sh01['pending_delta'] == '1.17'       # 只补跨档边际
    assert sh01['declared_status'] == '部分申报'

    result = tax_ledger.declare(path, late['manifest']['run_id'], late)
    assert result['applied'] == 1
    rows = tax_ledger.load(path, '2026-11')
    for eid, row in declared_before.items():
        assert rows[eid]['delta'] == row['delta']  # 旧税单逐笔不变
    assert money(sum((Decimal(r['delta']) for r in rows.values()), Decimal('0'))) == '117.09'


def test_declare_refused_when_owner_held(tmp_path):
    taxpayers = dict(CONTEXT['taxpayers'])
    taxpayers.pop('SH02')
    state = evaluate({'dataset': 'clean', 'overrides': {'taxpayers': taxpayers}})
    path = str(tmp_path / 'ledger.sqlite')
    with pytest.raises(BusinessError) as error:
        tax_ledger.declare(path, state['manifest']['run_id'], state)
    assert error.value.code == 'TAX_HELD'
    assert tax_ledger.load(path, '2026-11') == {}


def test_declare_failure_keeps_existing_tax(tmp_path, monkeypatch):
    path = str(tmp_path / 'ledger.sqlite')
    first = evaluate({'csv_text': _without_v2_csv()})
    tax_ledger.declare(path, first['manifest']['run_id'], first)
    before = tax_ledger.load(path, '2026-11')

    late = evaluate({'dataset': 'clean'})

    class FailingConnection(tax_ledger.sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql.lstrip().upper().startswith('INSERT INTO TAX_LEDGER'):
                raise RuntimeError('disk full')
            return super().execute(sql, *args, **kwargs)

    def flaky(path_, *args, **kwargs):
        con = tax_ledger.sqlite3.connect(path_, factory=FailingConnection, timeout=15)
        con.row_factory = tax_ledger.sqlite3.Row
        return con

    monkeypatch.setattr(tax_ledger, 'connect', flaky)
    with pytest.raises(BusinessError) as error:
        tax_ledger.declare(path, late['manifest']['run_id'], late)
    assert error.value.code == 'DECLARE_FAILED'
    monkeypatch.undo()
    after = tax_ledger.load(path, '2026-11')
    assert after == before  # 原税单原样保留


def test_payload_conflict_holds_owner(tmp_path, sample):
    path = str(tmp_path / 'ledger.sqlite')
    tax_ledger.declare(path, sample['manifest']['run_id'], sample)
    tampered = evaluate({'dataset': 'dirty'})
    entry = next(e for e in tampered['tax']['entries']
                 if e['owner_id'] == 'SH01' and e['kind'] == 'initial' and e['delta'] != '0.00')
    entry['delta'] = money(Decimal(entry['delta']) + Decimal('5'))
    tax_ledger.overlay(tampered, tax_ledger.load(path, '2026-11'))
    assert _by_owner(tampered, 'SH01')['status'] == '暂缓'


def test_missing_ledger_row_holds_owner(tmp_path, sample):
    path = str(tmp_path / 'ledger.sqlite')
    tax_ledger.declare(path, sample['manifest']['run_id'], sample)
    # 模拟成交/付款流水丢失：从账本删掉一笔已申报流水，当前输入仍含该票
    with tax_ledger.connect(path) as con:
        row = con.execute("SELECT entry_id FROM tax_ledger WHERE owner_id='SH01' LIMIT 1").fetchone()
        con.execute('DELETE FROM tax_ledger WHERE entry_id=?', (row['entry_id'],))
    state = evaluate({'dataset': 'dirty'})
    tax_ledger.overlay(state, tax_ledger.load(path, '2026-11'))
    assert _by_owner(state, 'SH01')['status'] == '暂缓'


# ---------- 页面字段 ----------

def test_page_exposes_settlement_version_and_deltas(sample):
    entry = sample['tax']['entries'][0]
    for key in ('settlement_version', 'cumulative_basis' if False else 'basis_after',
                'ticket_declared_tax', 'delta', 'decision'):
        assert key in entry
    owner = sample['tax']['owners'][0]
    for key in ('declared_basis', 'cumulative_basis', 'declared_tax', 'pending_delta', 'net_payable'):
        assert key in owner
