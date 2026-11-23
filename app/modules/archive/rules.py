import hashlib, json

def archive_decision(state):
    tax=state.get('tax') or {}
    tax_ready=tax.get('status')=='可结算'
    ready=(state['balance']['row_balanced'] and state['balance']['money_balanced'] and not state['balance']['blocked'] and tax_ready)
    payload={'final':state['final'],'tax':[{k:v for k,v in row.items() if k!='net_delta'} for row in tax.get('entries',[])]}
    seal=hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    return {'seal':seal,'status':'可归档' if ready else '待处理','tickets':len(state['final']),'reasons':len(state['quarantine']),'tax_status':tax.get('status','未计税')}

def apply(state):
    state['archive']=archive_decision(state)
    state['views']['archive']={'title':'结算封存','rows':state['version_audit'],'metrics':{'封存票':state['archive']['tickets'],'隔离证据':len(state['quarantine']),'代扣税':state['archive']['tax_status'],'状态':state['archive']['status']}}
