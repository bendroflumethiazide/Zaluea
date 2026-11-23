# Connecting ATLAS to fake-money accounts (Windows)

This guide connects ATLAS to **practice accounts only**: Kalshi's demo exchange and Alpaca's paper trading. No real money is involved. You keep your keys on your PC; never paste them into chat or put them in the repository.

## 1. Make a private folder for keys

Open PowerShell and run:

```powershell
mkdir "$env:USERPROFILE\.atlas\keys"
icacls "$env:USERPROFILE\.atlas\keys" /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F"
```

The second line makes the folder readable only by your Windows user. The repository's `.gitignore` also ignores `*.key` and `*.pem` files, but keep keys outside the repository folder anyway.

## 2. Kalshi demo key (fake money)

1. Sign up at **https://demo.kalshi.co**. The demo account is separate from any real Kalshi account.
2. Go to **Account & security → API Keys → Create Key**. Keep the default key type, **Ed25519**.
3. Save the downloaded private key file as `%USERPROFILE%\.atlas\keys\kalshi-demo.key`. Copy the **Key ID** shown on screen (it looks like `a952bcbe-…`).
4. Kalshi can't show the private key again, so if you lose it, create a new key.

**About balances:** Kalshi splits accounts into exchange shards. BTC and ETH 15-minute markets trade on **shard 2**, which needs its own balance. The connection check shows each shard's balance.

## 3. Run the read-only check

From the `atlas` folder:

```powershell
python -m atlas.check_connection --kalshi-key-id YOUR-KEY-ID --kalshi-key-file "$env:USERPROFILE\.atlas\keys\kalshi-demo.key"
```

You should see PASS lines for:
- the BTC reference price
- clock agreement with Kalshi
- a signed request returning your balance
- positions and orders
- account data freshness

It ends with **ALL CHECKS PASSED**.

Then prove the order path. This places **one** 1-contract order at $0.01 on the demo exchange, on a side priced far higher, so it can't fill. It then reads the order back:

```powershell
python -m atlas.check_connection --kalshi-key-id YOUR-KEY-ID --kalshi-key-file "$env:USERPROFILE\.atlas\keys\kalshi-demo.key" --demo-order-test
```

The test refuses to run against the real exchange.

## 4. Alpaca paper key (fake money)

1. Sign up at **https://alpaca.markets** and open the **Paper** account.
2. Generate API keys. Copy the **Key ID**, and save the **Secret Key** as a one-line text file: `%USERPROFILE%\.atlas\keys\alpaca-paper-secret.txt`.
3. Run:

```powershell
python -m atlas.check_connection --alpaca-key-id YOUR-ALPACA-KEY-ID --alpaca-secret-file "$env:USERPROFILE\.atlas\keys\alpaca-paper-secret.txt"
```

Alpaca accounts start with **margin and short selling enabled**. Your spec forbids both, so the check will show FAIL lines for them. To switch the account to long-only, no margin and no options, add `--apply-safe-settings`; it asks you to type `APPLY` first.

## 5. Before any real money (later phases)

We will set these up together before live trading:
- **A dedicated Kalshi subaccount for ATLAS,** so your own manual trades never look like "unexplained activity" and lock ATLAS.
- **Scoped Kalshi keys:**
  - The gateway gets `read` + `write::trade` and never `write::transfer`, so it can't move money.
  - The watchdog and the shadow risk manager get **read-only** keys.
- **Optionally, an IP allowlist** on the Kalshi key.
