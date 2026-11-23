// Number formats shared between a card and its sheet.
//
// A card and its sheet show the same figures, so the rounding rule has to
// live in one place: two roundings of one number read as a bug, and the
// sparkline's own axis labels are the third reader of it.

// One decimal until the number is big enough that the decimal is noise. The
// cut sits at 100 rather than 10 so a distance prints the same on the card,
// on the sheet, and on a chart axis.
export const fmtKm = (km) =>
  (km == null ? "—" : `${km < 100 ? km.toFixed(1) : Math.round(km)} km`);

// Money in its own currency, the locale's way. The card and the Money sheet
// print the same balances, so they print them through the same formatter.
// `signed` adds a + to inflows (transaction rows); callers flip Plaid's sign
// convention (positive = money leaving) before they get here.
export function fmtMoney(v, currency, signed = false) {
  return new Intl.NumberFormat(undefined, {
    style: "currency", currency: currency || "USD",
    ...(signed ? { signDisplay: "exceptZero" } : {}),
  }).format(v);
}
