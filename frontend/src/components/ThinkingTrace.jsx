import { useEffect, useLayoutEffect, useRef, useState } from "react";

/* Expandable agent trace (Steps variant styling). Props drive everything:
   working     spinner header, rows appear as they arrive
   doneLabel   settled header text ("Executed 6 actions", "Filled 1/1 …")
   rows        [{ status: ok|warn|error|sim, primary, secondary?, mono?, href? }]
   The trace settles once and remains expandable. */

function CheckIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--color-ink-3)" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" className="shrink-0">
      <path d="M20 6L9 17l-5-5" />
    </svg>
  );
}

function WarnIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--color-orange)" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" className="shrink-0">
      <path d="M12 3l10 18H2L12 3z" />
      <path d="M12 10v4" />
      <circle cx="12" cy="17.4" r="0.4" fill="var(--color-orange)" />
    </svg>
  );
}

function RowIcon({ status, working, isLast }) {
  if (status === "error") return <WarnIcon />;
  if (status === "warn") return <WarnIcon />;
  if (status === "sim") {
    return (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--color-ink-3)" strokeWidth="2" strokeLinecap="round" className="shrink-0">
        <circle cx="12" cy="12" r="9" />
        <path d="M8 12h8M12 8v8" />
      </svg>
    );
  }
  return working && isLast ? (
    <span className="size-3 shrink-0 rounded-full border-[1.5px] border-line-strong border-t-ink-2" style={{ animation: "spin 700ms linear infinite" }} />
  ) : (
    <CheckIcon />
  );
}

export default function ThinkingTrace({ working = false, rows = [], doneLabel = "", workingLabel = "Executing…", defaultExpanded = null }) {
  const [manualExpanded, setManualExpanded] = useState(null);
  const expanded = manualExpanded ?? (working ? true : defaultExpanded ?? false);
  const traceRef = useRef(null);
  const [lineHeight, setLineHeight] = useState(0);

  useLayoutEffect(() => {
    if (traceRef.current) setLineHeight(traceRef.current.offsetHeight);
  }, [rows.length, expanded, working]);

  return (
    <div className="flex w-full max-w-95 flex-col" style={{ minHeight: working || expanded ? 96 : undefined }}>
      <button
        type="button"
        aria-expanded={expanded}
        onClick={() => setManualExpanded(current => !(current ?? (working || defaultExpanded || false)))}
        className="-mx-1.5 flex w-fit items-center gap-2 rounded-control px-1.5 py-1 transition-colors duration-100 hover:bg-hover-2"
      >
        <svg width="16" height="16" viewBox="0 0 24 24" fill={working ? "var(--color-ink-2)" : "var(--color-ink-3)"}>
          <path d="M12 2l2.4 7.2L22 12l-7.6 2.8L12 22l-2.4-7.2L2 12l7.6-2.8z" />
        </svg>
        <span role="status" className="contents">
          {working ? (
            <span
              className="bg-clip-text text-[13px] font-medium whitespace-nowrap text-transparent"
              style={{
                backgroundImage: "linear-gradient(90deg, var(--color-ink-3) 35%, var(--color-ink) 50%, var(--color-ink-3) 65%)",
                backgroundSize: "200% 100%",
                animation: "shimmer-text 1.4s linear infinite",
              }}
            >
              {workingLabel}
            </span>
          ) : (
            <span className="text-[13px] font-medium whitespace-nowrap text-ink-2" style={{ animation: "fade-in 350ms ease-out both" }}>
              {doneLabel}
            </span>
          )}
        </span>
        <svg
          width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--color-ink-3)" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"
          className="transition-transform duration-300"
          style={{ transform: expanded ? "rotate(180deg)" : "rotate(0)" }}
        >
          <path d="M6 9l6 6 6-6" />
        </svg>
      </button>

      <div
        className="grid transition-[grid-template-rows,opacity] duration-300"
        style={{
          gridTemplateRows: expanded ? "1fr" : "0fr",
          opacity: expanded ? 1 : 0,
          transitionTimingFunction: "cubic-bezier(0.23, 1, 0.32, 1)",
        }}
      >
        <div className="overflow-hidden">
          <div className="relative mt-1 ml-[5px] pl-4">
            <span
              aria-hidden
              className="absolute left-[3px] w-px bg-line"
              style={{ top: -8, height: lineHeight ? lineHeight - 2 : 0 }}
            />
            <div ref={traceRef} className="flex flex-col gap-1 py-1">
              {rows.map((row, i) => {
                const status = row.status || "ok";
                const isLast = i === rows.length - 1;
                const content = (
                  <>
                    <RowIcon status={status} working={working} isLast={isLast} />
                    <span className={"min-w-0 truncate text-[12.5px] " + (status === "error" ? "text-red" : status === "warn" ? "text-orange" : "font-medium text-ink")}>
                      {row.primary}
                    </span>
                    {row.secondary && (
                      <span className={"shrink-0 text-[11.5px] text-ink-3 " + (row.mono ? "font-mono" : "")}>{row.secondary}</span>
                    )}
                    {row.add !== undefined && (
                      <span className="shrink-0 font-mono text-[11px] tabular-nums">
                        <span className="text-green">+{row.add}</span>{" "}
                        {row.del !== undefined && <span className="text-red">−{row.del}</span>}
                      </span>
                    )}
                  </>
                );
                const cls = "flex min-h-7 w-full items-center gap-2 rounded-[6px] px-1.5 py-0.5 text-left";
                return (
                  <div key={i} className={cls} style={{ animation: `fade-up 320ms cubic-bezier(0.23,1,0.32,1) ${Math.min(i, 12) * 90}ms both` }}>
                    {content}
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
