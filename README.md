# Home Budget

A self-hosted envelope/sinking-fund budget tracker. Single-file web app (`index.html`)
served by GitHub Pages, backed by a Supabase Postgres database with row-level security.

**Live app:** https://wneill3333.github.io/home-budget/ (login required — invited users only)

## Architecture
- **Front end:** one HTML file, no build step. supabase-js v2 via CDN. Deploy = commit `index.html` to `main`; GitHub Pages publishes automatically.
- **Back end:** Supabase project (Postgres 17). Schema in [`schema.sql`](schema.sql).
- **Auth:** Supabase email/password. Public signups disabled; users invited via dashboard. All tables RLS-protected (authenticated role only); views use `security_invoker`; `anon` revoked.

## Features
- **Dashboard** — checking balance, total set aside in buckets, safe-to-spend, month summary by person
- **Transactions** — bank-statement sign convention, filters (month/account/person/subcategory/search), inline categorization and person assignment, per-transaction notes, split transactions across multiple subcategories with reusable templates and over-allocation protection, sticky filter header and total footer
- **Buckets** — envelope budgeting: every subcategory accrues its monthly budget from the start date; spending draws it down. Collapsed category summary with click-to-expand subcategories. Add/rename/move categories and subcategories, manual +/− adjustments with history, bucket-to-bucket transfers, click-through ledger per subcategory (allotments, adjustments, and spending interleaved with running balance)
- **Reports** — category × month and person × month matrices

## Concepts
- `transactions.amount`: positive = expense, negative = credit/income (display flips sign)
- `is_transfer = true` excludes a row from bucket math (credit-card payments, internal transfers)
- `uid` column = md5 hash for idempotent statement re-imports (`on conflict do nothing`)
- `rules` table: merchant keyword → subcategory, used to auto-categorize imports
- Split transactions: parent's `subcategory_id` is nulled; pieces live in `transaction_splits`

## Statement import workflow
Statement CSVs are parsed externally (Claude Cowork session), auto-categorized via the
`rules` table, attributed to people via card member names / last-4 mapping, and inserted
with dedup-safe uids. Scanned rent statements are OCR'd and turned into split transactions
automatically after verifying the line items sum to the matching check amount.

*No financial data is stored in this repository.*
