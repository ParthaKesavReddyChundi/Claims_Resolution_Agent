"""FastAPI Backend for Claims Resolution Agent Web Dashboard.

Provides REST endpoints wrapping the MCP tool server, the approval protocol,
the hash-chained ledger, and the policy corpus.
"""
import os
import sys
import uuid
import time
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
sys.path.insert(0, os.path.join(_PROJECT_ROOT, "server"))
sys.path.insert(0, os.path.join(_PROJECT_ROOT, "policy"))
sys.path.insert(0, os.path.join(_PROJECT_ROOT, "ledger"))

# Set secret key if not set
os.environ.setdefault("CRA_JWT_SIGNING_KEY", "cra-web-production-secret-2026")

import mcp_server as srv
import approval as ap
from fixtures import CLAIMS, CUSTOMER_REFUND_HISTORY
from ledger import get_ledger, Ledger
from policy_index import get_index

app = FastAPI(
    title="Claims Resolution Agent API",
    description="Secure, auditable automated refund & claims resolution system",
    version="1.0.0"
)

# CORS setup for Vite dev server
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Pydantic Models
class ClaimCreate(BaseModel):
    claim_id: Optional[str] = None
    order_id: str
    customer_id: str
    amount: float = Field(..., gt=0)
    currency: str = "USD"
    reason: str  # "damaged", "wrong_item", "not_as_described", etc.
    photo_on_file: bool = False
    chargeback_in_progress: bool = False
    customer_description: Optional[str] = None


class ResolveRequest(BaseModel):
    claim_id: str
    idempotency_key: Optional[str] = None
    approval_token: Optional[str] = None


class ApprovalSignRequest(BaseModel):
    approval_id: str
    approver_name: str


class ApprovalRequestInput(BaseModel):
    claim_id: str
    amount: float
    currency: str = "USD"
    required_approvers: int = 1


@app.get("/api/health")
def health_check():
    return {"status": "ok", "timestamp": time.time()}


@app.get("/api/claims")
def list_claims():
    led = get_ledger()
    results = []
    # Check if already executed in ledger
    rows = led.conn.execute(
        "SELECT payload, entry_hash, written_at FROM ledger WHERE tenant_id='claims-agent'"
    ).fetchall()
    
    for cid, c in sorted(CLAIMS.items()):
        executed_info = None
        for r in rows:
            if cid in r[0] and "refund_executed" in r[0]:
                executed_info = {"entry_hash": r[1], "written_at": r[2]}
                break

        results.append({
            "claim_id": cid,
            "order_id": c.get("order_id"),
            "customer_id": c.get("customer_id"),
            "amount": c.get("amount"),
            "currency": c.get("currency", "USD"),
            "reason": c.get("reason"),
            "photo_on_file": c.get("photo_on_file", False),
            "chargeback_in_progress": c.get("chargeback_in_progress", False),
            "customer_description": c.get("customer_description"),
            "executed_info": executed_info
        })
    return {"claims": results}


@app.post("/api/claims")
def create_claim(claim: ClaimCreate):
    cid = claim.claim_id
    if not cid:
        # Generate sequence ID e.g. CLM-80001
        num = 80000 + len(CLAIMS) + 1
        cid = f"CLM-{num}"
    elif not cid.startswith("CLM-") or len(cid) != 9:
        raise HTTPException(status_code=400, detail="Claim ID must match format CLM-#####")

    if cid in CLAIMS:
        raise HTTPException(status_code=400, detail=f"Claim ID {cid} already exists")

    CLAIMS[cid] = {
        "order_id": claim.order_id,
        "customer_id": claim.customer_id,
        "amount": claim.amount,
        "currency": claim.currency,
        "reason": claim.reason,
        "photo_on_file": claim.photo_on_file,
        "chargeback_in_progress": claim.chargeback_in_progress,
        "customer_description": claim.customer_description
    }

    if claim.customer_id not in CUSTOMER_REFUND_HISTORY:
        CUSTOMER_REFUND_HISTORY[claim.customer_id] = 0

    return {"message": "Claim registered successfully", "claim_id": cid, "claim": CLAIMS[cid]}


