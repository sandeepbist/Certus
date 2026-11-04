-- Better Auth 1.7.3+ no longer writes issuer.
CREATE UNIQUE INDEX "account_providerId_accountId_uidx"
    ON "account" ("providerId", "accountId");

ALTER TABLE "account" ALTER COLUMN "issuer" DROP NOT NULL;
DROP INDEX "account_issuer_accountId_uidx";
