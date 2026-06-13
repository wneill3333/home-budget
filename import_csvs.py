#!/usr/bin/env python3
"""
Home Budget - Monday CSV importer.

Parses every CSV under Statements/<account>/ , normalizes to the transactions
schema, and emits SQL for a server-side, dedup-safe import.

Dedup (bulletproof, does NOT rely on the legacy uid hash):
  All candidate rows go into a temp staging table. A window function numbers
  duplicate natural keys (account_id, trans_date, description, amount) within
  the batch; rows insert into transactions only where their row-number exceeds
  the count already present in the DB for that key. Re-importing the same
  statements inserts nothing; overlapping statement files collapse.

Sign convention: positive = expense, negative = income/credit.

Usage:
  python3 import_csvs.py            -> report + writes import.sql (real import)
  python3 import_csvs.py --dryrun   -> writes import_dryrun.sql (counts only)
"""
import csv, glob, os, re, sys, hashlib
from datetime import datetime

BUDGET = os.path.dirname(os.path.abspath(__file__))
STMT = os.path.join(BUDGET, "Statements")

ACCT = {"Checking": 1, "Citi Strata": 2, "Costco Card": 3,
        "Custom Card": 4, "Laina's Card": 5}
NAMES = {1: "Checking", 2: "Citi Strata", 3: "Citi Costco",
         4: "Citi Custom Cash", 5: "Citi - Laina"}
PERSON_WILLIAM, PERSON_LAINA = 1, 2
CARD_MAP = {"9545": PERSON_WILLIAM}

TRANSFER_HINTS = ("PAYMENT THANK YOU", "ONLINE PAYMENT", "AUTOPAY", "AUTO PAY",
                  "AUTOMATIC PAYMENT")
SUFFIX_RE = re.compile(r"\s+null\s+X+(\d{4})\s*$", re.IGNORECASE)


def clean_desc(d):
    d = (d or "").strip()
    m = SUFFIX_RE.search(d)
    last4 = None
    if m:
        last4 = m.group(1)
        d = d[:m.start()].strip()
    return re.sub(r"\s+", " ", d), last4


