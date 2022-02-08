from time import time, sleep
from typing import List, Tuple, Dict, Any, Optional, Union
from base64 import b64decode
import base64
import random
import hashlib
import uuid
import sys

from algosdk.v2client.algod import AlgodClient
from algosdk.kmd import KMDClient
from algosdk import account, mnemonic
from algosdk.encoding import decode_address
from algosdk.future import transaction
from pyteal import compileTeal, Mode, Expr
from pyteal import *
from algosdk.logic import get_application_address

import pprint

class Account:
    """Represents a private key and address for an Algorand account"""

    def __init__(self, privateKey: str) -> None:
        self.sk = privateKey
        self.addr = account.address_from_private_key(privateKey)
        print (privateKey + " -> " + self.getMnemonic())

    def getAddress(self) -> str:
        return self.addr

    def getPrivateKey(self) -> str:
        return self.sk

    def getMnemonic(self) -> str:
        return mnemonic.from_private_key(self.sk)

    @classmethod
    def FromMnemonic(cls, m: str) -> "Account":
        return cls(mnemonic.to_private_key(m))

class PendingTxnResponse:
    def __init__(self, response: Dict[str, Any]) -> None:
        self.poolError: str = response["pool-error"]
        self.txn: Dict[str, Any] = response["txn"]

        self.applicationIndex: Optional[int] = response.get("application-index")
        self.assetIndex: Optional[int] = response.get("asset-index")
        self.closeRewards: Optional[int] = response.get("close-rewards")
        self.closingAmount: Optional[int] = response.get("closing-amount")
        self.confirmedRound: Optional[int] = response.get("confirmed-round")
        self.globalStateDelta: Optional[Any] = response.get("global-state-delta")
        self.localStateDelta: Optional[Any] = response.get("local-state-delta")
        self.receiverRewards: Optional[int] = response.get("receiver-rewards")
        self.senderRewards: Optional[int] = response.get("sender-rewards")

        self.innerTxns: List[Any] = response.get("inner-txns", [])
        self.logs: List[bytes] = [b64decode(l) for l in response.get("logs", [])]

