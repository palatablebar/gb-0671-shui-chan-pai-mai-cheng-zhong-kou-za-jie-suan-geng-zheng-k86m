"""按月为船户累计代扣经营税。

核心模型
--------
对船户当月成交额做累计计税：T(x) 为「当月累计计税依据 x」下的应扣税额，
免税额度随当月全部票累计消耗，税档按累进税率作用在 (x - 免税额度) 上。

票级与户级的关系不是把整月重算一遍，而是沿「结算序」逐笔登记增量：

    本次应扣/应退 = T(登记后累计计税依据) - T(登记前累计计税依据)

结算序 = 先全部基版（票号升序），再月末更正批（票号、版本升序）。
申报时以账本中该户「已申报累计计税依据」为起点，只对尚未申报的票/版本继续
向后排，因此：

* 已申报的历史税额不会被改写；
* 月末更正跨档的影响由一条增量流水承接，只影响本票及其后的位置；
* 后续新票从账本锚定的累计位置继续计算，不会少扣或多退；
* 增量之和恰好等于 T(月末累计)（T 先量化到分，差分会精确望远镜求和）。

任何一户出现下列情况时，该户当月整体暂缓，不申报、不扣税、不退款：
* 缺少有效纳税人身份；
* 更正链未闭合（缺基版 / 陈旧分支）；
* 户级累计计税依据与票级结果对不上。
"""

import hashlib
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP

from app.coerce import money

ZERO = Decimal('0')
CENT = Decimal('.01')
UNASSIGNED = '__unassigned__'

HOLD_TEXT = {
    'MISSING_TAXPAYER': '缺少有效纳税人身份',
    'INVALID_TAXPAYER': '纳税人身份无效',
    'OWNER_UNASSIGNED': '票号未归属船户',
    'CHAIN_OPEN': '更正链未闭合',
    'OWNER_TICKET_MISMATCH': '户级累计与票级结果不符',
}

VALID_STATUS = {'有效', '有效期内', 'VALID', 'ACTIVE', 'active', '正常'}


def _d(value, default='0'):
    if value is None or value == '':
        return Decimal(default)
    return Decimal(str(value))


def _q(value):
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def rule_from(context):
    context = context or {}
    tax = context.get('tax') or {}
    brackets = []
    for item in tax.get('brackets') or []:
        upto = item.get('upto')
        brackets.append({
            'upto': None if upto in (None, '') else _d(upto),
            'rate': _d(item.get('rate', '0')),
        })
    return {
        'month': str(tax.get('month') or context.get('month') or '2026-11'),
        'exemption': _d(tax.get('exemption', '0')),
        'brackets': brackets,
        'rule_version': str(tax.get('rule_version') or 'tax-v1'),
    }


def cumulative_tax(basis, rule):
    """T(累计计税依据)：先扣免税额度，再按累进档计算，结果量化到分。"""
    taxable = _d(basis) - rule['exemption']
    if taxable <= 0:
        return Decimal('0.00')
    total = ZERO
    low = ZERO
    brackets = rule['brackets']
    for bracket in brackets:
        upper = bracket['upto'] if bracket['upto'] is not None else taxable
        if upper <= low:
            low = upper
            continue
        span = min(taxable, upper) - low
        if span > 0:
            total += span * bracket['rate']
        low = upper
        if taxable <= low:
            break
    if brackets and taxable > low:  # 税档表未以 null 收尾时，超出部分沿用最高档
        total += (taxable - low) * brackets[-1]['rate']
    return _q(total)


def entry_id(owner, month, ticket, frm, to, amount_before, amount_after, rule_version):
    key = '|'.join([
        str(owner), str(month), str(ticket), '' if frm is None else str(frm),
        str(to), str(amount_before), str(amount_after), str(rule_version),
    ])
    return hashlib.sha256(key.encode('utf-8')).hexdigest()[:24]


def owner_of(row, context):
    explicit = str(row.get('owner') or '').strip()
    if explicit:
        return explicit
    mapping = (context or {}).get('ticket_owners') or {}
    return str(mapping.get(str(row.get('ticket', '')), '') or '').strip() or None


def taxpayer_state(owner, context):
    if not owner or owner == UNASSIGNED:
        return False, 'OWNER_UNASSIGNED'
    record = ((context or {}).get('taxpayers') or {}).get(owner)
    if not record:
        return False, 'MISSING_TAXPAYER'
    tax_id = str(record.get('tax_id') or '').strip()
    if len(tax_id) < 8:
        return False, 'MISSING_TAXPAYER'
    if str(record.get('status') or '').strip() not in VALID_STATUS:
        return False, 'INVALID_TAXPAYER'
    return True, None


