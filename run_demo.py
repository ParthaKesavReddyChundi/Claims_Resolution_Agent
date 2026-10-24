"""Interactive Claims Resolution Agent Demo & Runner.

Run this script to walk through claims resolution, see the MCP tool calls,
watch policy enforcement in action, simulate manager approvals, and inspect
the hash-chained audit ledger.
"""
import os
import sys
import uuid

# Ensure server, policy, and ledger are in import path
_BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_BASE, "server"))
sys.path.insert(0, os.path.join(_BASE, "policy"))
sys.path.insert(0, os.path.join(_BASE, "ledger"))

if sys.stdout.encoding != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

import mcp_server as srv
import approval as ap
from fixtures import CLAIMS, CUSTOMER_REFUND_HISTORY
from ledger import get_ledger


def format_box(title: str, text: str = ""):
    line = "=" * 65
    print(f"\n{line}\n {title}\n{line}")
    if text:
        print(text)


def show_claims_menu():
    print("\nAvailable Pre-configured Customer Claims:")
    print("-" * 65)
    for cid, c in CLAIMS.items():
        photo = "Yes" if c.get("photo_on_file") else "No"
        cb = "Yes" if c.get("chargeback_in_progress") else "No"
        print(f"[{cid}] Customer: {c['customer_id']} | Amount: ${c['amount']:.2f} {c['currency']}")
        print(f"            Reason: {c['reason']} | Photo: {photo} | Chargeback: {cb}")
    print("-" * 65)


def process_claim(claim_id: str):
    format_box(f"PROCESSING CLAIM: {claim_id}")

    # 1. Tool Call: lookup_claim
    print("STEP 1: Tool Call -> lookup_claim")
    res1 = srv.dispatch({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "lookup_claim", "arguments": {"claim_id": claim_id}}
    })
    claim = res1.get("result", {})
    print(f"  [Claim Data] Customer: {claim.get('customer_id')} | Amount: ${claim.get('amount')} {claim.get('currency')} | Reason: {claim.get('reason')}")
    if claim.get("customer_description"):
        print(f"  [Customer Note] \"{claim['customer_description']}\"")

    # 2. Tool Call: check_customer_history
    cust_id = claim.get("customer_id")
    print(f"\nSTEP 2: Tool Call -> check_customer_history (Customer: {cust_id})")
    res2 = srv.dispatch({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "check_customer_history", "arguments": {"customer_id": cust_id}}
    })
    hist = res2.get("result", {})
    refunds_90d = hist.get("approved_refunds_trailing_90d", 0)
    print(f"  [History] Approved refunds in last 90 days: {refunds_90d}")

    # 3. Tool Call: search_policy_corpus
    query = f"refund {claim.get('reason')} threshold"
    print(f"\nSTEP 3: Tool Call -> search_policy_corpus (Query: '{query}')")
    res3 = srv.dispatch({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "search_policy_corpus", "arguments": {"query": query, "k": 1}}
    })
    results = res3.get("result", {}).get("results", [])
    if results:
        print(f"  [Policy Match] {results[0]['locator']}")
        print(f"  [Excerpt] \"{results[0]['text'].strip()}\"")

    # 4. Tool Call: execute_refund (Initial Attempt)
    idem_key = f"demo-run-{uuid.uuid4().hex[:8]}"
    print(f"\nSTEP 4: Tool Call -> execute_refund (Idempotency Key: {idem_key})")
    exec_req = {
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {
            "name": "execute_refund",
            "arguments": {
                "claim_id": claim_id,
                "idempotency_key": idem_key
            }
        }
    }
    res4 = srv.dispatch(exec_req)
    result = res4.get("result", {})
    status = result.get("status")

    if status == "executed":
        print("  [Result] [SUCCESS] ROUTINE REFUND EXECUTED IMMEDIATELY!")
        print(f"  [Amount Refunded] ${result.get('amount'):.2f}")
        print(f"  [Ledger Hash] {result.get('entry_hash')}")

    elif status == "declined":
        print("  [Result] [DECLINED] REFUND DECLINED BY POLICY")
        print(f"  [Reasons] {result.get('reasons')}")
        print(f"  [Policy Clause] {result.get('policy_clause')}")

    elif status == "suspended":
        req_approvers = result.get("required_approvers", 1)
        tier = result.get("tier")
        print(f"  [Result] [SUSPENDED] REFUND SUSPENDED (Tier: {tier})")
        print(f"  [Reasons] {result.get('reasons')}")
        print(f"  [Required Approvers] {req_approvers} person(s)")

        print("\n  [Action] Requesting human authorization token...")
        req = ap.request_approval(claim_id, claim["amount"], claim["currency"], req_approvers)
        approval_id = req["approval_id"]
        print(f"  [Approval Record Created] ID: {approval_id}")

        approvers = ["manager-sarah", "director-alex"][:req_approvers]
        token = None
        for approver in approvers:
            print(f"  [Deliberation] Approver '{approver}' reviewing and signing claim...")
            token = ap.sign_approval(approval_id, approver)

        if token:
            print(f"  [Token Minted] Valid cryptographic token received!")
            print(f"\nSTEP 5: Resuming Refund Execution with Signed Token...")
            resume_req = {
                "jsonrpc": "2.0", "id": 5, "method": "tools/call",
                "params": {
                    "name": "execute_refund",
                    "arguments": {
                        "claim_id": claim_id,
                        "idempotency_key": idem_key,
                        "approval_token": token
                    }
                }
            }
            res5 = srv.dispatch(resume_req)
            res5_data = res5.get("result", {})
            if res5_data.get("status") == "executed":
                print("  [Result] [SUCCESS] REFUND SUCCESSFULLY EXECUTED UPON APPROVAL!")
                print(f"  [Ledger Hash] {res5_data.get('entry_hash')}")
            else:
                print(f"  [Result] Execution failed: {res5_data}")

    # Inspect Ledger
    print("\nSTEP 6: Verifying Tamper-Proof Audit Ledger")
    led = get_ledger()
    entry = led.find_by_idempotency_key(idem_key)
    if entry:
        print(f"  [Audit Status] Verified in ledger! Entry Hash: {entry['entry_hash'][:32]}...")
    else:
        print("  [Audit Status] No money moved — zero ledger footprint (Declined).")


def run():
    print("=" * 65)
    print("       THE CLAIMS RESOLUTION AGENT — LIVE DEMO RUNNER")
    print("=" * 65)

    if len(sys.argv) > 1:
        cid = sys.argv[1].upper()
        if cid in CLAIMS:
            process_claim(cid)
            return
        elif cid == "ALL":
            for claim_id in CLAIMS:
                process_claim(claim_id)
            return
        else:
            print(f"Unknown claim ID: {cid}")

    show_claims_menu()
    print("Enter a Claim ID to resolve (e.g. CLM-77005 for routine, CLM-77001 for approval,")
    print("CLM-77004 for decline, or 'ALL' to run all 6 scenarios, or 'Q' to quit):")
    try:
        choice = input("> ").strip().upper()
    except EOFError:
        choice = "ALL"

    if choice in ("Q", "QUIT", "EXIT"):
        print("Goodbye!")
        return

    if choice == "ALL":
        for claim_id in CLAIMS:
            process_claim(claim_id)
    elif choice in CLAIMS:
        process_claim(choice)
    else:
        print(f"Invalid selection: '{choice}'. Defaulting to running all scenarios.")
        for claim_id in CLAIMS:
            process_claim(claim_id)


if __name__ == "__main__":
    run()
