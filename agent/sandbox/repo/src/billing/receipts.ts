/**
 * Customer receipt email. Fire-and-forget; failures are logged, never thrown,
 * so that a mail outage cannot roll back a settled payment.
 */

import { logger } from "../platform/logger";

export async function sendReceipt(customerId: string, amount: number, currency: string) {
  try {
    await fetch("https://mail.meridian.io/v1/receipts", {
      method: "POST",
      body: JSON.stringify({ customerId, amount, currency }),
    });
  } catch (err) {
    logger.warn("receipt delivery failed", { code: "MAIL_DEFERRED", detail: String(err) });
  }
}
