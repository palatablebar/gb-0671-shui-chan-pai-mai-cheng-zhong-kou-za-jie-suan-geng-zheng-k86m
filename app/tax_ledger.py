"""月度代扣税申报账本。

职责
----
* 把 `app.tax` 计算出的逐笔增量流水**幂等**地登记为已申报记录；
* 申报以账本中该户「已申报累计计税依据」为锚点，只对尚未申报的票/版本
  向后排并计算本次应扣/应退，因此同一更正重跑不会重复扣税或退款；
* 申报失败整批回滚，既有税单原样保留，失败另记日志；
* 为页面提供「采用的结算版本、累计计税依据、已申报税额、本次应扣应退」
  的账本回填视图。

账本核对不通过时该户暂缓，不申报、不扣税、不退款：
* 同一 entry_id 出现不同载荷（冲突）；
* 账本存在当期已申报流水，但当前输入里找不到对应事件（成交或付款数据缺失）；
* 已申报流水不是规范结算序的前缀（户级累计与票级结果对不上）。
"""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from app import tax as taxcalc
from app.coerce import money
from app.errors import BusinessError

SCHEMA = """
CREATE TABLE IF NOT EXISTS tax_ledger (
  entry_id TEXT PRIMARY KEY,
  payload_hash TEXT NOT NULL,
  run_id TEXT NOT NULL,
  owner_id TEXT NOT NULL,
  month TEXT NOT NULL,
  ticket TEXT NOT NULL,
  kind TEXT NOT NULL,
  from_version INTEGER,
  to_version INTEGER,
  settlement_amount TEXT NOT NULL,
  basis_before TEXT NOT NULL,
  basis_after TEXT NOT NULL,
  tax_before TEXT NOT NULL,
  tax_after TEXT NOT NULL,
  ticket_declared_tax TEXT NOT NULL,
  delta TEXT NOT NULL,
  decision TEXT NOT NULL,
  status TEXT NOT NULL,
  rule_version TEXT NOT NULL,
  applied_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tax_ledger_owner_month ON tax_ledger(owner_id, month);
CREATE TABLE IF NOT EXISTS tax_declare_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  month TEXT,
  status TEXT NOT NULL,
  detail TEXT NOT NULL,
  applied INTEGER NOT NULL DEFAULT 0,
  replayed INTEGER NOT NULL DEFAULT 0,
  skipped INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
"""

PAYLOAD_FIELDS = ('owner_id', 'month', 'ticket', 'kind', 'from_version', 'to_version',
                  'settlement_amount', 'basis_before', 'basis_after', 'tax_before',
                  'tax_after', 'ticket_declared_tax', 'delta', 'decision', 'rule_version')


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.executescript(SCHEMA)
    return con


def _now():
    return datetime.now(timezone.utc).isoformat()


def payload_hash(entry):
    payload = {k: entry.get(k) for k in PAYLOAD_FIELDS}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def declared_status(delta):
    value = Decimal(str(delta))
    return '已扣' if value > 0 else ('已退' if value < 0 else '零')


def load(path, month=None):
    with connect(path) as con:
        if month:
            rows = con.execute('SELECT * FROM tax_ledger WHERE month=?', (month,)).fetchall()
        else:
            rows = con.execute('SELECT * FROM tax_ledger').fetchall()
    return {row['entry_id']: dict(row) for row in rows}


def _log(con, run_id, month, status, detail, applied=0, replayed=0, skipped=0):
    con.execute('INSERT INTO tax_declare_log(run_id,month,status,detail,applied,replayed,skipped,created_at)'
                ' VALUES(?,?,?,?,?,?,?,?)',
                (run_id, month, status, detail, applied, replayed, skipped, _now()))


def _event_of(entry):
    return {'ticket': entry['ticket'], 'kind': entry['kind'],
            'from_version': entry.get('from_version'), 'to_version': entry.get('to_version'),
            'amount_before': entry['amount_before'], 'amount_after': entry['amount_after']}


def _q_declared(value):
    return Decimal(str(value)).quantize(Decimal('.01'))


