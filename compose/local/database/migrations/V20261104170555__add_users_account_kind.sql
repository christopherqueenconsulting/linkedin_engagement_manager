-- Which kind of account a row is, so a role can be allowed to approve ONLY on an internal one.
--
-- `users.account_kind` is 'standard' (a real customer or owner account — today, every row),
-- 'internal' (an account the operator runs for their own testing) or 'sandbox' (a throwaway used
-- to exercise the product). The `operator` session scope may approve queued work only when the
-- account it acts on is 'internal' or 'sandbox'; on 'standard' it behaves exactly like `agent`
-- (it can queue, it cannot approve). The runtime reader (`get_user_account_kind`) treats any value
-- it cannot read or does not recognise as 'standard' — fail closed.
--
-- The column DEFAULT is 'standard', so every EXISTING row reads 'standard' and nothing changes
-- behaviour until a row is deliberately set by hand. VARCHAR, not ENUM: no enum-widening hazard
-- (see README.md § "Widening an ENUM"), and the value set is validated in code. A single DEFAULTed
-- column is additive, so the migration classifier lets it deploy on its own.
--
-- ROLLBACK (not run by Flyway): ALTER TABLE users DROP COLUMN account_kind;
-- Dropping it is safe at any time — the reader degrades to 'standard' on a missing column — but do
-- revert the code that reads it first, or the lookup logs an error per operator request.

ALTER TABLE users
    ADD COLUMN account_kind VARCHAR(16) NOT NULL DEFAULT 'standard';
