import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Tradewell — F&O Signal Dashboard",
  description: "Real-time F&O advisory and decision-support (Phase 1: live market dashboard)",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen antialiased">{children}</body>
    </html>
  );
}