class Token:
    def __init__(self) -> None:
        self.ALGOD_ADDRESS = "http://localhost:4001"
        self.ALGOD_TOKEN = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        self.FUNDING_AMOUNT = 100_000_000

        self.KMD_ADDRESS = "http://localhost:4002"
        self.KMD_TOKEN = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        self.KMD_WALLET_NAME = "unencrypted-default-wallet"
        self.KMD_WALLET_PASSWORD = ""

        self.kmdAccounts : Optional[List[Account]] = None

        self.accountList : List[Account] = []

    def waitForTransaction(
            self, client: AlgodClient, txID: str, timeout: int = 10
    ) -> PendingTxnResponse:
        lastStatus = client.status()
        lastRound = lastStatus["last-round"]
        startRound = lastRound
    
        while lastRound < startRound + timeout:
            pending_txn = client.pending_transaction_info(txID)
    
            if pending_txn.get("confirmed-round", 0) > 0:
                return PendingTxnResponse(pending_txn)
    
            if pending_txn["pool-error"]:
                raise Exception("Pool error: {}".format(pending_txn["pool-error"]))
    
            lastStatus = client.status_after_block(lastRound + 1)
    
            lastRound += 1
    
        raise Exception(
            "Transaction {} not confirmed after {} rounds".format(txID, timeout)
        )

    def getKmdClient(self) -> KMDClient:
        return KMDClient(self.KMD_TOKEN, self.KMD_ADDRESS)
    
    def getGenesisAccounts(self) -> List[Account]:
        if self.kmdAccounts is None:
            kmd = self.getKmdClient()
    
            wallets = kmd.list_wallets()
            walletID = None
            for wallet in wallets:
                if wallet["name"] == self.KMD_WALLET_NAME:
                    walletID = wallet["id"]
                    break
    
            if walletID is None:
                raise Exception("Wallet not found: {}".format(self.KMD_WALLET_NAME))
    
            walletHandle = kmd.init_wallet_handle(walletID, self.KMD_WALLET_PASSWORD)
    
            try:
                addresses = kmd.list_keys(walletHandle)
                privateKeys = [
                    kmd.export_key(walletHandle, self.KMD_WALLET_PASSWORD, addr)
                    for addr in addresses
                ]
                self.kmdAccounts = [Account(sk) for sk in privateKeys]
            finally:
                kmd.release_wallet_handle(walletHandle)
    
        return self.kmdAccounts
    
    def getTemporaryAccount(self, client: AlgodClient) -> Account:
        if len(self.accountList) == 0:
            sks = [account.generate_account()[0] for i in range(3)]
            self.accountList = [Account(sk) for sk in sks]
    
            genesisAccounts = self.getGenesisAccounts()
            suggestedParams = client.suggested_params()
    
            txns: List[transaction.Transaction] = []
            for i, a in enumerate(self.accountList):
                fundingAccount = genesisAccounts[i % len(genesisAccounts)]
                txns.append(
                    transaction.PaymentTxn(
                        sender=fundingAccount.getAddress(),
                        receiver=a.getAddress(),
                        amt=self.FUNDING_AMOUNT,
                        sp=suggestedParams,
                    )
                )
    
            txns = transaction.assign_group_id(txns)
            signedTxns = [
                txn.sign(genesisAccounts[i % len(genesisAccounts)].getPrivateKey())
                for i, txn in enumerate(txns)
            ]
    
            client.send_transactions(signedTxns)
    
            self.waitForTransaction(client, signedTxns[0].get_txid())
    
        return self.accountList.pop()
    
    def getAlgodClient(self) -> AlgodClient:
        return AlgodClient(self.ALGOD_TOKEN, self.ALGOD_ADDRESS)

    def getBalances(self, client: AlgodClient, account: str) -> Dict[int, int]:
        balances: Dict[int, int] = dict()
    
        accountInfo = client.account_info(account)
    
        # set key 0 to Algo balance
        balances[0] = accountInfo["amount"]
    
        assets: List[Dict[str, Any]] = accountInfo.get("assets", [])
        for assetHolding in assets:
            assetID = assetHolding["asset-id"]
            amount = assetHolding["amount"]
            balances[assetID] = amount
    
        return balances

    def fullyCompileContract(self, client: AlgodClient, contract: Expr) -> bytes:
        teal = compileTeal(contract, mode=Mode.Application, version=5)
        response = client.compile(teal)
        return response

    # helper function that formats global state for printing
    def format_state(self, state):
        formatted = {}
        for item in state:
            key = item['key']
            value = item['value']
            formatted_key = base64.b64decode(key).decode('utf-8')
            if value['type'] == 1:
                # byte string
                if formatted_key == 'voted':
                    formatted_value = base64.b64decode(value['bytes']).decode('utf-8')
                else:
                    formatted_value = value['bytes']
                formatted[formatted_key] = formatted_value
            else:
                # integer
                formatted[formatted_key] = value['uint']
        return formatted
    
    # helper function to read app global state
    def read_global_state(self, client, addr, app_id):
        results = client.account_info(addr)
        apps_created = results['created-apps']
        for app in apps_created:
            if app['id'] == app_id and 'global-state' in app['params']:
                return self.format_state(app['params']['global-state'])
        return {}

    def read_state(self, client, addr, app_id):
        results = client.account_info(addr)
        apps_created = results['created-apps']
        for app in apps_created:
            if app['id'] == app_id:
                return app
        return {}

    def getPrimaryContracts(self, client: AlgodClient) -> Tuple[bytes, bytes]:
        @Subroutine(TealType.anytype)
        def magic_load(key: TealType.bytes, default: Expr) -> Expr:
            maybe = App.globalGetEx(Int(0), key)
            return Seq(maybe, If(maybe.hasValue(), maybe.value(), default))

        def check_load(key: TealType.bytes) -> int:
            maybe = App.globalGetEx(Int(0), key)
            return Seq(maybe, If (maybe.hasValue(), Int(1), Int(0)))

        def magic_store(key: TealType.bytes, val: any):
            return Seq([App.globalPut(key, val)])

        def createWrapped():
            uid = ScratchVar()
            mine = Global.current_application_address()
        
            return Seq([
                InnerTxnBuilder.Begin(),
                InnerTxnBuilder.SetFields(
                    {
                        TxnField.type_enum: TxnType.AssetConfig,
                        TxnField.config_asset_name: Bytes("hi"),
                        TxnField.config_asset_unit_name: Bytes("there"),
                        TxnField.config_asset_total: Int(int(1e10)),  # Is this needed?
                        TxnField.config_asset_decimals: Int(8),
                        TxnField.config_asset_manager: mine,
                        TxnField.config_asset_reserve: mine,
                        TxnField.config_asset_clawback: mine,
                    }
                ),
                InnerTxnBuilder.Submit(),
        
                Log(Itob(InnerTxn.created_asset_id())),
        
                Approve()
            ])

        @Subroutine(TealType.uint64)
        def bootstrap():
            return Seq([Approve()])
        
        @Subroutine(TealType.uint64)
        def is_creator():
            return Txn.sender() == Global.creator_address()
        
        def vaa_processor_program():
            handle_create = Return(bootstrap())
            handle_update = Return(is_creator())
            handle_delete = Return(is_creator())
            METHOD = Txn.application_args[0]
            handle_noop = Cond(
                [METHOD == Bytes("createWrapped"), createWrapped()]
            )
            return Cond(
                [Txn.application_id() == Int(0), handle_create],
                [Txn.on_completion() == OnComplete.UpdateApplication, handle_update],
                [Txn.on_completion() == OnComplete.DeleteApplication, handle_delete],
                [Txn.on_completion() == OnComplete.NoOp, handle_noop]
            )
        
        def clear_state_program():
            return Int(1)
    
        APPROVAL_PROGRAM = self.fullyCompileContract(client, vaa_processor_program())
        CLEAR_STATE_PROGRAM = self.fullyCompileContract(client, clear_state_program())

        return APPROVAL_PROGRAM, CLEAR_STATE_PROGRAM

    def createTokenApp(
        self,
        client: AlgodClient,
        sender: Account,
    ) -> int:
        approval, clear = self.getPrimaryContracts(client)
    
        globalSchema = transaction.StateSchema(num_uints=40, num_byte_slices=6)
        localSchema = transaction.StateSchema(num_uints=0, num_byte_slices=0)
    
        app_args = [ ]
    
        txn = transaction.ApplicationCreateTxn(
            sender=sender.getAddress(),
            on_complete=transaction.OnComplete.NoOpOC,
            approval_program=b64decode(approval["result"]),
            clear_program=b64decode(clear["result"]),
            global_schema=globalSchema,
            local_schema=localSchema,
            app_args=app_args,
            sp=client.suggested_params(),
        )
    
        signedTxn = txn.sign(sender.getPrivateKey())
    
        client.send_transaction(signedTxn)
    
        response = self.waitForTransaction(client, signedTxn.get_txid())
        assert response.applicationIndex is not None and response.applicationIndex > 0

        return response.applicationIndex

    def createWrapped(self, client: AlgodClient, appID: int, bidder: Account, bidAmount: int, coin: int) -> None:
        appAddr = get_application_address(appID)
    
        suggestedParams = client.suggested_params()
    
        payTxn = transaction.PaymentTxn(
            sender=bidder.getAddress(),
            receiver=appAddr,
            amt=bidAmount,
            sp=suggestedParams,
        )

        appCallTxn = transaction.ApplicationCallTxn(
            sender=bidder.getAddress(),
            index=appID,
            on_complete=transaction.OnComplete.NoOpOC,
            app_args=[b"createWrapped"],
            sp=suggestedParams,
        )

        transaction.assign_group_id([payTxn, appCallTxn])
    
        signedPayTxn = payTxn.sign(bidder.getPrivateKey())
        signedAppCallTxn = appCallTxn.sign(bidder.getPrivateKey())
    
        client.send_transactions([signedPayTxn, signedAppCallTxn])
    
        pprint.pprint(self.waitForTransaction(client, appCallTxn.get_txid()).__dict__)

    def simple_token(self):
        client = self.getAlgodClient()

        print("Generating the foundation account...")
        foundation = self.getTemporaryAccount(client)

        print("Creating the Token app")
        appID = self.createTokenApp(client=client, sender=foundation)
        print("appID = " + str(appID))

        print("create wrapped coin 0")
        self.createWrapped(client, appID, foundation, 201000, 0)

        pprint.pprint(self.read_state(client, foundation.getAddress(), appID))

token = Token()
token.simple_token()
