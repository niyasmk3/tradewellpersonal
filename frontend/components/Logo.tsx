"use client";

/** Tradewell mark — shield + ascending candlesticks + upward arrow. */
export function Logo({ size = 26 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 48 48" fill="none" xmlns="http://www.w3.org/2000/svg" aria-label="Tradewell">
      <defs>
        <linearGradient id="tw-shield" x1="7" y1="3" x2="41" y2="45" gradientUnits="userSpaceOnUse">
          <stop stopColor="#1b3a6b" />
          <stop offset="1" stopColor="#2563eb" />
        </linearGradient>
        <linearGradient id="tw-arrow" x1="10" y1="40" x2="40" y2="14" gradientUnits="userSpaceOnUse">
          <stop stopColor="#14b8a6" />
          <stop offset="1" stopColor="#2dd4bf" />
        </linearGradient>
      </defs>
      {/* shield */}
      <path
        d="M24 3.5 L40 9 V23.5 C40 33.5 33 41 24 44.5 C15 41 8 33.5 8 23.5 V9 Z"
        fill="url(#tw-shield)"
        opacity="0.16"
        stroke="url(#tw-shield)"
        strokeWidth="2.2"
        strokeLinejoin="round"
      />
      {/* ascending candlesticks */}
      <g strokeLinecap="round">
        <line x1="17" y1="26" x2="17" y2="34" stroke="#2563eb" strokeWidth="1.2" />
        <rect x="15.2" y="28" width="3.6" height="5" rx="0.8" fill="#2563eb" />
        <line x1="23" y1="20" x2="23" y2="30" stroke="#1d4ed8" strokeWidth="1.2" />
        <rect x="21.2" y="22" width="3.6" height="6" rx="0.8" fill="#1d4ed8" />
        <line x1="29" y1="15" x2="29" y2="25" stroke="#14b8a6" strokeWidth="1.2" />
        <rect x="27.2" y="17" width="3.6" height="6" rx="0.8" fill="#14b8a6" />
      </g>
      {/* upward arrow */}
      <path
        d="M13 33 C20 33 24 30 34 18"
        stroke="url(#tw-arrow)"
        strokeWidth="2.4"
        strokeLinecap="round"
        fill="none"
      />
      <path d="M30 17.5 L35 16.5 L34.5 21.5 Z" fill="#2dd4bf" />
    </svg>
  );
}
