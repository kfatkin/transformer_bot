export const ACTIVE_TRADE_STATUSES = new Set(["ACTIVE_CALENDAR", "RISK_FREE_CONDOR"]);

const calendar = ["short_put", "back_put", "short_call", "back_call"];
const condor = ["short_put", "outer_put", "short_call", "outer_call"];

export function tradeLegGroups(trade) {
  const closed = !ACTIVE_TRADE_STATUSES.has(trade.status);
  const legs = (names) => names.map(name => ({
    name,
    position: name.startsWith("short_") ? "Short / SELL" : "Long / BUY",
    quantity: trade.quantity,
    contract: trade.contracts?.[name] ?? null,
  }));
  const entry = {
    stage: "calendar",
    title: "Entry calendar legs",
    current: !closed && !trade.transformed,
    description: trade.transformed
      ? "Original entry: back-month longs were sold at transformation; the front shorts carried into the condor."
      : closed ? "Historical entry positions. This trade is closed; no legs remain open."
        : "Current paper position: front-expiry shorts and back-expiry longs. Outer wings are not held.",
    legs: legs(calendar),
  };
  if (!trade.transformed) return [entry];
  return [{
    stage: "condor",
    title: "Post-transform condor legs",
    current: !closed,
    description: closed
      ? "Final structure before closure or settlement. No legs remain open."
      : "Current paper position: front-expiry shorts and protective outer wings. Back-month longs are no longer held.",
    legs: legs(condor),
  }, entry];
}