def reconcile(state, rows):
    """以账本为已申报锚点，重算尚未申报流水的边际税，并回填核对字段。

    直接改写 state['tax']，返回
    {'pending': [...], 'replayed': [...], 'problems': {owner: [code]}}。
    """
    tax = state.get('tax') or {}
    rule = taxcalc.rule_from(state.get('context') or {})
    owners = {owner['owner_id']: owner for owner in tax.get('owners') or []}
    run_id = (state.get('manifest') or {}).get('run_id')

    canonical_all = [entry for entry in tax.get('entries') or [] if entry.get('kind') != 'blocked']
    blocked_all = [entry for entry in tax.get('entries') or [] if entry.get('kind') == 'blocked']
    final_entries = []
    pending_all = []
    replayed = []
    problems = {}

    for owner_id in sorted(owners):
        owner = owners[owner_id]
        canonical = [e for e in canonical_all if e['owner_id'] == owner_id]
        owner_rows = {eid: row for eid, row in rows.items() if row.get('owner_id') == owner_id}

        declared, pending = [], []
        declared_basis = Decimal('0')
        declared_tax = Decimal('0')
        declared_ticket = {}
        matched = set()
        for entry in canonical:
            row = owner_rows.get(entry['entry_id'])
            if row is not None and row.get('payload_hash') == payload_hash(entry):
                # 已申报流水保持原值，只作为累计锚点；不按规范序回写
                matched.add(entry['entry_id'])
                declared.append((entry, row))
                declared_basis += Decimal(str(entry['amount_after'])) - Decimal(str(entry['amount_before']))
                declared_tax += Decimal(str(row['delta']))
                declared_ticket[entry['ticket']] = (
                    declared_ticket.get(entry['ticket'], Decimal('0')) + Decimal(str(row['delta'])))
                replayed.append(entry['entry_id'])
            else:
                if row is not None:
                    problems.setdefault(owner_id, []).append('ENTRY_PAYLOAD_CONFLICT')
                pending.append(entry)

        if set(owner_rows) - matched:
            # 账本里有当前输入没有的流水：成交归集或付款流水丢失
            problems.setdefault(owner_id, []).append('OWNER_TICKET_MISMATCH')

        # 待申报流水从「已申报累计计税依据」这个锚点继续向后排，
        # 因此已申报税单不被改写，后续票也不会少扣或多退。
        anchored, basis_after = taxcalc.sequence_entries(
            owner_id, tax.get('month'), [_event_of(e) for e in pending], declared_basis, rule,
            declared_tax, declared_ticket)
        anchored_by_id = {e['entry_id']: e for e in anchored}

        for entry, row in declared:
            display = dict(entry)
            display.update({
                'basis_before': row['basis_before'], 'basis_after': row['basis_after'],
                'tax_before': row['tax_before'], 'tax_after': row['tax_after'],
                'ticket_declared_tax': row['ticket_declared_tax'],
                'delta': row['delta'], 'decision': row['decision'],
                'status': '暂缓' if owner.get('status') == '暂缓' else '已申报',
                'applied_status': row['status'], 'net_delta': '0.00',
                'replay': row.get('run_id') != run_id,
            })
            final_entries.append(display)
        for entry in pending:
            display = dict(anchored_by_id[entry['entry_id']])
            display['status'] = '暂缓' if owner.get('status') == '暂缓' else display['status']
            display['applied_status'] = '待申报'
            display['replay'] = False
            display['net_delta'] = display['delta']
            pending_all.append(display)
            final_entries.append(display)

        total_basis = basis_after
        total_tax = declared_tax + sum((Decimal(str(e['delta'])) for e in anchored), Decimal('0'))
        owner['declared_basis'] = money(declared_basis)
        owner['cumulative_basis'] = money(total_basis)
        owner['tax_computed'] = money(total_tax)
        owner['tax_withheld'] = money(declared_tax)
        owner['tax_pending'] = money(total_tax - declared_tax)
        owner['declared_tax'] = money(declared_tax)
        owner['pending_delta'] = money(total_tax - declared_tax)
        owner['net_payable'] = money(Decimal(owner['settlement_due']) - total_tax)
        owner['declared_status'] = ('已全部申报' if total_tax == declared_tax
                                    else ('部分申报' if declared_tax else '未申报'))

        if total_basis != declared_basis + sum(
                (Decimal(str(e['amount_after'])) - Decimal(str(e['amount_before'])) for e in pending),
                Decimal('0')):
            problems.setdefault(owner_id, []).append('OWNER_TICKET_MISMATCH')
        if _q_declared(declared_tax) != taxcalc.cumulative_tax(declared_basis, rule):
            problems.setdefault(owner_id, []).append('OWNER_TICKET_MISMATCH')
        if _q_declared(total_tax) != taxcalc.cumulative_tax(total_basis, rule):
            problems.setdefault(owner_id, []).append('OWNER_TICKET_MISMATCH')

        if owner_id in problems:
            codes = sorted(set(owner.get('hold_codes') or []) | set(problems[owner_id]))
            owner['status'] = '暂缓'
            owner['hold_codes'] = codes
            owner['hold_reason'] = '；'.join(
                taxcalc.HOLD_TEXT.get(code, '账本核对不符') for code in codes)
            owner['declared_status'] = '暂缓'
            for entry in final_entries:
                if entry['owner_id'] == owner_id:
                    entry['status'] = '暂缓'
                    entry['hold_codes'] = codes
                    entry['hold_reason'] = owner['hold_reason']

    final_entries.extend(blocked_all)
    final_entries.sort(key=lambda e: (e['owner_id'], str(e.get('kind') != 'initial'),
                                      e['ticket'], e.get('to_version') or 0))
    tax['entries'] = final_entries
    tax['owners'] = [owners[k] for k in sorted(owners)]
    pending_all = [e for e in pending_all if e['status'] != '暂缓']
    tax['ledger'] = {'declared': len(rows), 'pending': len(pending_all),
                     'replayed': len(replayed), 'problems': problems}
    return {'pending': pending_all, 'replayed': replayed, 'problems': problems}


