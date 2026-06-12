-- HOME BUDGET — database schema (Supabase Postgres)
-- Data-free DDL backup. Current as of 2026-06-12.

create table people (id serial primary key, name text unique not null);
create table categories (id serial primary key, name text unique not null, is_income boolean default false);
create table subcategories (
  id serial primary key,
  category_id int references categories(id),
  name text unique not null,
  monthly_budget numeric(10,2) default 0,
  active boolean default true
);
create table accounts (id serial primary key, name text unique not null, type text, notes text);
create table transactions (
  id serial primary key,
  account_id int references accounts(id),
  trans_date date not null,
  description text not null,
  amount numeric(10,2) not null,          -- positive = expense, negative = credit/income
  subcategory_id int references subcategories(id),
  person_id int references people(id),
  last4 text,
  notes text,
  is_transfer boolean default false,      -- excluded from bucket math
  uid text unique                         -- md5 for dedup-safe re-imports
);
create table rules (id serial primary key, keyword text not null, subcategory_id int references subcategories(id), person_id int references people(id), source text);
create table card_map (last4 text primary key, person_id int references people(id), note text);
create table settings (key text primary key, value text);  -- start_date, checking_balance, balance_asof
create table bucket_adjustments (
  id serial primary key,
  subcategory_id int references subcategories(id),
  adj_date date default current_date,
  amount numeric(10,2) not null,          -- +adds to bucket, -removes; transfers = two rows
  note text,
  created_at timestamptz default now()
);
create table transaction_splits (
  id serial primary key,
  transaction_id int references transactions(id) on delete cascade,
  subcategory_id int references subcategories(id),
  amount numeric(10,2) not null,
  note text
);
create table split_templates (id serial primary key, name text unique not null, items jsonb not null);

-- Row-level security: authenticated users only
do $$ declare t text;
begin
  foreach t in array array['people','categories','subcategories','accounts','transactions',
    'rules','card_map','settings','bucket_adjustments','transaction_splits','split_templates'] loop
    execute format('alter table %I enable row level security', t);
    execute format('create policy "auth_all_%s" on %I for all to authenticated using (true) with check (true)', t, t);
    execute format('revoke all on %I from anon', t);
  end loop;
end $$;

-- Views
create view months_elapsed as
 select (extract(year from age(current_date,(select value::date from settings where key='start_date')))*12
       + extract(month from age(current_date,(select value::date from settings where key='start_date'))) + 1)::int as months;

create view bucket_status as
 select sc.id, c.name as category, sc.name as subcategory, sc.monthly_budget,
  round(sc.monthly_budget*(select months from months_elapsed),2) as budgeted_to_date,
  coalesce((select sum(t.amount) from transactions t where t.subcategory_id=sc.id and not t.is_transfer),0)
   + coalesce((select sum(s.amount) from transaction_splits s where s.subcategory_id=sc.id),0) as spent_to_date,
  coalesce((select sum(a.amount) from bucket_adjustments a where a.subcategory_id=sc.id),0) as adjustments,
  round(sc.monthly_budget*(select months from months_elapsed),2)
   - (coalesce((select sum(t.amount) from transactions t where t.subcategory_id=sc.id and not t.is_transfer),0)
      + coalesce((select sum(s.amount) from transaction_splits s where s.subcategory_id=sc.id),0))
   + coalesce((select sum(a.amount) from bucket_adjustments a where a.subcategory_id=sc.id),0) as balance
 from subcategories sc join categories c on c.id=sc.category_id
 where not c.is_income and sc.active;

create view dashboard as
 select (select value::numeric from settings where key='checking_balance') as checking_balance,
 (select coalesce(sum(balance),0) from bucket_status where balance>0) as total_set_aside,
 (select value::numeric from settings where key='checking_balance') - (select coalesce(sum(balance),0) from bucket_status where balance>0) as safe_to_spend,
 (select count(*) from transactions t where t.subcategory_id is null and not t.is_transfer
   and not exists (select 1 from transaction_splits s where s.transaction_id=t.id)) as uncategorized_count;

alter view months_elapsed set (security_invoker = true);
alter view bucket_status set (security_invoker = true);
alter view dashboard set (security_invoker = true);
