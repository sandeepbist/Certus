-- ============================================================
-- Certus — Better Auth 1.7.1 schema
-- Generated from services/web/src/lib/auth.ts with the official CLI.
-- ============================================================

CREATE TABLE "user" (
    "id" TEXT NOT NULL PRIMARY KEY,
    "name" TEXT NOT NULL,
    "email" TEXT NOT NULL UNIQUE,
    "emailVerified" BOOLEAN NOT NULL,
    "image" TEXT,
    "createdAt" TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP NOT NULL,
    "updatedAt" TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP NOT NULL,
    "twoFactorEnabled" BOOLEAN
);

CREATE TABLE "session" (
    "id" TEXT NOT NULL PRIMARY KEY,
    "expiresAt" TIMESTAMPTZ NOT NULL,
    "token" TEXT NOT NULL UNIQUE,
    "createdAt" TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP NOT NULL,
    "updatedAt" TIMESTAMPTZ NOT NULL,
    "ipAddress" TEXT,
    "userAgent" TEXT,
    "userId" TEXT NOT NULL REFERENCES "user" ("id") ON DELETE CASCADE,
    "activeOrganizationId" TEXT
);

CREATE TABLE "account" (
    "id" TEXT NOT NULL PRIMARY KEY,
    "issuer" TEXT NOT NULL,
    "accountId" TEXT NOT NULL,
    "providerId" TEXT NOT NULL,
    "userId" TEXT NOT NULL REFERENCES "user" ("id") ON DELETE CASCADE,
    "accessToken" TEXT,
    "refreshToken" TEXT,
    "idToken" TEXT,
    "accessTokenExpiresAt" TIMESTAMPTZ,
    "refreshTokenExpiresAt" TIMESTAMPTZ,
    "scope" TEXT,
    "password" TEXT,
    "createdAt" TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP NOT NULL,
    "updatedAt" TIMESTAMPTZ NOT NULL
);

CREATE TABLE "verification" (
    "id" TEXT NOT NULL PRIMARY KEY,
    "identifier" TEXT NOT NULL,
    "value" TEXT NOT NULL,
    "expiresAt" TIMESTAMPTZ NOT NULL,
    "createdAt" TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP NOT NULL,
    "updatedAt" TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE TABLE "organization" (
    "id" TEXT NOT NULL PRIMARY KEY,
    "name" TEXT NOT NULL,
    "slug" TEXT NOT NULL UNIQUE,
    "logo" TEXT,
    "createdAt" TIMESTAMPTZ NOT NULL,
    "metadata" TEXT
);

CREATE TABLE "member" (
    "id" TEXT NOT NULL PRIMARY KEY,
    "organizationId" TEXT NOT NULL REFERENCES "organization" ("id") ON DELETE CASCADE,
    "userId" TEXT NOT NULL REFERENCES "user" ("id") ON DELETE CASCADE,
    "role" TEXT NOT NULL,
    "createdAt" TIMESTAMPTZ NOT NULL
);

CREATE TABLE "invitation" (
    "id" TEXT NOT NULL PRIMARY KEY,
    "organizationId" TEXT NOT NULL REFERENCES "organization" ("id") ON DELETE CASCADE,
    "email" TEXT NOT NULL,
    "role" TEXT,
    "status" TEXT NOT NULL,
    "expiresAt" TIMESTAMPTZ NOT NULL,
    "createdAt" TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP NOT NULL,
    "inviterId" TEXT NOT NULL REFERENCES "user" ("id") ON DELETE CASCADE
);

CREATE TABLE "twoFactor" (
    "id" TEXT NOT NULL PRIMARY KEY,
    "secret" TEXT NOT NULL,
    "backupCodes" TEXT NOT NULL,
    "userId" TEXT NOT NULL REFERENCES "user" ("id") ON DELETE CASCADE,
    "verified" BOOLEAN,
    "failedVerificationCount" INTEGER,
    "lockedUntil" TIMESTAMPTZ
);

CREATE TABLE "passkey" (
    "id" TEXT NOT NULL PRIMARY KEY,
    "name" TEXT,
    "publicKey" TEXT NOT NULL,
    "userId" TEXT NOT NULL REFERENCES "user" ("id") ON DELETE CASCADE,
    "credentialID" TEXT NOT NULL,
    "counter" INTEGER NOT NULL,
    "deviceType" TEXT NOT NULL,
    "backedUp" BOOLEAN NOT NULL,
    "transports" TEXT,
    "createdAt" TIMESTAMPTZ,
    "aaguid" TEXT
);

CREATE TABLE "apikey" (
    "id" TEXT NOT NULL PRIMARY KEY,
    "configId" TEXT NOT NULL,
    "name" TEXT,
    "start" TEXT,
    "referenceId" TEXT NOT NULL,
    "prefix" TEXT,
    "key" TEXT NOT NULL,
    "refillInterval" INTEGER,
    "refillAmount" INTEGER,
    "lastRefillAt" TIMESTAMPTZ,
    "enabled" BOOLEAN,
    "rateLimitEnabled" BOOLEAN,
    "rateLimitTimeWindow" INTEGER,
    "rateLimitMax" INTEGER,
    "requestCount" INTEGER,
    "remaining" INTEGER,
    "lastRequest" TIMESTAMPTZ,
    "expiresAt" TIMESTAMPTZ,
    "createdAt" TIMESTAMPTZ NOT NULL,
    "updatedAt" TIMESTAMPTZ NOT NULL,
    "permissions" TEXT,
    "metadata" TEXT
);

CREATE INDEX "session_userId_idx" ON "session" ("userId");
CREATE INDEX "account_userId_idx" ON "account" ("userId");
CREATE INDEX "verification_identifier_idx" ON "verification" ("identifier");
CREATE INDEX "member_organizationId_idx" ON "member" ("organizationId");
CREATE INDEX "member_userId_idx" ON "member" ("userId");
CREATE INDEX "invitation_organizationId_idx" ON "invitation" ("organizationId");
CREATE INDEX "invitation_email_idx" ON "invitation" ("email");
CREATE INDEX "twoFactor_secret_idx" ON "twoFactor" ("secret");
CREATE INDEX "twoFactor_userId_idx" ON "twoFactor" ("userId");
CREATE INDEX "passkey_userId_idx" ON "passkey" ("userId");
CREATE INDEX "passkey_credentialID_idx" ON "passkey" ("credentialID");
CREATE INDEX "apikey_configId_idx" ON "apikey" ("configId");
CREATE INDEX "apikey_referenceId_idx" ON "apikey" ("referenceId");
CREATE INDEX "apikey_key_idx" ON "apikey" ("key");
CREATE UNIQUE INDEX "account_issuer_accountId_uidx" ON "account" ("issuer", "accountId");
