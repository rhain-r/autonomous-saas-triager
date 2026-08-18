/**
 * Append-only billing ledger. Entries are never mutated, only reversed.
 */

import { store } from "../platform/store";

interface CreditEntry {
  customerId: string;
  amount: number;
  currency: string;
  reference: string;
}

export const ledger = {
  async credit(entry: CreditEntry) {
    const entries = (await store.get<CreditEntry[]>(`ledger:${entry.customerId}`)) ?? [];
    entries.push(entry);
    await store.put(`ledger:${entry.customerId}`, entries);
    return entry;
  },

  async reverse(reference: string) {
    await store.put(`ledger:reversal:${reference}`, { reference, at: Date.now() });
    return { status: 200 };
  },
};
