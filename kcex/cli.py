from __future__ import annotations

import argparse
import json
import sys
import time

from dotenv import load_dotenv

from .client import KcexClient
from .login import login_interactive, require_live_token
from .session import token_preview


def _print(payload: object) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def build_client() -> KcexClient:
    load_dotenv()
    return KcexClient()


def build_private_client() -> KcexClient:
    return KcexClient(token=require_live_token())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KCEX spot bot CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ping")
    p_login = sub.add_parser("login", help="Abre o Chrome, você entra na conta, o bot salva o token")
    p_login.add_argument("--timeout", type=int, default=600, help="Segundos esperando o login (padrão 600)")
    sub.add_parser("auth")
    p_ticker = sub.add_parser("ticker")
    p_ticker.add_argument("symbol", nargs="?", default="BTC_USDT")
    p_depth = sub.add_parser("depth")
    p_depth.add_argument("symbol", nargs="?", default="BTC_USDT")
    sub.add_parser("balances")
    sub.add_parser("open-orders")
    sub.add_parser("user")
    p_deals = sub.add_parser("my-deals")
    p_deals.add_argument("--currency", default="BTC")
    p_deals.add_argument("--market", default="USDT")
    p_kline = sub.add_parser("kline")
    p_kline.add_argument("symbol", nargs="?", default="BTC_USDT")
    p_kline.add_argument("--interval", default="Min15")
    p_kline.add_argument("--hours", type=int, default=12)

    args = parser.parse_args(argv)

    if args.cmd == "login":
        token = login_interactive(timeout_sec=args.timeout)
        print(f"Pronto. Próximos comandos usam {token_preview(token)} até a sessão expirar.")
        return 0

    client = build_client()

    if args.cmd == "ping":
        _print(client.ping())
        return 0
    elif args.cmd == "auth":
        try:
            token = require_live_token()
        except Exception as exc:
            print(f"sessao invalida: {exc}")
            return 1
        client = KcexClient(token=token)
        try:
            info = client.user_info()
        except Exception as exc:
            print(f"sessao invalida: {exc}")
            return 1
        data = (info or {}).get("data") or {}
        _print(
            {
                "ok": True,
                "token": token_preview(token),
                "token_len": len(token),
                "account": data.get("account") or data.get("email"),
                "lastLoginTime": data.get("lastLoginTime"),
                "hint": "Captcha e 2FA so na janela de login. Depois o bot reusa o token ~7 dias.",
            }
        )
        return 0
    elif args.cmd == "ticker":
        _print(client.ticker(args.symbol))
    elif args.cmd == "depth":
        _print(client.depth(args.symbol))
    elif args.cmd == "balances":
        _print(build_private_client().balances())
    elif args.cmd == "open-orders":
        _print(build_private_client().open_orders())
    elif args.cmd == "user":
        _print(build_private_client().user_info())
    elif args.cmd == "my-deals":
        _print(build_private_client().my_deals(args.currency, args.market))
    elif args.cmd == "kline":
        end = int(time.time() * 1000)
        start = end - args.hours * 3600 * 1000
        _print(client.kline(args.symbol, interval=args.interval, start=start, end=end))
    else:
        parser.error("unknown command")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
