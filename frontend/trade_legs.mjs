export const ACTIVE_TRADE_STATUSES = new Set(["ACTIVE_CALENDAR", "RISK_FREE_CONDOR", "ACTIVE_DEBIT_SPREAD", "RISK_FREE_BUTTERFLY"]);

const calendar = ["short_put", "back_put", "short_call", "back_call"];
const condor = ["short_put", "outer_put", "short_call", "outer_call"];

export function tradeLegGroups(trade) {
  const closed = !ACTIVE_TRADE_STATUSES.has(trade.status);
  if (trade.strategy === "ZERO_DTE_BUTTERFLY") {
    const legs = (names, transformed) => names.map(name => ({
      name,
      position: name === "short" ? "Short / SELL" : "Long / BUY",
      quantity: trade.quantity * (name === "short" && transformed ? 2 : 1),
      contract: trade.contracts?.[name] ?? null,
    }));
    const entry = {
      stage: "debit", title: "Entry debit-spread legs",
      current: !closed && !trade.transformed,
      description: closed ? "Historical entry positions; no legs remain open."
        : trade.transformed ? "Original debit spread before adding the second short and outer long."
          : "Current paper position: one long and one short. The outer wing is not held.",
      legs: legs(["long", "short"], false),
    };
    if (!trade.transformed) return [entry];
    return [{
      stage: "butterfly", title: "Post-transform butterfly legs", current: !closed,
      description: closed ? "Final structure before closure or settlement; no legs remain open."
        : "Current balanced butterfly: one long, TWO middle shorts, one outer long per spread.",
      legs: legs(["long", "short", "outer"], true),
    }, entry];
  }
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
