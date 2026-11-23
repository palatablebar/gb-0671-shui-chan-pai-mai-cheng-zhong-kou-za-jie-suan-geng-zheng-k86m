from fastapi import APIRouter, Request, Body
from fastapi.responses import JSONResponse
from app.engine import evaluate
from app.errors import BusinessError
from app.web import templates
from app.config import META
from app import store, tax_ledger, main

router = APIRouter()


def _db_path():
    return main.DB_PATH


def _load_state(request: Request):
    db_path = _db_path()
    run_id = request.query_params.get('run_id')
    if run_id:
        state = store.get(db_path, run_id)
    else:
        state = store.save(db_path, evaluate({'dataset': request.query_params.get('dataset', 'dirty')}))
    if state.get('tax'):
        tax_ledger.overlay(state, tax_ledger.load(db_path, state['tax'].get('month')))
    return state


@router.get('/tax')
def page(request: Request):
    state = _load_state(request)
    return templates.TemplateResponse(request=request, name='tax.html',
                                      context={'meta': META, 'key': 'tax', 'state': state,
                                               'view': state['views']['tax']})


@router.post('/api/tax')
def calculate(payload: dict = Body(default={})):
    result = evaluate(payload)
    if result.get('tax'):
        tax_ledger.overlay(result, tax_ledger.load(_db_path(), result['tax'].get('month')))
    return result


@router.post('/api/tax/declare')
def declare(payload: dict = Body(default={})):
    db_path = _db_path()
    run_id = payload.get('run_id')
    if not run_id:
        raise BusinessError('BAD_RUN_ID', '需要批次号')
    state = store.get(db_path, run_id)
    tax = state.get('tax') or {}
    if not tax:
        raise BusinessError('INCOMPLETE_RUN', '该批次尚未生成代扣税结果')
    tax_ledger.reconcile(state, tax_ledger.load(db_path, tax.get('month')))
    if any(owner.get('status') == '暂缓' for owner in tax.get('owners') or []):
        raise BusinessError('TAX_HELD', '存在暂缓船户，本期不得申报')
    return JSONResponse(tax_ledger.declare(db_path, run_id, state))


@router.get('/api/tax/ledger')
def ledger(month: str = None):
    rows = tax_ledger.load(_db_path(), month)
    return sorted(rows.values(), key=lambda r: (r['owner_id'], r['ticket'], r['to_version'] or 0))


@router.get('/api/tax/declarations')
def declarations(limit: int = 50):
    with tax_ledger.connect(_db_path()) as con:
        rows = con.execute('SELECT * FROM tax_declare_log ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
    return [dict(row) for row in rows]