def parse_date(s):
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(s.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def money(s):
    s = (s or "").strip().replace("$", "").replace(",", "")
    return None if s == "" else round(float(s), 2)


def is_transfer(desc):
    u = desc.upper()
    return any(h in u for h in TRANSFER_HINTS) or u.startswith("TRANSFER FROM")


def parse_checking(path, rows):
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if (r.get("Status") or "").strip().lower() != "posted":
                continue
            d, _ = clean_desc(r.get("Description"))
            debit, credit = money(r.get("Debit")), money(r.get("Credit"))
            if debit is not None:
                amt = debit
            elif credit is not None:
                amt = -credit
            else:
                continue
            rows.append(dict(account_id=1, trans_date=parse_date(r["Post Date"]),
                             description=d, amount=amt, person_id=None,
                             last4=None, is_transfer=is_transfer(d)))


def parse_card(path, account_id, rows, default_person=None, use_member=False):
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if (r.get("Status") or "").strip().lower() != "cleared":
                continue
            d, last4 = clean_desc(r.get("Description"))
            debit, credit = money(r.get("Debit")), money(r.get("Credit"))
            if debit is not None:
                amt = debit
            elif credit is not None:
                amt = credit  # Citi credits already carry a leading '-'
            else:
                continue
            person = default_person
            if use_member:
                mn = (r.get("Member Name") or "").strip().upper()
                if mn.startswith("WILLIAM"):
                    person = PERSON_WILLIAM
                elif mn.startswith("SELAINA"):
                    person = PERSON_LAINA
            if person is None and last4 in CARD_MAP:
                person = CARD_MAP[last4]
            rows.append(dict(account_id=account_id, trans_date=parse_date(r["Date"]),
                             description=d, amount=amt, person_id=person,
                             last4=last4, is_transfer=is_transfer(d)))


def latest_checking_balance():
    best = None
    for path in glob.glob(os.path.join(STMT, "Checking", "*.csv")) + \
               glob.glob(os.path.join(STMT, "Checking", "*.CSV")):
        with open(path, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if (r.get("Status") or "").strip().lower() != "posted":
                    continue
                bal, dt = money(r.get("Balance")), parse_date(r.get("Post Date"))
                if bal is None or dt is None:
                    continue
                if best is None or dt > best[1]:
                    best = (bal, dt)
    return best


def collect():
    rows = []
    for folder, aid in ACCT.items():
        files = glob.glob(os.path.join(STMT, folder, "*.csv")) + \
                glob.glob(os.path.join(STMT, folder, "*.CSV"))
        per_key_max = {}  # key -> (max per-file count, representative row)
        for path in files:
            file_rows = []
            if aid == 1:
                parse_checking(path, file_rows)
            elif aid == 3:
                parse_card(path, 3, file_rows, use_member=True)
            elif aid == 5:
                parse_card(path, 5, file_rows, default_person=PERSON_LAINA)
            else:
                parse_card(path, aid, file_rows)
            counts = {}
            for r in file_rows:
                k = (r["account_id"], r["trans_date"], r["description"], r["amount"])
                counts[k] = counts.get(k, 0) + 1
            for r in file_rows:
                k = (r["account_id"], r["trans_date"], r["description"], r["amount"])
                if counts[k] > per_key_max.get(k, (0, None))[0]:
                    per_key_max[k] = (counts[k], r)
        for k, (c, rep) in per_key_max.items():
            rows.extend([rep] * c)
    return rows


def sql_escape(s):
    return s.replace("'", "''")


def _staging_inserts(rows):
    lines = ["create temp table _stg (account_id int, trans_date date, "
             "description text, amount numeric, person_id int, last4 text, "
             "is_transfer boolean, uid text) on commit drop;"]
    vals, seen = [], {}
    for r in rows:
        k = (r["account_id"], r["trans_date"], r["description"], r["amount"])
        seen[k] = seen.get(k, 0) + 1
        raw = f"{r['account_id']}|{r['trans_date']}|{r['description']}|{r['amount']:.2f}|{seen[k]}"
        uid = hashlib.md5(raw.encode()).hexdigest()[:16]
        p = "null" if r["person_id"] is None else str(r["person_id"])
        l4 = "null" if not r["last4"] else f"'{r['last4']}'"
        vals.append(f"({r['account_id']},'{r['trans_date']}',"
                    f"'{sql_escape(r['description'])}',{r['amount']:.2f},{p},{l4},"
                    f"{str(r['is_transfer']).lower()},'{uid}')")
    for i in range(0, len(vals), 100):
        lines.append("insert into _stg (account_id,trans_date,description,amount,"
                     "person_id,last4,is_transfer,uid) values "
                     + ",".join(vals[i:i+100]) + ";")
    return lines


DEDUP_CTE = """with staged as (
  select *, row_number() over (partition by account_id,trans_date,description,amount
                               order by uid) as rn
  from _stg),
existing as (
  select account_id,trans_date,description,amount, count(*) cnt
  from public.transactions group by 1,2,3,4)"""

CATEGORIZE = """
update public.transactions t set subcategory_id = r.sub
from (
  select x.id, (array_agg(rl.subcategory_id order by length(rl.keyword) desc))[1] sub
  from public.transactions x
  join public.rules rl on position(lower(rl.keyword) in lower(x.description))>0
  where x.subcategory_id is null and coalesce(x.is_transfer,false)=false
  group by x.id
) r where t.id = r.id;"""


def build_sql(rows, bal):
    lines = ["-- Home Budget Monday import (dedup-safe via staging)"]
    lines += _staging_inserts(rows)
    lines.append(DEDUP_CTE + """
insert into public.transactions
  (account_id,trans_date,description,amount,person_id,last4,is_transfer,uid)
select s.account_id,s.trans_date,s.description,s.amount,s.person_id,s.last4,
       s.is_transfer,s.uid
from staged s
left join existing e using (account_id,trans_date,description,amount)
where s.rn > coalesce(e.cnt,0);""")
    lines.append(CATEGORIZE)
    if bal:
        lines.append(f"update public.settings set value='{bal[0]:.2f}' where key='checking_balance';")
        lines.append(f"update public.settings set value='{bal[1]}' where key='balance_asof';")
    return "\n".join(lines)


def build_dryrun_sql(rows):
    lines = _staging_inserts(rows)
    lines.append(DEDUP_CTE + """,
newrows as (
  select s.* from staged s
  left join existing e using (account_id,trans_date,description,amount)
  where s.rn > coalesce(e.cnt,0))
select account_id, count(*) new_rows, min(trans_date) first_new,
       max(trans_date) last_new
from newrows group by account_id order by account_id;""")
    return "\n".join(lines)


def main():
    rows = collect()
    bal = latest_checking_balance()
    if "--since" in sys.argv:
        floor = sys.argv[sys.argv.index("--since") + 1]
        rows = [r for r in rows if r["trans_date"] and r["trans_date"] >= floor]
    by = {}
    for r in rows:
        by[r["account_id"]] = by.get(r["account_id"], 0) + 1
    print("Parsed candidate rows (pre-dedup):")
    for aid in sorted(by):
        print(f"  {NAMES[aid]:18} {by[aid]:4}")
    print(f"  {'TOTAL':18} {len(rows):4}")
    if bal:
        print(f"Latest checking balance: ${bal[0]:,.2f} as of {bal[1]}")
    if "--dryrun" in sys.argv:
        path = os.path.join(BUDGET, "import_dryrun.sql")
        open(path, "w", encoding="utf-8").write(build_dryrun_sql(rows))
        print("Wrote import_dryrun.sql")
    else:
        path = os.path.join(BUDGET, "import.sql")
        open(path, "w", encoding="utf-8").write(build_sql(rows, bal))
        print("Wrote import.sql")


if __name__ == "__main__":
    main()