def build_chains(settlements):
    """按票重放版本链，返回 {票号: {rows, ok, reason}}。

    只接受「第一版 replaces 为空、后续每版恰好替代当前生效版」的闭合链。
    """
    grouped = defaultdict(list)
    for row in settlements:
        grouped[str(row['ticket'])].append(row)
    chains = {}
    for ticket, rows in grouped.items():
        rows = sorted(rows, key=lambda r: (int(r['version']), str(r.get('kind', ''))))
        current = None
        ok, reason = True, None
        for row in rows:
            target = str(row.get('replaces') or '').strip()
            if current is None:
                if target:
                    ok, reason = False, 'MISSING_BASE'
                    break
            elif target != str(current['version']):
                ok, reason = False, 'STALE_CORRECTION'
                break
            current = row
        chains[ticket] = {'rows': rows, 'ok': ok, 'reason': reason}
    return chains


def ticket_events(chains, tickets):
    """生成某户的规范结算序：先全部基版，再月末更正批。"""
    events = []
    for ticket in tickets:
        info = chains.get(ticket)
        if not info or not info['ok']:
            continue
        base = info['rows'][0]
        events.append({
            'ticket': ticket, 'kind': 'initial', 'from_version': None,
            'to_version': int(base['version']),
            'amount_before': '0.00', 'amount_after': money(base['amount']),
        })
    for ticket in tickets:
        info = chains.get(ticket)
        if not info or not info['ok']:
            continue
        rows = info['rows']
        for previous, current in zip(rows, rows[1:]):
            events.append({
                'ticket': ticket, 'kind': 'correction',
                'from_version': int(previous['version']), 'to_version': int(current['version']),
                'amount_before': money(previous['amount']), 'amount_after': money(current['amount']),
            })
    return events


def event_entry_id(owner, month, event, rule):
    return entry_id(owner, month, event['ticket'], event['from_version'], event['to_version'],
                    event['amount_before'], event['amount_after'], rule['rule_version'])


def sequence_entries(owner, month, events, start_basis, rule, start_declared=ZERO,
                     declared_tickets=None):
    """从某一累计计税依据起点，沿结算序生成逐笔增量流水。"""
    declared_tickets = dict(declared_tickets or {})
    basis = _d(start_basis)
    declared = _d(start_declared)
    out = []
    for event in events:
        before = basis
        after = basis + _d(event['amount_after']) - _d(event['amount_before'])
        tax_before = cumulative_tax(before, rule)
        tax_after = cumulative_tax(after, rule)
        delta = tax_after - tax_before
        ticket_prior = declared_tickets.get(event['ticket'], ZERO)
        eid = event_entry_id(owner, month, event, rule)
        out.append({
            'entry_id': eid, 'owner_id': owner, 'month': month, 'ticket': event['ticket'],
            'kind': event['kind'], 'from_version': event['from_version'],
            'to_version': event['to_version'],
            'settlement_version': event['to_version'],
            'settlement_amount': event['amount_after'],
            'amount_before': money(event['amount_before']),
            'amount_after': money(event['amount_after']),
            'basis_before': money(before), 'basis_after': money(after),
            'tax_before': money(tax_before), 'tax_after': money(tax_after),
            'declared_before': money(declared),
            'ticket_declared_tax': money(ticket_prior),
            'delta': money(delta),
            'decision': '应扣' if delta > 0 else ('应退' if delta < 0 else '零'),
            'status': '待申报', 'hold_codes': [], 'hold_reason': '',
            'applied': False, 'replay': False, 'rule_version': rule['rule_version'],
        })
        basis = after
        declared += delta
        declared_tickets[event['ticket']] = ticket_prior + delta
    return out, basis


