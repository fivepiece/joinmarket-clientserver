from __future__ import (absolute_import, division,
                        print_function, unicode_literals)
from builtins import * # noqa: F401

"""
Test doing payjoin joins (with message channel layer mocked)
"""

import os
import sys
import pytest
from twisted.internet import reactor
from future.utils import iteritems
from jmbase import get_log
from jmclient import cryptoengine
from jmclient import (load_program_config, jm_single, sync_wallet,
                      P2EPMaker, P2EPTaker,
                      LegacyWallet, SegwitLegacyWallet, SegwitWallet)
from commontest import make_wallets
from test_taker import dummy_filter_orderbook
from test_coinjoin import make_wallets_to_list, sync_wallets, create_orderbook
import jmbitcoin as btc

testdir = os.path.dirname(os.path.realpath(__file__))
log = get_log()

def create_taker(wallet, schedule, monkeypatch):
    taker = P2EPTaker("fakemaker", wallet, schedule,
                      callbacks=(None, None, None))
    return taker

def dummy_user_check(message):
    # No user interaction in test; just print message
    # and assume acceptance.
    log.info(message)
    return True

def getbals(wallet, mixdepth):
    """ Retrieves balances for a mixdepth and the 'next'
    """
    bbm = wallet.get_balance_by_mixdepth()
    return (bbm[mixdepth], bbm[(mixdepth + 1) % (wallet.mixdepth + 1)])

def final_checks(wallets, amount, txfee, tsb, msb, source_mixdepth=0):
    """We use this to check that the wallet contents are
    as we've expected according to the test case.
    amount is the payment amount going from taker to maker.
    txfee is the bitcoin network transaction fee, paid by the
    taker.
    tsb, msb are taker and maker starting balances, each a tuple
    of two entries, source and destination mixdepth respectively.
    """
    jm_single().bc_interface.tickchain()
    for wallet in wallets:
        sync_wallet(wallet)
    takerbals = getbals(wallets[1], source_mixdepth)
    makerbals = getbals(wallets[0], source_mixdepth)
    # is the payment received?
    maker_newcoin_amt = makerbals[1] - msb[1]
    if not maker_newcoin_amt >= amount:
        print("Maker expected to receive at least: ", amount,
              " but got: ", maker_newcoin_amt)
        return False
    # assert that the maker received net exactly the right amount
    maker_spentcoin_amt = msb[0] - makerbals[0]
    if not maker_spentcoin_amt >= 0:
        # for now allow the non-cj fallback case
        print("maker's spent coin should have been positive, was: ", maker_spentcoin_amt)
        return False
    if not maker_newcoin_amt == amount + maker_spentcoin_amt:
        print("maker's new coins should have been: ", amount + maker_spentcoin_amt,
              " but was: ", maker_newcoin_amt)
        return False

    # Taker-side check
    # assert that the taker's total ending minus total starting
    # balance is the amount plus the txfee given.
    if not (sum(takerbals) - sum(tsb) + txfee + amount) == 0:
        print("Taker should have spent: ", txfee + amount,
              " but spent: ", sum(tsb) - sum(takerbals))
        return False
    return True

@pytest.mark.parametrize('wallet_cls, wallet_structures, mean_amt',
        [(LegacyWallet, [[4, 0, 0, 0, 0]] * 2, 1.0),
         (SegwitLegacyWallet, [[1, 3, 0, 0, 0]] * 2, 2.0),
         (SegwitWallet, [[1, 0, 0, 0, 0]] * 2, 4.0),
         ])
def test_simple_payjoin(monkeypatch, tmpdir, setup_cj, wallet_cls,
                        wallet_structures, mean_amt):
    def raise_exit(i):
        raise Exception("sys.exit called")
    monkeypatch.setattr(sys, 'exit', raise_exit)
    wallets = make_wallets_to_list(make_wallets(
        2, wallet_structures=wallet_structures,
        mean_amt=mean_amt, wallet_cls=wallet_cls))

    jm_single().bc_interface.tickchain()
    sync_wallets(wallets)

    # For accounting purposes, record the balances
    # at the start.
    msb = getbals(wallets[0], 0)
    tsb = getbals(wallets[1], 0)

    cj_amount = int(1.1 * 10**8)
    maker = P2EPMaker(wallets[0], 0, cj_amount)
    destaddr = maker.destination_addr
    monkeypatch.setattr(maker, 'user_check', dummy_user_check)
    # TODO use this to sanity check behaviour
    # in presence of the rest of the joinmarket orderbook.
    orderbook = create_orderbook([maker])
    assert len(orderbook) == 1
    # mixdepth, amount, counterparties, dest_addr, waittime;
    # in payjoin we only pay attention to the first two entries.
    schedule = [(0, cj_amount, 1, destaddr, 0)]
    taker = create_taker(wallets[-1], schedule, monkeypatch)
    monkeypatch.setattr(taker, 'user_check', dummy_user_check)
    init_data = taker.initialize(orderbook)
    # the P2EPTaker.initialize() returns:
    # (True, self.cjamount, "p2ep", "p2ep", {self.p2ep_receiver_nick:{}})
    assert init_data[0], "taker.initialize error"
    active_orders = init_data[4]
    assert len(active_orders.keys()) == 1
    response = taker.receive_utxos(list(active_orders.keys()))
    assert response[0], "taker receive_utxos error"
    # test for validity of signed fallback transaction; requires 0.17
    res = jm_single().bc_interface.rpc('testmempoolaccept', [[response[2]]])
    print("Got this result from rpc call: ", res)
    assert res[0]["allowed"], "Proposed transaction was rejected from mempool."
    maker_response = maker.on_tx_received("faketaker", response[2])
    if not maker_response[0]:
        print("maker on_tx_received failed, reason: ", maker_response[1])
        assert False
    taker_response = taker.on_tx_received("fakemaker", maker_response[2])
    if not taker_response[1] == "OK":
        print("Failure in taker on_tx_received, reason: ", taker_response[1])
        assert False
    # Although the above OK is proof that a transaction went through,
    # it doesn't prove it was a good transaction! Here do balance checks:
    assert final_checks(wallets, cj_amount, taker.total_txfee, tsb, msb)

@pytest.fixture(scope='module')
def setup_cj():
    load_program_config()
    jm_single().config.set('POLICY', 'tx_broadcast', 'self')
    jm_single().bc_interface.tick_forward_chain_interval = 5
    jm_single().bc_interface.simulate_blocks()
    #see note in cryptoengine.py:
    cryptoengine.BTC_P2WPKH.VBYTE = 100
    yield None
    # teardown
    for dc in reactor.getDelayedCalls():
        dc.cancel()