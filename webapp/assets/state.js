// In-memory state shared between pages during one visit.
export const state = {
  run: null,          // latest dashboard monitoring run
  event: null,        // user-selected event on the dashboard (null = automatic selection)
  detail: null,       // analysis of the user-selected event
  source: null,       // label of the user-selected event
  layers: new Map(),  // cached Earth Engine tile URLs per analysed event
};
