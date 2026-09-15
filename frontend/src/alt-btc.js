export function selectAltBtcRows(rows, query, filter, sort) {
  const search = query.trim().toUpperCase();
  return rows.filter(row => `${row.asset} ${row.symbol} ${row.binanceSymbol}`.includes(search)
    && (filter === "all" || (filter === "up" ? row.changeVsBtcPct > 0 : row.changeVsBtcPct < 0)))
    .sort((a, b) => (sort === "worst" ? 1 : -1) * (a.changeVsBtcPct - b.changeVsBtcPct)
      || a.symbol.localeCompare(b.symbol));
}