def _blocked_entry(owner, month, ticket, chain, rule):
    last = chain['rows'][-1]
    return {
        'entry_id': entry_id(owner, month, ticket, None, last['version'],
                             '', '', rule['rule_version']),
        'owner_id': owner, 'month': month, 'ticket': ticket, 'kind': 'blocked',
        'from_version': None, 'to_version': int(last['version']),
        'settlement_version': int(last['version']), 'settlement_amount': money(last['amount']),
        'amount_before': '0.00', 'amount_after': '0.00',
        'basis_before': '', 'basis_after': '', 'tax_before': '', 'tax_after': '',
        'declared_before': '', 'ticket_declared_tax': '',
        'delta': '0.00', 'decision': '暂缓',
        'status': '暂缓', 'hold_codes': ['CHAIN_OPEN'], 'hold_reason': '',
        'applied': False, 'replay': False, 'rule_version': rule['rule_version'],
        'block_reason': chain['reason'],
    }


def _new_summary(owner, rule):
    return {
        'owner_id': owner, 'month': rule['month'], 'taxpayer_valid': True,
        'ticket_count': 0, 'turnover': ZERO, 'paid': ZERO, 'settlement_due': ZERO,
        'cumulative_basis': ZERO, 'exemption': rule['exemption'],
        'tax_computed': ZERO, 'tax_withheld': ZERO, 'tax_pending': ZERO,
        'declared_basis': ZERO, 'declared_tax': ZERO, 'pending_delta': ZERO,
        'declared_status': '未申报',
        'net_payable': ZERO, 'status': '可支付', 'hold_codes': [],
        'hold_reason': '', 'rule_version': rule['rule_version'],
    }


