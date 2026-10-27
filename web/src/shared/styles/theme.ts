// Motion and layout constants for components (colours and type live in tokens.css as CSS variables).

export const motion = {
  fast: 0.12,
  base: 0.22,
  ease: [0.2, 0.7, 0.2, 1] as const,
  /** the one orchestrated page-load moment: ledger rows settle in, top to bottom */
  rowStagger: 0.018,
  rowRise: 6,
  /** an adjustment being struck through when a reviewer accepts it */
  strike: 0.28,
};

export const layout = {
  narrow: 900, // px: below this the inspector becomes a sheet under the ledger
  queuePageSize: 200,
};

export const marks = {
  rule: "■",
  person: "●",
  open: "○",
} as const;