def overlay(state, rows):
    """页面视图：在 reconcile 结果上标注重跑命中与待申报。"""
    result = reconcile(state, rows)
    state['tax_ledger'] = {
        'rows': rows,
        'pending': len(result['pending']),
        'replayed': len(result['replayed']),
        'problems': result['problems'],
    }
    return state['tax_ledger']


def declare(path, run_id, state):
    """把一期尚未申报的流水登记入账；幂等、整批失败回滚。

    任一船户暂缓或账本核对不符，整批拒绝申报；既有税单原样保留。
    """
    month = (state.get('tax') or {}).get('month')
    existing = load(path, month)
    result = reconcile(state, existing)
    pending = result['pending']
    held = [owner['owner_id'] for owner in (state.get('tax') or {}).get('owners') or []
            if owner.get('status') == '暂缓']
    if result['problems'] or held:
        raise BusinessError('TAX_HELD', '存在暂缓船户，本期不得申报：' + '、'.join(sorted(held)))
    if not pending:
        return {'run_id': run_id, 'month': month, 'status': '无待申报',
                'applied': 0, 'replayed': len(result['replayed']), 'skipped': 0,
                'held_owners': [], 'problems': {}, 'entries': []}

    con = connect(path)
    applied, replayed, skipped = [], [], []
    try:
        con.execute('BEGIN IMMEDIATE')
        for entry in pending:
            phash = payload_hash(entry)
            row = con.execute('SELECT payload_hash FROM tax_ledger WHERE entry_id=?',
                              (entry['entry_id'],)).fetchone()
            if row:
                if row['payload_hash'] != phash:
                    raise BusinessError('ENTRY_PAYLOAD_CONFLICT', '申报期间出现流水载荷冲突')
                replayed.append(entry['entry_id'])
                continue
            con.execute(
                'INSERT INTO tax_ledger(entry_id,payload_hash,run_id,owner_id,month,ticket,kind,'
                'from_version,to_version,settlement_amount,basis_before,basis_after,tax_before,'
                'tax_after,ticket_declared_tax,delta,decision,status,rule_version,applied_at)'
                ' VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (entry['entry_id'], phash, run_id, entry['owner_id'], entry['month'],
                 entry['ticket'], entry['kind'], entry.get('from_version'), entry.get('to_version'),
                 entry['settlement_amount'], entry['basis_before'], entry['basis_after'],
                 entry['tax_before'], entry['tax_after'], entry['ticket_declared_tax'],
                 entry['delta'], entry['decision'], declared_status(entry['delta']),
                 entry['rule_version'], _now()))
            applied.append(entry['entry_id'])
        for entry in (state.get('tax') or {}).get('entries') or []:
            if entry.get('status') == '暂缓' or entry.get('kind') == 'blocked':
                skipped.append(entry.get('entry_id'))
        _log(con, run_id, month, '成功', json.dumps({'problems': result['problems']},
                                                    ensure_ascii=False),
             len(applied), len(replayed), len(skipped))
        con.commit()
    except Exception as error:
        con.rollback()
        try:
            with connect(path) as log_con:
                _log(log_con, run_id, month, '失败', str(getattr(error, 'message', error)))
        except Exception:
            pass
        if isinstance(error, BusinessError):
            raise
        raise BusinessError('DECLARE_FAILED', '申报失败，原税单已保留') from error
    finally:
        con.close()

    owners = (state.get('tax') or {}).get('owners') or []
    return {
        'run_id': run_id, 'month': month, 'status': '成功',
        'applied': len(applied), 'replayed': len(replayed) + len(result['replayed']),
        'skipped': len(skipped),
        'held_owners': sorted(owner['owner_id'] for owner in owners if owner.get('status') == '暂缓'),
        'problems': result['problems'],
        'entries': applied + replayed,
    }