@app.get("/api/customers/{customer_id}")
def get_customer(customer_id: str):
    count = CUSTOMER_REFUND_HISTORY.get(customer_id, 0)
    return {
        "customer_id": customer_id,
        "approved_refunds_trailing_90d": count
    }


@app.post("/api/resolve")
def resolve_claim(req: ResolveRequest):
    cid = req.claim_id
    if cid not in CLAIMS:
        raise HTTPException(status_code=404, detail=f"Claim {cid} not found")

    claim = CLAIMS[cid]
    idem_key = req.idempotency_key or f"api-{cid}-{uuid.uuid4().hex[:8]}"

    # Execution trace records each step
    trace = []

    # Step 1: lookup_claim
    s1_resp = srv.dispatch({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "lookup_claim", "arguments": {"claim_id": cid}}
    })
    trace.append({"step": "lookup_claim", "name": "Lookup Claim", "data": s1_resp.get("result")})

    # Step 2: check_customer_history
    s2_resp = srv.dispatch({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "check_customer_history", "arguments": {"customer_id": claim["customer_id"]}}
    })
    trace.append({"step": "check_customer_history", "name": "Check Customer 90-Day History", "data": s2_resp.get("result")})

    # Step 3: search_policy_corpus
    query = f"refund {claim.get('reason')} threshold"
    s3_resp = srv.dispatch({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "search_policy_corpus", "arguments": {"query": query, "k": 2}}
    })
    trace.append({"step": "search_policy_corpus", "name": "Query Policy Corpus", "data": s3_resp.get("result")})

    # Step 4: execute_refund
    exec_args = {"claim_id": cid, "idempotency_key": idem_key}
    if req.approval_token:
        exec_args["approval_token"] = req.approval_token

    s4_resp = srv.dispatch({
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "execute_refund", "arguments": exec_args}
    })
    exec_res = s4_resp.get("result", {})
    trace.append({"step": "execute_refund", "name": "Execute Refund Safe Gateway", "data": exec_res})

    return {
        "claim_id": cid,
        "idempotency_key": idem_key,
        "status": exec_res.get("status"),
        "tier": exec_res.get("tier"),
        "reasons": exec_res.get("reasons"),
        "policy_clause": exec_res.get("policy_clause"),
        "required_approvers": exec_res.get("required_approvers"),
        "entry_hash": exec_res.get("entry_hash"),
        "trace": trace
    }


@app.get("/api/approvals")
def list_approvals():
    approvals_list = []
    for aid, rec in ap._APPROVALS.items():
        approvals_list.append({
            "approval_id": aid,
            "claim_id": rec.get("claim_id"),
            "required_approvers": rec.get("required_approvers"),
            "signatures": [s["approver_id"] if isinstance(s, dict) else s for s in rec.get("signatures", [])],
            "created_at": rec.get("created_at"),
            "expires_at": rec.get("expires_at"),
            "status": rec.get("status"),
            "token": rec.get("token")
        })
    return {"approvals": approvals_list}


@app.post("/api/approvals/request")
def create_approval_request(req: ApprovalRequestInput):
    record = ap.request_approval(req.claim_id, req.amount, req.currency, req.required_approvers)
    return {"message": "Approval record initialized", "record": record}