def build(state):
    context = state.get('context') or {}
    rule = rule_from(context)
    final = state.get('final') or []
    settlements = state.get('settlements') or []
    conflicts = state.get('version_conflicts') or []
    conflict_tickets = {str(c.get('ticket')) for c in conflicts}
    chains = build_chains(settlements)

    ticket_owner = {ticket: (owner_of({'ticket': ticket}, context) or UNASSIGNED)
                    for ticket in chains}

    summaries = {owner: _new_summary(owner, rule) for owner in set(ticket_owner.values())}
    hold_codes = defaultdict(set)
    blocked_tickets = {t for t, info in chains.items() if not info['ok'] or t in conflict_tickets}

    # 1) 更正链闭合与纳税人身份
    for ticket in blocked_tickets:
        hold_codes[ticket_owner.get(ticket, UNASSIGNED)].add('CHAIN_OPEN')
    for owner in summaries:
        ok, reason = taxpayer_state(owner, context)
        summaries[owner]['taxpayer_valid'] = ok
        if not ok:
            hold_codes[owner].add(reason)

    # 2) 户级成交额与已付（只取生效版本，与应付对账一致）
    final_by_ticket = {str(row['ticket']): row for row in final}
    for row in final:
        ticket = str(row['ticket'])
        owner = ticket_owner.get(ticket) or owner_of(row, context) or UNASSIGNED
        ticket_owner.setdefault(ticket, owner)
        if owner not in summaries:
            summaries[owner] = _new_summary(owner, rule)
            ok, reason = taxpayer_state(owner, context)
            summaries[owner]['taxpayer_valid'] = ok
            if not ok:
                hold_codes[owner].add(reason)
        summaries[owner]['turnover'] += _d(row['amount'])
        summaries[owner]['paid'] += _d(row['paid'])
        summaries[owner]['ticket_count'] += 1

    # 3) 规范结算序 → 逐笔增量流水（纯计算视角：账本为空）
    owner_events = {}
    entries = []
    for owner in sorted(summaries):
        tickets = sorted(t for t, o in ticket_owner.items() if o == owner)
        events = ticket_events(chains, tickets)
        owner_events[owner] = events
        owner_entries, basis = sequence_entries(owner, rule['month'], events, ZERO, rule)
        entries.extend(owner_entries)
        summary = summaries[owner]
        summary['cumulative_basis'] = basis
        summary['tax_computed'] = _q(sum((_d(e['delta']) for e in owner_entries), ZERO))
        summary['settlement_due'] = _q(summary['turnover'] - summary['paid'])

    # 4) 被阻断的票：留证据、挂暂缓、不动税基
    for ticket in sorted(blocked_tickets):
        owner = ticket_owner.get(ticket, UNASSIGNED)
        if owner not in summaries:
            summaries[owner] = _new_summary(owner, rule)
        entries.append(_blocked_entry(owner, rule['month'], ticket, chains[ticket], rule))

    # 5) 户级累计 vs 票级结果
    for owner, summary in summaries.items():
        chain_total = ZERO
        for ticket, ticket_owner_id in ticket_owner.items():
            if ticket_owner_id != owner:
                continue
            info = chains.get(ticket)
            if not info or not info['ok']:
                continue
            expected = info['rows'][-1]
            actual = final_by_ticket.get(ticket)
            if actual is None or money(expected['amount']) != money(actual['amount']):
                hold_codes[owner].add('OWNER_TICKET_MISMATCH')
            chain_total += _d(expected['amount'])
        event_total = sum((_d(e['amount_after']) - _d(e['amount_before'])
                           for e in owner_events.get(owner, [])), ZERO)
        if (_q(chain_total) != _q(summary['turnover'])
                or _q(event_total) != _q(summary['turnover'])
                or _q(summary['cumulative_basis']) != _q(summary['turnover'])
                or _q(summary['tax_computed']) != cumulative_tax(summary['cumulative_basis'], rule)):
            hold_codes[owner].add('OWNER_TICKET_MISMATCH')

    # 6) 暂缓落到户与流水，计算可支付净额
    for owner, summary in summaries.items():
        codes = sorted(hold_codes.get(owner, set()))
        held = bool(codes)
        summary['hold_codes'] = codes
        summary['hold_reason'] = '；'.join(HOLD_TEXT.get(c, c) for c in codes)
        summary['status'] = '暂缓' if held else '可支付'
        # 纯计算视角尚未申报：应扣税全部待申报，已扣为 0
        summary['tax_withheld'] = ZERO
        summary['tax_pending'] = summary['tax_computed']
        summary['declared_tax'] = ZERO
        summary['declared_basis'] = ZERO
        summary['pending_delta'] = summary['tax_computed']
        summary['declared_status'] = '暂缓' if held else '未申报'
        summary['net_payable'] = _q(summary['settlement_due'] - summary['tax_computed'])
        for key in ('turnover', 'paid', 'settlement_due', 'cumulative_basis', 'exemption',
                    'tax_computed', 'tax_withheld', 'tax_pending', 'net_payable',
                    'declared_basis', 'declared_tax', 'pending_delta'):
            summary[key] = money(summary[key])

    for entry in entries:
        summary = summaries[entry['owner_id']]
        entry['status'] = '暂缓' if summary['status'] == '暂缓' else '待申报'
        entry['hold_codes'] = summary['hold_codes']
        entry['hold_reason'] = summary['hold_reason']

    # 7) 守恒校验：成交归集与票级结果不得丢失
    valid_versions = sum(len(info['rows']) for info in chains.values() if info['ok'])
    blocked_versions = sum(len(info['rows']) for info in chains.values() if not info['ok'])
    owner_basis = sum((_d(s['cumulative_basis']) for s in summaries.values()), ZERO)
    owner_turnover = sum((_d(s['turnover']) for s in summaries.values()), ZERO)
    unaccounted = len(settlements) - valid_versions - blocked_versions
    checks = {
        'settlement_rows': len(settlements),
        'ledger_rows': len(entries),
        'tickets': len(chains),
        'blocked_tickets': len(blocked_tickets),
        'blocked_versions': blocked_versions,
        'unaccounted_rows': unaccounted,
        'row_balanced': unaccounted == 0,
        'basis_balanced': _q(owner_basis) == _q(owner_turnover),
    }
    checks['tax_balanced'] = all(
        _q(_d(s['tax_computed'])) == cumulative_tax(_d(s['cumulative_basis']), rule)
        for s in summaries.values())
    checks['money_balanced'] = checks['basis_balanced'] and checks['tax_balanced']
    state['tax'] = {
        'month': rule['month'],
        'rule_version': rule['rule_version'],
        'exemption': money(rule['exemption']),
        'brackets': [{'upto': None if b['upto'] is None else money(b['upto']),
                      'rate': str(b['rate'])} for b in rule['brackets']],
        'entries': entries,
        'owners': [summaries[k] for k in sorted(summaries)],
        'owner_events': owner_events,
        'checks': checks,
        'status': '可结算' if (checks['money_balanced'] and checks['row_balanced']
                            and all(s['status'] == '可支付' for s in summaries.values())) else '暂缓',
    }
    state['views']['tax'] = {
        'title': '月度代扣税',
        'rows': entries,
        'metrics': {
            '船户': len(summaries),
            '计税流水': len(entries),
            '代扣税_CNY': money(sum((_d(s['tax_withheld']) for s in summaries.values()), ZERO)),
            '暂缓户': len([s for s in summaries.values() if s['status'] == '暂缓']),
        },
    }
    return state
