/**
 * Password reset tokens. Single-use, short-lived, stored hashed.
 */

import { hashToken, randomToken } from "../platform/crypto";
import { store } from "../platform/store";
import { logger } from "../platform/logger";

export const RESET_TOKEN_TTL_SECONDS = 3600;

interface ResetToken {
  userId: string;
  tokenHash: string;
  issuedAt: number;
  consumed: boolean;
}

export async function issueResetToken(userId: string): Promise<string> {
  const token = randomToken(32);
  const record: ResetToken = {
    userId,
    tokenHash: hashToken(token),
    issuedAt: Date.now(),
    consumed: false,
  };
  await store.put(`reset:${record.tokenHash}`, record);
  return token;
}

export async function verifyResetToken(token: string): Promise<string | null> {
  const record = await store.get<ResetToken>(`reset:${hashToken(token)}`);
  if (!record || record.consumed) {
    return null;
  }

  const age = Date.now() - record.issuedAt;
  if (age > RESET_TOKEN_TTL_SECONDS) {
    logger.warn("reset token expired", {
      code: "RESET_TOKEN_EXPIRED",
      user: record.userId,
      ageMs: age,
    });
    return null;
  }

  record.consumed = true;
  await store.put(`reset:${record.tokenHash}`, record);
  return record.userId;
}
