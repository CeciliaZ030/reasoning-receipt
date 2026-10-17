"""Settlement acceptance: configured transfer only, simulation gate, verified
finality, and one linked outcome receipt per source receipt."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from protocol.receipt import verify_receipt
from server.portable import router as portable_router
from server.settlement import TRANSFER_TOPIC
from server.settlement import router as settlement_router
from storage.db import Session
from storage.db import Settlement as SettlementRow

RPC = "https://rpc.test/arc"
CHAIN = 5_042_002
USDC = "0x3600000000000000000000000000000000000000"
RECIPIENT = "0x" + "22" * 20
PAYER = "0x" + "33" * 20
AUTH = {"Authorization": "Bearer settlement-test-token"}
TX = "0x" + "aa" * 32
OTHER_TX = "0x" + "bb" * 32

app = FastAPI()
app.include_router(portable_router)
app.include_router(settlement_router)
client = TestClient(app)


def word(value: str | int) -> str:
    return "0x" + (value[2:] if isinstance(value, str) else f"{value:x}").rjust(64, "0")


def transfer_log(*, frm=PAYER, to=RECIPIENT, amount=10_000, token=USDC, index=0) -> dict:
    return {
        "address": token,
        "topics": [TRANSFER_TOPIC, word(frm), word(to)],
        "data": word(amount),
        "logIndex": hex(index),
    }


class FakeChain:
    """Just enough JSON-RPC for the settlement routes."""

    def __init__(self) -> None:
        self.chain_id = CHAIN
        self.call = {"result": word(1)}
        self.receipts: dict[str, dict] = {}
        self.finalized = 100
        self.blocks = {hex(n): "0x" + f"{n:064x}" for n in range(1, 200)}

    def mine(self, tx: str, *, status="0x1", block=90, logs=None) -> None:
        self.receipts[tx] = {
            "status": status,
            "blockNumber": hex(block),
            "blockHash": self.blocks[hex(block)],
            "logs": [transfer_log()] if logs is None else logs,
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        if method == "eth_chainId":
            reply = {"result": hex(self.chain_id)}
        elif method == "eth_call":
            reply = self.call
        elif method == "eth_getTransactionReceipt":
            reply = {"result": self.receipts.get(params[0])}
        elif params[0] == "finalized":
            reply = {"result": {"number": hex(self.finalized)}}
        else:
            reply = {"result": {"hash": self.blocks[params[0]]}}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, **reply})


@pytest.fixture
def chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = tmp_path / "settlement-deployment.json"
    config.write_text(
        json.dumps(
            {
                "enabled": True,
                "chain_id": CHAIN,
                "token": USDC,
                "recipient": RECIPIENT,
                "amount_units": "10000",
                "api_url": "https://api.example",
            }
        )
    )
    monkeypatch.setenv("RR_SETTLEMENT_CONFIG", str(config))
    monkeypatch.setenv("RR_SETTLEMENT_API_TOKEN", "settlement-test-token")
    monkeypatch.setenv("RR_SETTLEMENT_RPC_URL", RPC)
    fake = FakeChain()
    with respx.mock(assert_all_called=False) as mock:
        mock.post(RPC).mock(side_effect=fake)
        yield fake


def source_receipt(subject="agent:pay-reviewer") -> dict:
    created = client.post(
        "/v1/receipts",
        json={
            "subject": subject,
            "nodes": [
                {"id": "intent", "kind": "request", "payload": {"pay": "0.01 USDC"}},
                {"id": "decision", "kind": "outcome", "payload": {"approved": True}},
            ],
        },
    ).json()
    created.pop("receipt_hash")
    return created


def prepare(receipt: dict, **overrides):
    body = {"receipt": receipt, "execution_account": PAYER, "chain_id": CHAIN, **overrides}
    return client.post("/v1/settlements/prepare", json=body, headers=AUTH)


def verify(receipt_hash: str, tx: str = TX):
    return client.post(
        "/v1/settlements/verify",
        json={"receipt_hash": receipt_hash, "transaction_hash": tx},
        headers=AUTH,
    )


def settled_rows() -> list[SettlementRow]:
    with Session() as s:
        return list(s.scalars(select(SettlementRow).where(SettlementRow.status == "settled")))


def test_transfers_are_disabled_without_a_configured_deployment(monkeypatch) -> None:
    monkeypatch.delenv("RR_SETTLEMENT_CONFIG", raising=False)
    assert prepare(source_receipt()).status_code == 503
    assert verify("0x" + "11" * 32).status_code == 503


def test_preview_matches_configuration_and_settles_with_a_linked_outcome(chain) -> None:
    original = source_receipt()
    pristine = deepcopy(original)

    prepared = prepare(original)
    assert prepared.status_code == 200, prepared.text
    body = prepared.json()
    data = body["transaction"]["data"]
    assert body["transaction"] == {"to": USDC, "chain_id": CHAIN, "value": "0", "data": data}
    assert data[:10] == "0xa9059cbb"
    assert "0x" + data[34:74] == RECIPIENT
    assert int(data[74:], 16) == 10_000 == int(body["amount_units"])
    assert (body["recipient"], body["token"], body["chain_id"]) == (RECIPIENT, USDC, CHAIN)

    chain.mine(TX)
    settled = verify(body["receipt_hash"])
    assert settled.status_code == 200, settled.text
    outcome = settled.json()["outcome_receipt"]
    assert settled.json()["status"] == "settled"
    assert verify_receipt({k: v for k, v in outcome.items() if k != "receipt_hash"})
    nodes = {node["id"]: node["payload"] for node in outcome["nodes"]}
    assert nodes["source"]["receipt_hash"] == body["receipt_hash"]
    assert nodes["source"]["merkle_root"] == original["merkle_root"]
    assert nodes["transfer"]["transaction_hash"] == TX
    assert outcome["receipt_hash"] != body["receipt_hash"]

    # The original is untouched, still verifies, and is stored exactly as verified.
    assert original == pristine
    assert verify_receipt(original)
    with Session() as s:
        stored = s.scalars(select(SettlementRow)).one()
        assert json.loads(stored.source_receipt) == pristine


def test_failed_simulation_blocks_preparation(chain) -> None:
    chain.call = {"error": {"code": 3, "message": "execution reverted: transfer amount exceeds balance"}}
    receipt = source_receipt()
    response = prepare(receipt)
    assert response.status_code == 409
    assert "transaction" not in response.json()
    chain.call = {"result": word(0)}
    assert prepare(receipt).status_code == 409
    with Session() as s:
        assert s.scalars(select(SettlementRow)).first() is None


def test_wrong_network_tampered_receipt_and_bad_credentials_are_refused(chain) -> None:
    receipt = source_receipt()
    assert prepare(receipt, chain_id=1).status_code == 409
    tampered = deepcopy(receipt)
    tampered["nodes"][0]["payload"]["pay"] = "100 USDC"
    assert prepare(tampered).status_code == 422
    bad = client.post(
        "/v1/settlements/prepare",
        json={"receipt": receipt, "execution_account": PAYER, "chain_id": CHAIN},
        headers={"Authorization": "Bearer wrong"},
    )
    assert bad.status_code == 401
    chain.chain_id = 1
    assert prepare(receipt).status_code == 503


def test_pending_and_reverted_never_become_success(chain) -> None:
    receipt_hash = prepare(source_receipt()).json()["receipt_hash"]

    assert verify(receipt_hash).json() == {"status": "pending"}
    chain.mine(TX, status="0x0", logs=[])
    assert verify(receipt_hash).json() == {"status": "reverted"}
    chain.mine(TX, block=150)
    assert verify(receipt_hash).json() == {"status": "pending_finality"}
    assert settled_rows() == []

    # A non-canonical block hash is refused, not recorded.
    chain.mine(TX, block=90)
    chain.receipts[TX]["blockHash"] = "0x" + "ff" * 32
    assert verify(receipt_hash).status_code == 409
    assert settled_rows() == []

    chain.mine(TX, block=90)
    assert verify(receipt_hash).json()["status"] == "settled"


@pytest.mark.parametrize(
    "logs",
    [
        [transfer_log(amount=9_999)],
        [transfer_log(to="0x" + "44" * 20)],
        [transfer_log(frm="0x" + "55" * 20)],
        [transfer_log(token="0x" + "66" * 20)],
        [transfer_log(index=0), transfer_log(index=1)],
        [],
    ],
)
def test_transaction_must_contain_exactly_the_configured_transfer(chain, logs) -> None:
    receipt_hash = prepare(source_receipt()).json()["receipt_hash"]
    chain.mine(TX, logs=logs)
    assert verify(receipt_hash).status_code == 409
    assert settled_rows() == []


def test_repeated_processing_creates_one_success_record(chain) -> None:
    receipt = source_receipt()
    receipt_hash = prepare(receipt).json()["receipt_hash"]
    chain.mine(TX)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: verify(receipt_hash), range(6)))
    assert {r.status_code for r in results} == {200}
    assert len({r.json()["outcome_receipt"]["receipt_hash"] for r in results}) == 1
    assert len(settled_rows()) == 1

    # Re-preparing, a second transaction, or reusing the hash elsewhere is refused.
    assert prepare(receipt).status_code == 409
    chain.mine(OTHER_TX)
    assert verify(receipt_hash, OTHER_TX).status_code == 409
    other_hash = prepare(source_receipt("agent:other")).json()["receipt_hash"]
    assert verify(other_hash, TX).status_code == 409
    assert len(settled_rows()) == 1