@app.post("/api/approvals/sign")
def sign_approval_request(req: ApprovalSignRequest):
    record = ap._APPROVALS.get(req.approval_id)
    if not record:
        raise HTTPException(status_code=404, detail="Approval record not found")

    try:
        token = ap.sign_approval(req.approval_id, req.approver_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    updated = ap._APPROVALS[req.approval_id]
    if token:
        updated["token"] = token

    return {
        "approval_id": req.approval_id,
        "status": updated["status"],
        "signatures": [s["approver_id"] if isinstance(s, dict) else s for s in updated.get("signatures", [])],
        "token": token,
        "ready": bool(token)
    }


@app.get("/api/ledger")
def get_ledger_entries():
    led = get_ledger()
    rows = led.conn.execute(
        "SELECT seq, tenant_id, payload, prev_hash, entry_hash, written_at FROM ledger ORDER BY seq ASC"
    ).fetchall()

    entries = []
    for r in rows:
        import json
        payload_obj = json.loads(r[2])
        entries.append({
            "seq": r[0],
            "tenant_id": r[1],
            "payload": payload_obj,
            "prev_hash": r[3],
            "entry_hash": r[4],
            "written_at": r[5]
        })

    # Validate chain integrity
    integrity = {"valid": True, "entries_checked": len(entries)}
    for i in range(1, len(entries)):
        if entries[i]["prev_hash"] != entries[i - 1]["entry_hash"]:
            integrity = {
                "valid": False,
                "first_bad_seq": entries[i]["seq"],
                "reason": "Mismatched prev_hash link"
            }
            break

    return {"entries": entries, "integrity": integrity}


@app.post("/api/ledger/tamper-demo")
def simulate_tamper_demo():
    """Simulates what happens when a database row is maliciously edited in storage."""
    tenant = "claims-agent"
    temp_led = Ledger(":memory:")
    for i in range(4):
        temp_led.append(tenant, {
            "event": "refund_executed",
            "claim_id": f"CLM-9010{i}",
            "amount": 50.0 + i,
            "idempotency_key": f"tamper-test-{i}"
        })

    # 1. Untampered verification
    pre_tamper = temp_led.verify(tenant)

    # 2. Maliciously tamper entry seq=2 in SQLite storage
    temp_led._corrupt_for_test(
        tenant, seq=2,
        new_payload={
            "event": "refund_executed",
            "claim_id": "CLM-90102",
            "amount": 9999.00,
            "idempotency_key": "tamper-test-2"
        }
    )

    # 3. Post-tamper verification
    post_tamper = temp_led.verify(tenant)
    return {
        "pre_tamper_validation": pre_tamper,
        "tampered_action": "Altered entry seq=2 amount from $52.00 to $9,999.00 in SQLite storage",
        "post_tamper_validation": post_tamper
    }


@app.get("/api/policy/search")
def search_policy(q: str = Query(..., min_length=1), k: int = 4):
    idx = get_index()
    results = idx.search(q, k=k)
    return {
        "query": q,
        "results": [
            {
                "locator": r["locator"],
                "text": r["text"],
                "bm25_score": r.get("bm25_score"),
                "dense_score": r.get("dense_score"),
                "combined_score": r.get("combined_score")
            }
            for r in results
        ]
    }


# Serve static build of React UI if available
_DIST_DIR = os.path.join(_PROJECT_ROOT, "frontend", "dist")
if os.path.exists(_DIST_DIR):
    app.mount("/assets", StaticFiles(directory=os.path.join(_DIST_DIR, "assets")), name="assets")

    @app.get("/")
    def serve_root():
        index_file = os.path.join(_DIST_DIR, "index.html")
        if os.path.exists(index_file):
            return FileResponse(index_file)
        return {"error": "Frontend build not found"}

    @app.get("/{full_path:path}")
    def serve_frontend(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail=f"API route not found: /{full_path}")
        file_path = os.path.join(_DIST_DIR, full_path)
        if os.path.isfile(file_path):
            return FileResponse(file_path)
        index_file = os.path.join(_DIST_DIR, "index.html")
        if os.path.exists(index_file):
            return FileResponse(index_file)
        return {"error": "Frontend build not found"}


if __name__ == "__main__":
    import uvicorn
    print("Starting Claims Resolution Agent Server at http://127.0.0.1:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000)
