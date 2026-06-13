# Monday Weekly Budget Import — Runbook
*Pure-CSV flow. Bill downloads CSVs + handles logins; AI parses, dedups, imports.*
*Read W:\AI\Budget\PROJECT-NOTES.md first for full system context.*

## Why CSV-only
- **Citi is hard-blocked** for the Chrome tool: navigating to citi.com returns
  "site not allowed," and Citi tabs don't even appear in the tool's tab list, so
  live scraping of the credit cards is impossible. Re-verified Jun 13 2026.
- Mission Fed checking *can* be live-scraped, but Bill chose CSV for everything so
  the routine needs no logins and is fully reliable regardless of page changes.

## System
- Database: Supabase project `tiihzphmvlzvrxdigxan` (home-budget) — use the
  Supabase MCP (execute_sql) for all reads/writes.
- App: https://wneill3333.github.io/home-budget/
- Importer: `W:\AI\Budget\import_csvs.py` (parses CSVs → dedup-safe import SQL).

## How dedup works (no dependence on the legacy uid hash)
The importer stages all parsed rows in a temp table, then inserts into
`transactions` only where a row's in-batch rank on the natural key
`(account_id, trans_date, description, amount)` exceeds the count already in the
DB for that key. Re-importing the same statements inserts nothing; overlapping
statement files collapse; genuine same-day repeats are preserved. A fresh
deterministic `uid` is generated per inserted row to satisfy the UNIQUE column.

## Steps, in order

### 1. Reminder kicks off the session (scheduled)
The Monday scheduled task messages Bill: "Weekly budget import — download this
week's CSVs into the Statements subfolders, and log into Amazon/eBay if you want
order links refreshed. Reply when ready." WAIT for his reply. Nothing imports
until the CSVs are in place (the routine can only read files that exist).

### 2. CSV locations (Bill files each into its subfolder)
```
W:\AI\Budget\Statements\Checking\        <- Mission Fed AccountHistory.csv
W:\AI\Budget\Statements\Citi Strata\     <- Citi Strata statement/activity CSV(s)
W:\AI\Budget\Statements\Costco Card\      <- Citi Costco CSV(s) (has Member Name)
W:\AI\Budget\Statements\Custom Card\      <- Citi Custom Cash CSV(s)
W:\AI\Budget\Statements\Laina's Card\     <- Laina's Citi CSV (all = Laina)
```
CSV formats: Checking = `Account Number,Post Date,Check,Description,Debit,Credit,
Status,Balance,Classification` (import Status=Posted only). Cards = `Status,Date,
Description,Debit,Credit[,Member Name]` (import Status=Cleared only). Citi credit
amounts already carry a leading minus.

### 3. Run the importer
```
cd /sessions/<mount>/mnt/Budget
python3 import_csvs.py --since <~6 weeks ago, YYYY-MM-DD>
```
The `--since` floor keeps the generated SQL small (the CSVs carry full history;
dedup makes the overlap harmless). Use a floor a few weeks before the last import
to catch late-posting items. Then run the contents of the generated `import.sql`
via Supabase `execute_sql`. It: inserts only new rows, auto-categorizes new
non-transfer rows via the `rules` table (case-insensitive substring, longest
keyword wins), and updates `settings.checking_balance` / `balance_asof` from the
latest POSTED checking row that carries a Balance.

Parsing rules baked into the importer:
- Sign: positive = expense, negative = income/credit.
- Person: Costco Member Name WILLIAM→Bill(1)/SELAINA→Laina(2); Laina's card→Laina;
  Strata last4 via card_map (9545→Bill); else null.
- last4: stripped from the trailing " null XXXXXXXXXXXX####" suffix into `last4`.
- is_transfer=true for `PAYMENT THANK YOU`, `ONLINE PAYMENT`, `AUTOPAY`/`AUTO PAY`,
  `AUTOMATIC PAYMENT`, and checking `Transfer from …` (incoming). `Transfer to …`
  stays a normal expense.
- Pending (checking) / non-Cleared (cards) rows are skipped until they post.

### 4. Verify (always)
- Row count rose by exactly the predicted number (run `--dryrun` first if unsure;
  it writes `import_dryrun.sql` returning new-row counts per account — read-only).
- `select count(*) from (select account_id,trans_date,description,amount,count(*)
  from transactions group by 1,2,3,4 having count(*)>1) d;` — investigate any new
  duplicate group (one known PRE-EXISTING dup: Costco 2026-03-13 ANTHROPIC $21.03).
- Spot-check amounts/signs and a few categorizations.

### 5. Amazon/eBay order research (standing step — needs Bill logged in)
Bill wants every Amazon/eBay charge tied back to its actual order so he can tell
which budget category it belongs to. For each NEW Amazon or eBay transaction this
import added (account-agnostic — they appear on the Citi cards), do BOTH:

1. **Find the order.** Per PROJECT-NOTES "Amazon/eBay order links": with Bill
   logged in, open amazon.com/cpe/yourpayments/transactions, extract
   (date | amount | order#) per page, filter out Gift Card rows, paginate via
   coordinate clicks; match to the transaction by exact amount + closest date
   (best-guess on ties). eBay order ids parse from the description (XX-XXXXX-XXXXX).
2. **Write what it was.** Open the matched order and capture the item name(s).
   Write `transactions.order_url` (physical: /gp/css/summary/edit.html?orderID=X;
   digital D01: /gp/digital/your-account/order-summary.html?orderID=X) AND put a
   short item summary into `transactions.notes` (e.g. "Amazon: HP 65 ink + USB-C
   cable") so Bill can categorize at a glance. If a note already exists, append
   rather than overwrite.

Only touch transactions where the order is matched with confidence; leave the
rest for Bill and say which ones you couldn't match. This step needs Bill logged
into Amazon/eBay — if he isn't, skip it and note it in the summary.

### 6. Summary to Bill
Report per-account new rows, new checking balance, safe-to-spend (dashboard),
uncategorized count, order links added, and anything skipped or flagged.

## Hard rules
- Never enter passwords, never bypass 2FA, never move money.
- All inserts dedup-safe; NEVER delete transactions (flag dupes for Bill instead).
- Do NOT attempt to reach citi.com by any means — it is blocked by policy.
- If anything looks wrong (amounts don't reconcile, a CSV format changed), stop
  that account, note it in the summary, and leave the database untouched for it.
