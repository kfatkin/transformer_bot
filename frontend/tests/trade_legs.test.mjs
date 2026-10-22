import assert from "node:assert/strict";
import test from "node:test";
import {tradeLegGroups} from "../trade_legs.mjs";

function trade(status = "ACTIVE_CALENDAR", transformed = false) {
  const front = "2026-10-30";
  const back = "2026-11-02";
  const strikes = {
    short_put:"7645.5", back_put:"7645.5", outer_put:"7640.5",
    short_call:"7800", back_call:"7800", outer_call:"7805",
  };
  return {
    status, transformed, quantity:2,
    contracts:Object.fromEntries(Object.entries(strikes).map(([name,strike],index) => [
      name, {symbol:"SPX",strike,expiry:name.startsWith("back_") ? back : front,
        right:name.endsWith("put") ? "P" : "C",con_id:1000 + index},
    ])),
  };
}

test("calendar shows exactly the four held legs, not prospective wings",() => {
  const value = trade();
  const groups = tradeLegGroups(value);
  assert.equal(groups.length,1);
  assert.equal(groups[0].current,true);
  assert.deepEqual(groups[0].legs.map(leg => leg.name),[
    "short_put","back_put","short_call","back_call",
  ]);
  for (const leg of groups[0].legs) {
    assert.equal(leg.quantity,2);
    assert.deepEqual(leg.contract,value.contracts[leg.name]);
  }
  assert.equal(groups[0].legs[0].contract.strike,"7645.5");
  assert.equal(groups[0].legs[0].contract.expiry,"2026-10-30");
  assert.equal(groups[0].legs[1].contract.expiry,"2026-11-02");
  assert.equal(groups[0].legs[0].position,"Short / SELL");
  assert.equal(groups[0].legs[1].position,"Long / BUY");
});

test("condor shows front wings as held and original back longs only as historical",() => {
  const groups = tradeLegGroups(trade("RISK_FREE_CONDOR",true));
  assert.equal(groups.length,2);
  assert.equal(groups[0].stage,"condor");
  assert.equal(groups[0].current,true);
  assert.deepEqual(groups[0].legs.map(leg => leg.name),[
    "short_put","outer_put","short_call","outer_call",
  ]);
  assert.ok(groups[0].legs.every(leg => leg.contract.expiry === "2026-10-30"));
  assert.equal(groups[0].legs[1].contract.strike,"7640.5");
  assert.equal(groups[1].stage,"calendar");
  assert.equal(groups[1].current,false);
});

for (const [status,transformed] of [
  ["CLOSED_STOP_LOSS",false],["CLOSED_MAX_PROFIT",true],["CLOSED_FLOOR",true],
]) {
  test(`${status} retains contract history without claiming open positions`,() => {
    const groups = tradeLegGroups(trade(status,transformed));
    assert.ok(groups.every(group => !group.current));
    assert.equal(groups.length,transformed ? 2 : 1);
    assert.ok(groups[0].description.includes("No legs remain open") ||
      groups[0].description.includes("no legs remain open"));
  });
}

test("missing contract metadata remains an explicit missing leg",() => {
  const value = trade();
  delete value.contracts.back_put;
  const group = tradeLegGroups(value)[0];
  assert.equal(group.legs.length,4);
  assert.equal(group.legs[1].name,"back_put");
  assert.equal(group.legs[1].contract,null);
});

test("building display groups never mutates the persisted trade shape",() => {
  const value = trade("RISK_FREE_CONDOR",true);
  const original = structuredClone(value);
  tradeLegGroups(value);
  assert.deepEqual(value,original);
});

function butterfly(status = "ACTIVE_DEBIT_SPREAD", transformed = false) {
  return {
    strategy:"ZERO_DTE_BUTTERFLY", status, transformed, quantity:3,
    contracts:Object.fromEntries(["long","short","outer"].map((name,i) => [
      name, {symbol:"SPX",strike:6000 + i*5,expiry:"2026-10-20",right:"C",con_id:2000+i},
    ])),
  };
}

test("debit spread has only its two held legs",() => {
  const groups = tradeLegGroups(butterfly());
  assert.equal(groups.length,1);
  assert.equal(groups[0].current,true);
  assert.deepEqual(groups[0].legs.map(leg => leg.name),["long","short"]);
  assert.deepEqual(groups[0].legs.map(leg => leg.quantity),[3,3]);
});

test("funded butterfly doubles only the CURRENT middle shorts",() => {
  const value = butterfly("RISK_FREE_BUTTERFLY",true);
  const original = structuredClone(value);
  const groups = tradeLegGroups(value);
  assert.deepEqual(groups[0].legs.map(leg => leg.quantity),[3,6,3]);
  assert.deepEqual(groups[1].legs.map(leg => leg.quantity),[3,3]);
  assert.equal(groups[0].legs[1].position,"Short / SELL");
  assert.equal(groups[0].current,true);
  assert.equal(groups[1].current,false);
  assert.deepEqual(value,original);
});

for (const [status,transformed] of [
  ["CLOSED_0DTE_TIME_EXIT",false],["CLOSED_UNTRANSFORMED_EXPIRY",false],
  ["CLOSED_PARTIAL_PROFIT",true],["CLOSED_FLOOR",true],
]) {
  test(`${status} butterfly keeps history without claiming current positions`,() => {
    assert.ok(tradeLegGroups(butterfly(status,transformed)).every(group => !group.current));
  });
}
