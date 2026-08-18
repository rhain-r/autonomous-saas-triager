/**
 * Stripe webhook ingress.
 *
 * Stripe retries any delivery it does not see a 2xx for, with exponential
 * backoff, for up to three days. Handlers must therefore be safe to run more
 * than once for the same event id.
 */

import { ledger } from "./ledger";
import { sendReceipt } from "./receipts";
import { logger } from "../platform/logger";

interface StripeEvent {
  id: string;
  type: string;
  created: number;
  data: { object: { id: string; customer: string; amount: number; currency: string } };
}

export async function handleStripeWebhook(event: StripeEvent) {
  logger.info("stripe webhook received", { code: "BILLING_EVENT", detail: event.id });

  switch (event.type) {
    case "charge.succeeded":
      return applyCharge(event);
    case "charge.refunded":
      return ledger.reverse(event.data.object.id);
    default:
      return { status: 204 };
  }
}

async function applyCharge(event: StripeEvent) {
  const charge = event.data.object;

  await ledger.credit({
    customerId: charge.customer,
    amount: charge.amount,
    currency: charge.currency,
    reference: charge.id,
  });

  await sendReceipt(charge.customer, charge.amount, charge.currency);

  return { status: 200 };
}
