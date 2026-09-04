# bot-trade (KCEX spot)

O bot não faz 2FA em cada ordem. Ele abre o Chrome **uma vez** (como OAuth), você entra na conta, e ele guarda o token da sessão.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chrome
cp .env.example .env
```

## Login (leigo)

```bash
python -m kcex.cli login
```

1. Abre uma janela do Chrome do bot (perfil em `.kcex-profile/`).
2. Você resolve captcha e o código do Google Authenticator, se pedir.
3. O terminal detecta o cookie `Authorization` e grava `KCEX_TOKEN` no `.env`.
4. Fecha a janela. Pelos ~7 dias seguintes os comandos usam só o token, **sem abrir o site**.

Quando passar ~7 dias (ou der 401):

```bash
python -m kcex.cli login
```

Se o perfil do Chrome ainda estiver logado, captura na hora. Se a sessão morreu, você autentica de novo na janela.

Opcional no `.env`: `KCEX_EMAIL` e `KCEX_PASSWORD` para o bot preencher o formulário. Captcha e 2FA continuam manuais — o site exige isso, não tem refresh silencioso tipo OAuth.

## Comandos

```bash
python -m kcex.cli auth
python -m kcex.cli ticker BTC_USDT
python -m kcex.cli balances
python -m kcex.cli open-orders
```

Mapa das APIs: [docs/kcex-spot-api.md](docs/kcex-spot-api.md)
